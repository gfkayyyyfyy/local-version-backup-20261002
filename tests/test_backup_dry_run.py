#!/usr/bin/env python3
"""backup --dry-run 备份预览的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum]
                            [--exclude PATH]... [--exclude-dir PATH]...
                            --dry-run
    python backup.py backup SOURCE SNAPSHOT [--checksum] ...

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。

夹具源目录（与验收 demo 一致）：

- ``note.txt``：内容为 ``old``（无结尾换行）；
- ``cache/tmp.txt``：内容为 ``temp``；
- ``cache-old/keep.txt``：空文件。

覆盖约定：

1. 预览成功：退出码 0、标准错误为空、标准输出恰好一行 JSON 对象加末尾
   换行，对象仅含 source、snapshot、files、paths；前两项为解析后的
   绝对路径，files 为收录文件数，paths 按 Unicode 码点升序。
2. 验收场景 ``--exclude-dir cache`` 预览不存在的同级目标：files 为 2、
   paths 为 ["cache-old/keep.txt","note.txt"]，目标及其父目录不创建。
3. 源文件未变时去掉 --dry-run 在同一目标备份：清单路径与预览一致，
   data 内文件字节与源文件一致，成功输出保持原样。
4. 空源目录、合法排除全部文件：预览成功，files 为 0、paths 为空数组。
5. 与 --exclude、重复/父子 --exclude-dir、--checksum 任意组合；
   被排除目录内的单文件排除仍按完整源目录匹配。
6. 预览不创建 SNAPSHOT 及其父目录、不生成清单或临时文件、不改动源目录。
7. 源目录不存在、源为符号链接、源内（含排除区域内）符号链接、目标已
   存在、目标位于源目录内：退出码 2、标准输出为空、标准错误沿用现有
   原因，且不创建目标。
8. 非法/未匹配的 --exclude 与 --exclude-dir：退出码 2，标准错误沿用
   “排除路径无效”/“排除项未匹配普通文件”/“排除目录路径无效”/
   “排除目录未匹配普通目录”及原始参数。
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

NOTE_REL = "note.txt"
TMP_REL = "cache/tmp.txt"
KEEP_REL = "cache-old/keep.txt"
CACHE_DIR_REL = "cache"

NOTE_BYTES = b"old"
TMP_BYTES = b"temp"
KEEP_BYTES = b""

DEMO_FILES = {
    NOTE_REL: NOTE_BYTES,
    TMP_REL: TMP_BYTES,
    KEEP_REL: KEEP_BYTES,
}

ERROR_PREFIX = "错误"
REASON_SOURCE_MISSING = "源目录不存在"
REASON_SOURCE_SYMLINK = "源目录自身是符号链接"
REASON_INSIDE_SYMLINK = "源目录中包含符号链接"
REASON_SNAPSHOT_EXISTS = "快照路径已存在"
REASON_SNAPSHOT_INSIDE = "快照目录不得位于源目录内"
REASON_EXCLUDE_INVALID = "排除路径无效"
REASON_EXCLUDE_UNMATCHED = "排除项未匹配普通文件"
REASON_DIR_INVALID = "排除目录路径无效"
REASON_DIR_UNMATCHED = "排除目录未匹配普通目录"


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
        self.snapshot = self.work / "snap"
        write_files(self.source, DEMO_FILES)

    def run_preview(self, snapshot=None, excludes=None, exclude_dirs=None,
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
            set(doc.keys()),
            {"source", "snapshot", "files", "paths"},
        )
        return doc

    # ---- 验收场景 ----

    def test_acceptance_exclude_dir_cache(self):
        """--exclude-dir cache：files=2，paths 为 keep 与 note，目标不存在。"""
        source_before = capture_tree(self.source)
        proc = self.run_preview(exclude_dirs=[CACHE_DIR_REL])
        doc = self.parse_preview(proc)

        self.assertEqual(doc["source"], str(self.source.resolve()))
        self.assertEqual(doc["snapshot"], str(self.snapshot.resolve()))
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], [KEEP_REL, NOTE_REL])

        self.assertFalse(
            os.path.lexists(self.snapshot), "预览不得创建快照目录"
        )
        # 父目录中只应有源目录，无任何快照或临时产物。
        self.assertEqual(
            sorted(p.name for p in self.work.iterdir()),
            ["source"],
        )
        self.assertEqual(capture_tree(self.source), source_before)

    def test_preview_paths_sorted_by_codepoint(self):
        """paths 按 Unicode 码点升序，与目录遍历顺序无关。"""
        proc = self.run_preview()
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 3)
        self.assertEqual(doc["paths"], sorted(doc["paths"]))
        self.assertEqual(doc["paths"], [KEEP_REL, TMP_REL, NOTE_REL])

    def test_preview_does_not_create_snapshot_parents(self):
        """快照位于尚不存在的多级父目录下：预览成功但父目录同样不创建。"""
        nested = self.work / "new" / "nested" / "snap"
        proc = self.run_preview(snapshot=nested)
        self.parse_preview(proc)
        self.assertFalse(os.path.lexists(nested))
        self.assertFalse(os.path.lexists(self.work / "new"))

    # ---- 预览后实际备份一致 ----

    def test_preview_then_real_backup_consistent(self):
        """去掉 --dry-run：清单路径与预览一致，字节与源文件一致。"""
        proc = self.run_preview(exclude_dirs=[CACHE_DIR_REL], checksum=True)
        doc = self.parse_preview(proc)
        self.assertFalse(os.path.lexists(self.snapshot))

        proc = run_cmd(
            ["backup", str(self.source), str(self.snapshot),
             "--checksum", "--exclude-dir", CACHE_DIR_REL]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stdout = proc.stdout.decode("utf-8")
        self.assertIn("已创建快照目录", stdout)
        self.assertIn("已备份文件数: 2", stdout)

        with open(self.snapshot / "manifest.json", "rb") as f:
            manifest = json.loads(f.read().decode("utf-8"))
        self.assertEqual(manifest["version"], 1)
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            doc["paths"],
        )
        for rel in doc["paths"]:
            data = (self.snapshot / "data" / rel).read_bytes()
            self.assertEqual(data, DEMO_FILES[rel])
        # 被排除的文件不进 data。
        self.assertFalse(
            (self.snapshot / "data" / TMP_REL).exists()
        )

    # ---- 空源目录与全部排除 ----

    def test_empty_source_preview(self):
        empty_source = self.work / "empty-source"
        empty_snapshot = self.work / "empty-snap"
        empty_source.mkdir()
        proc = run_cmd(
            ["backup", str(empty_source), str(empty_snapshot), "--dry-run"]
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 0)
        self.assertEqual(doc["paths"], [])
        self.assertFalse(os.path.lexists(empty_snapshot))

    def test_all_files_excluded_preview(self):
        proc = self.run_preview(
            excludes=[NOTE_REL, KEEP_REL], exclude_dirs=[CACHE_DIR_REL]
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 0)
        self.assertEqual(doc["paths"], [])
        self.assertFalse(os.path.lexists(self.snapshot))

    # ---- 排除组合 ----

    def test_exclude_file_inside_excluded_dir_matches(self):
        """被排除目录内的单文件排除仍按完整源目录匹配，不报错。"""
        proc = self.run_preview(
            excludes=[TMP_REL], exclude_dirs=[CACHE_DIR_REL]
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], [KEEP_REL, NOTE_REL])

    def test_duplicate_and_parent_child_dirs_union(self):
        """重复目录与父子目录重叠取并集；--checksum 不影响预览结果。"""
        proc = self.run_preview(
            exclude_dirs=[CACHE_DIR_REL, CACHE_DIR_REL, "cache"],
            checksum=True,
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], [KEEP_REL, NOTE_REL])
        self.assertFalse(os.path.lexists(self.snapshot))

    # ---- 失败：源目录与目标 ----

    def assert_rejected(self, proc, *reasons, target=None,
                        literal_args=()):
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        stderr = proc.stderr.decode("utf-8")
        self.assertIn(ERROR_PREFIX, stderr)
        for reason in reasons:
            self.assertIn(reason, stderr)
        for raw in literal_args:
            self.assertIn(raw, stderr)
        if target is not None:
            self.assertFalse(os.path.lexists(target))

    def test_reject_missing_source(self):
        proc = run_cmd(
            ["backup", str(self.work / "no-such"), str(self.snapshot),
             "--dry-run"]
        )
        self.assert_rejected(proc, REASON_SOURCE_MISSING, target=self.snapshot)

    @unittest.skipUnless(hasattr(os, "symlink"), "平台不支持符号链接")
    def test_reject_source_self_symlink(self):
        link_source = self.work / "source-link"
        os.symlink(str(self.source), str(link_source))
        proc = run_cmd(
            ["backup", str(link_source), str(self.snapshot), "--dry-run"]
        )
        self.assert_rejected(
            proc, REASON_SOURCE_SYMLINK, str(link_source),
            target=self.snapshot,
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "平台不支持符号链接")
    def test_reject_symlink_even_inside_excluded_dir(self):
        link_rel = "cache/link"
        os.symlink("tmp.txt", str(self.source / link_rel))
        proc = self.run_preview(exclude_dirs=[CACHE_DIR_REL])
        self.assert_rejected(
            proc, REASON_INSIDE_SYMLINK, link_rel, target=self.snapshot
        )

    def test_reject_existing_snapshot(self):
        self.snapshot.mkdir()
        proc = self.run_preview(exclude_dirs=[CACHE_DIR_REL])
        self.assert_rejected(proc, REASON_SNAPSHOT_EXISTS)
        # 已存在的目标不被改动。
        self.assertEqual(capture_tree(self.snapshot), {})

    def test_reject_snapshot_inside_source(self):
        inside = self.source / "inside"
        proc = self.run_preview(snapshot=inside)
        self.assert_rejected(proc, REASON_SNAPSHOT_INSIDE, target=inside)

    # ---- 失败：排除项 ----

    def test_reject_invalid_exclude(self):
        proc = self.run_preview(excludes=["../x"])
        self.assert_rejected(
            proc, REASON_EXCLUDE_INVALID, literal_args=["../x"],
            target=self.snapshot,
        )

    def test_reject_unmatched_exclude(self):
        proc = self.run_preview(excludes=["no-such.txt"])
        self.assert_rejected(
            proc, REASON_EXCLUDE_UNMATCHED, literal_args=["no-such.txt"],
            target=self.snapshot,
        )

    def test_reject_invalid_exclude_dir(self):
        proc = self.run_preview(exclude_dirs=["cache/"])
        self.assert_rejected(
            proc, REASON_DIR_INVALID, literal_args=["cache/"],
            target=self.snapshot,
        )

    def test_reject_unmatched_exclude_dir(self):
        proc = self.run_preview(exclude_dirs=[NOTE_REL])
        self.assert_rejected(
            proc, REASON_DIR_UNMATCHED, literal_args=[NOTE_REL],
            target=self.snapshot,
        )


if __name__ == "__main__":
    unittest.main()
