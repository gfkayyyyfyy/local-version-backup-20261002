#!/usr/bin/env python3
"""backup --dry-run 备份预览的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum] [--exclude PATH]...
                            [--exclude-dir PATH]... --dry-run
    python backup.py backup SOURCE SNAPSHOT [--checksum] [--exclude PATH]...
                            [--exclude-dir PATH]...

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。

验收演示目录：

- ``note.txt``：内容为 ``old``；
- ``cache/tmp.txt``：内容为 ``temp``；
- ``cache-old/keep.txt``：空文件。

覆盖约定：

1. 预览成功：退出码 0、标准错误为空、标准输出恰好一行 JSON 对象加末尾
   换行，对象仅含 source、snapshot、files、paths；前两项为解析后的
   绝对路径，files 为收录文件数，paths 按 Unicode 码点升序。
2. 验收场景 ``--exclude-dir cache`` 预览不存在的同级目标：files 为 2、
   paths 为 ["cache-old/keep.txt", "note.txt"]，目标及其父目录仍不存在，
   源目录保持原样。
3. 空源目录或合法排除全部文件时预览仍成功：files 为 0、paths 为空数组。
4. 源文件未变时去掉 --dry-run 做实际备份：清单路径与预览一致，文件字节
   与源文件一致，成功输出保持原样；--checksum 可与预览同用且不读内容。
5. 源目录不存在、不是目录、内部含符号链接或非普通文件（含被排除区域）、
   目标已存在（文件/目录/符号链接）、目标位于源目录内、非法或未匹配的
   排除值：预览退出码 2、标准输出为空、标准错误沿用现有原因，且不创建
   任何目标。
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
TMP_REL = "cache/tmp.txt"
KEEP_REL = "cache-old/keep.txt"

NOTE_BYTES = b"old"
TMP_BYTES = b"temp"
KEEP_BYTES = b""

DEMO_FILES = {
    NOTE_REL: NOTE_BYTES,
    TMP_REL: TMP_BYTES,
    KEEP_REL: KEEP_BYTES,
}

ERROR_PREFIX = "错误"
REASON_EXCLUDE_INVALID = "排除路径无效"
REASON_EXCLUDE_UNMATCHED = "排除项未匹配普通文件"
REASON_DIR_INVALID = "排除目录路径无效"
REASON_DIR_UNMATCHED = "排除目录未匹配普通目录"
REASON_SYMLINK = "源目录中包含符号链接"
REASON_NOT_REGULAR = "源目录中包含非普通文件"
REASON_SNAPSHOT_EXISTS = "快照路径已存在"
REASON_SNAPSHOT_INSIDE = "快照目录不得位于源目录内"

BACKUP_SUMMARY_MARKERS = ("已创建快照目录", "已备份文件数")


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


class BackupDryRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-dryrun-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        # 目标的父目录同样故意不存在，用于核对预览不创建父目录。
        self.snapshot = self.work / "nested" / "snap"

        write_files(self.source, DEMO_FILES)

    def run_preview(self, snapshot=None, exclude_dirs=None, excludes=None,
                    checksum=False):
        argv = ["backup", str(self.source), str(snapshot or self.snapshot)]
        if checksum:
            argv.append("--checksum")
        for raw in excludes or []:
            argv.extend(["--exclude", raw])
        for raw in exclude_dirs or []:
            argv.extend(["--exclude-dir", raw])
        argv.append("--dry-run")
        return run_cmd(argv)

    def parse_preview(self, proc):
        """成功预览的标准输出必须是单行 JSON 对象加末尾换行。"""
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        self.assertTrue(
            proc.stdout.endswith(b"\n"),
            f"预览输出应以换行结束: {proc.stdout!r}",
        )
        line = proc.stdout[:-1]
        self.assertNotIn(b"\n", line, "预览输出应只有一行")
        doc = json.loads(line.decode("utf-8"))
        self.assertEqual(
            set(doc.keys()), {"source", "snapshot", "files", "paths"}
        )
        return doc

    # ---- 验收场景 ----

    def test_acceptance_exclude_cache_preview(self):
        source_before = capture_tree(self.source)
        proc = self.run_preview(exclude_dirs=["cache"])
        doc = self.parse_preview(proc)

        self.assertEqual(doc["source"], str(self.source.resolve()))
        self.assertEqual(doc["snapshot"], str(self.snapshot.resolve()))
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], [KEEP_REL, NOTE_REL])

        # 快照目标及其父目录均不得被创建，源目录保持原样。
        self.assertFalse(os.path.lexists(self.snapshot))
        self.assertFalse(os.path.lexists(self.snapshot.parent))
        self.assertEqual(capture_tree(self.source), source_before)
        # 工作区中仍只有源目录，没有任何预览副产物。
        self.assertEqual(sorted(p.name for p in self.work.iterdir()),
                         ["source"])

    def test_preview_without_exclusions_lists_all_sorted(self):
        proc = self.run_preview()
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 3)
        self.assertEqual(doc["paths"],
                         sorted([NOTE_REL, TMP_REL, KEEP_REL]))
        self.assertFalse(os.path.lexists(self.snapshot))

    def test_preview_checksum_combination_does_not_read_or_create(self):
        """--checksum 与预览同用：输出一致、不读内容、不创建任何目标。"""
        proc = self.run_preview(exclude_dirs=["cache"], checksum=True)
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], [KEEP_REL, NOTE_REL])
        self.assertFalse(os.path.lexists(self.snapshot))

    def test_empty_source_preview(self):
        empty_source = self.work / "empty-source"
        empty_snapshot = self.work / "empty-nested" / "snap"
        empty_source.mkdir()
        proc = run_cmd(
            ["backup", str(empty_source), str(empty_snapshot), "--dry-run"]
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["source"], str(empty_source.resolve()))
        self.assertEqual(doc["snapshot"], str(empty_snapshot.resolve()))
        self.assertEqual(doc["files"], 0)
        self.assertEqual(doc["paths"], [])
        self.assertFalse(os.path.lexists(empty_snapshot))
        self.assertFalse(os.path.lexists(empty_snapshot.parent))

    def test_all_files_excluded_preview_succeeds_empty(self):
        proc = self.run_preview(
            exclude_dirs=["cache", "cache-old"], excludes=[NOTE_REL]
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 0)
        self.assertEqual(doc["paths"], [])
        self.assertFalse(os.path.lexists(self.snapshot))

    def test_exclude_file_inside_excluded_dir_still_matches(self):
        """排除目录内的单文件排除仍按完整源目录匹配，预览不报错。"""
        proc = self.run_preview(exclude_dirs=["cache"], excludes=[TMP_REL])
        doc = self.parse_preview(proc)
        self.assertEqual(doc["paths"], [KEEP_REL, NOTE_REL])

    def test_duplicate_and_parent_child_dir_union(self):
        """重复排除与父子目录重叠取并集，计数不重复。"""
        write_files(self.source, {"cache/sub/deep.txt": b"deep"})
        proc = self.run_preview(
            exclude_dirs=["cache", "cache", "cache/sub"]
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], [KEEP_REL, NOTE_REL])

    # ---- 预览后实际备份：清单与预览一致，字节与源一致 ----

    def test_preview_then_real_backup_matches(self):
        snapshot = self.work / "snap"
        proc = self.run_preview(snapshot=snapshot, exclude_dirs=["cache"])
        doc = self.parse_preview(proc)
        self.assertFalse(os.path.lexists(snapshot))

        proc = run_cmd(
            ["backup", str(self.source), str(snapshot),
             "--exclude-dir", "cache"]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stdout = proc.stdout.decode("utf-8")
        self.assertIn("已创建快照目录", stdout)
        self.assertIn(f"已备份文件数: {doc['files']}", stdout)
        self.assertEqual(proc.stderr, b"")

        manifest_path = snapshot / "manifest.json"
        with open(manifest_path, "rb") as f:
            manifest = json.loads(f.read().decode("utf-8"))
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(
            [item["path"] for item in manifest["files"]], doc["paths"]
        )
        self.assertEqual(
            set(manifest.keys()), {"version", "files"},
            "实际备份清单不应新增任何字段",
        )
        for rel in doc["paths"]:
            data_file = snapshot / "data" / rel
            self.assertEqual(
                data_file.read_bytes(), DEMO_FILES[rel],
                f"备份字节应与源文件一致: {rel}",
            )
        self.assertFalse(
            (snapshot / "data" / "cache").exists(),
            "被排除的 cache 不应出现在快照 data 中",
        )

    def test_preview_then_real_backup_with_checksum(self):
        snapshot = self.work / "snap-ck"
        proc = self.run_preview(
            snapshot=snapshot, exclude_dirs=["cache"], checksum=True
        )
        doc = self.parse_preview(proc)

        proc = run_cmd(
            ["backup", str(self.source), str(snapshot),
             "--checksum", "--exclude-dir", "cache"]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(snapshot / "manifest.json", "rb") as f:
            manifest = json.loads(f.read().decode("utf-8"))
        self.assertEqual(
            [item["path"] for item in manifest["files"]], doc["paths"]
        )
        for item in manifest["files"]:
            self.assertEqual(set(item.keys()), {"path", "sha256"})

    # ---- 失败路径：退出码 2、标准输出为空、不创建任何目标 ----

    def assert_rejected(self, proc, reasons, raw_args=(), target_absent=True):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        stderr = proc.stderr.decode("utf-8")
        self.assertIn(ERROR_PREFIX, stderr)
        for reason in reasons:
            self.assertIn(reason, stderr)
        for raw in raw_args:
            if raw != "":
                self.assertIn(raw, stderr)
        if target_absent:
            self.assertFalse(os.path.lexists(self.snapshot))
            self.assertFalse(os.path.lexists(self.snapshot.parent))

    def test_reject_missing_source(self):
        missing = self.work / "no-such-source"
        proc = run_cmd(
            ["backup", str(missing), str(self.snapshot), "--dry-run"]
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("源目录不存在", proc.stderr.decode("utf-8"))
        self.assertFalse(os.path.lexists(self.snapshot))

    def test_reject_source_not_directory(self):
        file_source = self.work / "source-file"
        file_source.write_bytes(b"x")
        proc = run_cmd(
            ["backup", str(file_source), str(self.snapshot), "--dry-run"]
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("源路径不是目录", proc.stderr.decode("utf-8"))
        self.assertFalse(os.path.lexists(self.snapshot))

    @unittest.skipUnless(hasattr(os, "symlink"), "平台不支持符号链接")
    def test_reject_symlink_inside_excluded_area(self):
        """排除区域仍参与安全检查：cache 内的符号链接照样拒绝。"""
        os.symlink("tmp.txt", str(self.source / "cache" / "link"))
        source_before = capture_tree(self.source)
        proc = self.run_preview(exclude_dirs=["cache"])
        self.assert_rejected(proc, [REASON_SYMLINK, "cache/link"])
        self.assertEqual(capture_tree(self.source), source_before)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "平台不支持 FIFO")
    def test_reject_non_regular_file_in_source(self):
        os.mkfifo(str(self.source / "fifo"))
        proc = self.run_preview()
        self.assert_rejected(proc, [REASON_NOT_REGULAR, "fifo"])

    def test_reject_snapshot_existing_directory(self):
        self.snapshot.mkdir(parents=True)
        proc = self.run_preview(exclude_dirs=["cache"])
        self.assert_rejected(
            proc, [REASON_SNAPSHOT_EXISTS], target_absent=False
        )

    def test_reject_snapshot_existing_file(self):
        self.snapshot.parent.mkdir(parents=True)
        self.snapshot.write_bytes(b"x")
        proc = self.run_preview()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(REASON_SNAPSHOT_EXISTS, proc.stderr.decode("utf-8"))
        # 已有的目标文件不得被改动。
        self.assertEqual(self.snapshot.read_bytes(), b"x")

    @unittest.skipUnless(hasattr(os, "symlink"), "平台不支持符号链接")
    def test_reject_snapshot_existing_symlink(self):
        self.snapshot.parent.mkdir(parents=True)
        os.symlink("somewhere", str(self.snapshot))
        proc = self.run_preview()
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(REASON_SNAPSHOT_EXISTS, proc.stderr.decode("utf-8"))
        self.assertTrue(os.path.islink(self.snapshot))

    def test_reject_snapshot_within_source(self):
        inside = self.source / "snap-inside"
        proc = self.run_preview(snapshot=inside, exclude_dirs=["cache"])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(REASON_SNAPSHOT_INSIDE, proc.stderr.decode("utf-8"))
        self.assertFalse(os.path.lexists(inside))

    def test_reject_invalid_exclude_value(self):
        proc = self.run_preview(excludes=["../note.txt"])
        self.assert_rejected(
            proc, [REASON_EXCLUDE_INVALID], ["../note.txt"]
        )

    def test_reject_unmatched_exclude_value(self):
        proc = self.run_preview(excludes=["no-such.txt"])
        self.assert_rejected(
            proc, [REASON_EXCLUDE_UNMATCHED], ["no-such.txt"]
        )

    def test_reject_exclude_pointing_to_directory(self):
        """--exclude 指向目录：未匹配普通文件。"""
        proc = self.run_preview(excludes=["cache"])
        self.assert_rejected(proc, [REASON_EXCLUDE_UNMATCHED], ["cache"])

    def test_reject_invalid_exclude_dir_value(self):
        proc = self.run_preview(exclude_dirs=["cache/"])
        self.assert_rejected(proc, [REASON_DIR_INVALID], ["cache/"])

    def test_reject_unmatched_exclude_dir(self):
        proc = self.run_preview(exclude_dirs=["no-such-dir"])
        self.assert_rejected(
            proc, [REASON_DIR_UNMATCHED], ["no-such-dir"]
        )

    def test_failure_output_has_no_success_markers(self):
        proc = self.run_preview(excludes=["no-such.txt"])
        stdout = proc.stdout.decode("utf-8")
        for marker in BACKUP_SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout)


if __name__ == "__main__":
    unittest.main()
