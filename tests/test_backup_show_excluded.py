#!/usr/bin/env python3
"""backup --dry-run --show-excluded 预览的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum] [--exclude PATH]...
                            [--exclude-dir PATH]... --dry-run
                            [--show-excluded]

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。

验收演示目录：

- ``note.txt``：内容为 ``note``；
- ``cache/a.bin``：内容为 ``aaa``；
- ``cache/b.bin``：内容为 ``bbb``。

覆盖约定：

1. 合法预览：退出码 0、标准错误为空、标准输出恰好一行 JSON 对象加末尾
   换行，对象在 source、snapshot、files、paths 之外仅多 excluded_paths；
   ``--exclude-dir cache`` 时 files 为 1、paths 为 ["note.txt"]、
   excluded_paths 为 ["cache/a.bin", "cache/b.bin"]。
2. paths 与 excluded_paths 不重叠且覆盖全部普通文件；未排除文件时
   excluded_paths 为空数组；合法排除全部文件时 files 为 0、paths 为空。
   重复规则、父子目录、单文件与目录规则重叠命中的文件只列一次；目录
   本身不列入，排除存在的空目录仍合法；中文、空格与大小写逐字保留，
   排序为 Unicode 码点升序。
3. 不传 --show-excluded 时预览仍只有原有四个字段。
4. --show-excluded 未与 --dry-run 同用：退出码 2、标准输出为空、
   标准错误包含“--show-excluded 只能与 --dry-run 一起使用”。
5. 沿用全部备份前检查与现有错误原因（被排除区域仍参与安全检查）：
   任一失败退出码 2、标准输出为空；预览成功或失败都不创建快照、父目录
   或任何文件，不改动源目录。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = ROOT / "backup.py"

CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# 验收演示目录（斜杠分隔的相对路径 -> 字节）。
NOTE_REL = "note.txt"
A_REL = "cache/a.bin"
B_REL = "cache/b.bin"

DEMO_FILES = {
    NOTE_REL: b"note",
    A_REL: b"aaa",
    B_REL: b"bbb",
}

REASON_FLAG_ONLY_WITH_DRY_RUN = "--show-excluded 只能与 --dry-run 一起使用"
REASON_EXCLUDE_UNMATCHED = "排除项未匹配普通文件"
REASON_SYMLINK = "源目录中包含符号链接"


def run_cmd(argv):
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), *argv],
        cwd=str(ROOT),
        env=CHILD_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_files(base, files):
    base = Path(base)
    for rel, data in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def capture_tree(root):
    """递归记录目录树：相对路径 -> (类型, 字节或链接目标)。"""
    root = Path(root)
    tree = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_dir = Path(dirpath).relative_to(root)
        for name in dirnames:
            path = Path(dirpath) / name
            key = (rel_dir / name).as_posix()
            if path.is_symlink():
                tree[key] = ("symlink", os.readlink(path))
            else:
                tree[key] = ("dir", None)
        for name in filenames:
            path = Path(dirpath) / name
            key = (rel_dir / name).as_posix()
            if path.is_symlink():
                tree[key] = ("symlink", os.readlink(path))
            else:
                tree[key] = ("file", path.read_bytes())
    return tree


class BackupShowExcludedTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-show-excl-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "demo"
        # 目标的父目录故意不存在，用于核对预览不创建父目录。
        self.snapshot = self.work / "nested" / "snapshot"
        write_files(self.source, DEMO_FILES)

    def run_preview(self, *rules, show_excluded=True, snapshot=None,
                    checksum=False):
        argv = ["backup", str(self.source), str(snapshot or self.snapshot)]
        for kind, raw in rules:
            argv.extend([kind, raw])
        if checksum:
            argv.append("--checksum")
        argv.append("--dry-run")
        if show_excluded:
            argv.append("--show-excluded")
        return run_cmd(argv)

    def parse_preview(self, proc):
        """成功预览的标准输出必须是单行 JSON 对象加末尾换行。"""
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")
        self.assertTrue(
            proc.stdout.endswith(b"\n"),
            f"预览输出应以换行结束: {proc.stdout!r}",
        )
        line = proc.stdout[:-1]
        self.assertNotIn(b"\n", line, "预览输出应只有一行")
        doc = json.loads(line.decode("utf-8"))
        self.assertEqual(
            set(doc.keys()),
            {"source", "snapshot", "files", "paths", "excluded_paths"},
        )
        return doc

    # ---- 验收场景 ----

    def test_acceptance_exclude_cache_dir(self):
        source_before = capture_tree(self.source)
        proc = self.run_preview(("--exclude-dir", "cache"))
        doc = self.parse_preview(proc)

        self.assertEqual(doc["source"], str(self.source.resolve()))
        self.assertEqual(doc["snapshot"], str(self.snapshot.resolve()))
        self.assertEqual(doc["files"], 1)
        self.assertEqual(doc["paths"], [NOTE_REL])
        self.assertEqual(doc["excluded_paths"], [A_REL, B_REL])

        self.assertFalse(os.path.lexists(self.snapshot))
        self.assertFalse(os.path.lexists(self.snapshot.parent))
        self.assertEqual(capture_tree(self.source), source_before)
        self.assertEqual(
            sorted(p.name for p in self.work.iterdir()), ["demo"]
        )

    def test_paths_and_excluded_partition_all_regular_files(self):
        proc = self.run_preview(
            ("--exclude", A_REL), ("--exclude-dir", "cache")
        )
        doc = self.parse_preview(proc)
        all_files = sorted(DEMO_FILES)
        union = sorted(doc["paths"] + doc["excluded_paths"])
        self.assertEqual(union, all_files)
        # 单文件规则与目录规则重叠命中：只列一次。
        self.assertEqual(doc["excluded_paths"], [A_REL, B_REL])
        self.assertEqual(set(doc["paths"]) & set(doc["excluded_paths"]), set())
        self.assertEqual(doc["files"], len(doc["paths"]))

    def test_duplicate_file_rule_lists_file_once(self):
        proc = self.run_preview(
            ("--exclude", A_REL), ("--exclude", A_REL)
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["excluded_paths"], [A_REL])

    def test_no_exclusions_lists_empty_array_and_all_paths(self):
        proc = self.run_preview()
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 3)
        self.assertEqual(doc["paths"], sorted(DEMO_FILES))
        self.assertEqual(doc["excluded_paths"], [])

    def test_excluding_empty_directory_is_legal(self):
        (self.source / "emptydir").mkdir()
        proc = self.run_preview(("--exclude-dir", "emptydir"))
        doc = self.parse_preview(proc)
        # 目录本身不列入数组；没有普通文件被剔除。
        self.assertEqual(doc["excluded_paths"], [])
        self.assertEqual(
            doc["paths"], sorted(DEMO_FILES), "空目录本来就不在 paths 中"
        )

    def test_exclude_all_files_succeeds(self):
        proc = self.run_preview(
            ("--exclude", NOTE_REL), ("--exclude-dir", "cache")
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 0)
        self.assertEqual(doc["paths"], [])
        self.assertEqual(doc["excluded_paths"], [A_REL, B_REL, NOTE_REL])

    def test_unicode_spaces_and_case_preserved_sorted_by_codepoint(self):
        extra = {
            "Z.txt": b"z",
            "a.txt": b"a",
            "中文 目录/x 文件.bin": b"x",
            "中文 目录/y": b"y",
        }
        write_files(self.source, extra)
        proc = self.run_preview(("--exclude-dir", "中文 目录"))
        doc = self.parse_preview(proc)
        self.assertEqual(
            doc["excluded_paths"],
            sorted(["中文 目录/x 文件.bin", "中文 目录/y"]),
        )
        self.assertEqual(
            doc["paths"],
            sorted(set(DEMO_FILES) | {"Z.txt", "a.txt"}),
        )
        # JSON 逐字保留中文与空格（不转义为 \uXXXX）。
        self.assertIn("中文 目录/x 文件.bin", proc.stdout.decode("utf-8"))

    def test_checksum_combination_still_no_create(self):
        proc = self.run_preview(
            ("--exclude-dir", "cache"), checksum=True
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 1)
        self.assertEqual(doc["paths"], [NOTE_REL])
        self.assertEqual(doc["excluded_paths"], [A_REL, B_REL])
        self.assertFalse(os.path.lexists(self.snapshot))
        self.assertFalse(os.path.lexists(self.snapshot.parent))

    # ---- 不传新参数：预览字段保持原样 ----

    def test_without_flag_preview_has_only_original_fields(self):
        proc = self.run_preview(
            ("--exclude-dir", "cache"), show_excluded=False
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        line = proc.stdout[:-1]
        doc = json.loads(line.decode("utf-8"))
        self.assertEqual(
            set(doc.keys()), {"source", "snapshot", "files", "paths"}
        )
        self.assertEqual(doc["files"], 1)
        self.assertEqual(doc["paths"], [NOTE_REL])

    # ---- 组合错误：--show-excluded 未与 --dry-run 同用 ----

    def test_flag_without_dry_run_rejected_with_exit_2(self):
        source_before = capture_tree(self.source)
        proc = run_cmd(
            ["backup", str(self.source), str(self.snapshot),
             "--exclude-dir", "cache", "--show-excluded"]
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(
            REASON_FLAG_ONLY_WITH_DRY_RUN, proc.stderr.decode("utf-8")
        )
        self.assertFalse(os.path.lexists(self.snapshot))
        self.assertFalse(os.path.lexists(self.snapshot.parent))
        self.assertEqual(capture_tree(self.source), source_before)
        self.assertEqual(
            sorted(p.name for p in self.work.iterdir()), ["demo"]
        )

    def test_flag_without_dry_run_does_not_create_even_without_rules(self):
        proc = run_cmd(
            ["backup", str(self.source), str(self.snapshot),
             "--show-excluded"]
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(
            REASON_FLAG_ONLY_WITH_DRY_RUN, proc.stderr.decode("utf-8")
        )
        self.assertFalse(os.path.lexists(self.snapshot))

    # ---- 失败路径：沿用现有检查与原因 ----

    def assert_rejected_stdout_empty(self, proc, *reasons):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        stderr = proc.stderr.decode("utf-8")
        for reason in reasons:
            self.assertIn(reason, stderr)
        self.assertFalse(os.path.lexists(self.snapshot))
        self.assertFalse(os.path.lexists(self.snapshot.parent))

    def test_unmatched_exclude_still_rejected(self):
        proc = self.run_preview(("--exclude", "no-such.txt"))
        self.assert_rejected_stdout_empty(
            proc, REASON_EXCLUDE_UNMATCHED, "no-such.txt"
        )

    def test_file_inside_excluded_dir_unmatched_still_rejected(self):
        proc = self.run_preview(
            ("--exclude-dir", "cache"),
            ("--exclude", "cache/missing.bin"),
        )
        self.assert_rejected_stdout_empty(
            proc, REASON_EXCLUDE_UNMATCHED, "cache/missing.bin"
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "平台不支持符号链接")
    def test_excluded_area_still_safety_checked(self):
        os.symlink("a.bin", str(self.source / "cache" / "link"))
        source_before = capture_tree(self.source)
        proc = self.run_preview(("--exclude-dir", "cache"))
        self.assert_rejected_stdout_empty(
            proc, REASON_SYMLINK, "cache/link"
        )
        self.assertEqual(capture_tree(self.source), source_before)

    def test_snapshot_already_exists_rejected(self):
        self.snapshot.mkdir(parents=True)
        proc = self.run_preview(("--exclude-dir", "cache"))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("快照路径已存在", proc.stderr.decode("utf-8"))
        self.assertTrue(self.snapshot.is_dir(), "已有目标不得被改动")


if __name__ == "__main__":
    unittest.main()
