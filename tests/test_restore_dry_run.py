#!/usr/bin/env python3
"""restore --dry-run 恢复预览的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH]... --dry-run
    python backup.py restore SNAPSHOT DEST [--file PATH]...

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。

夹具快照由公开 backup 命令从两个普通文件生成：

- ``note.txt``：UTF-8 文本“旧内容”；
- ``子目录/空文件.bin``：空文件。

覆盖约定：

1. 预览成功：退出码 0、标准错误为空、标准输出恰好一行 JSON 对象加末尾
   换行，对象仅含 snapshot、destination、files、paths；前两项为解析后的
   绝对路径，files 为选择文件数，paths 按 Unicode 码点升序。
2. 重复选择同一相对路径只计一次；不指定 --file 时选择全部文件；paths
   顺序不受清单排列与 --file 参数顺序影响。
3. 空快照预览 files 为 0、paths 为空数组。
4. 预览不创建 DEST（及父目录）、不改动快照；data/ 中的额外文件不纳入。
5. 实际恢复（不带 --dry-run）在同一目标上随后成功并还原旧字节，保持
   原有成功输出。
6. 数据缺失（含未选中条目）、摘要不一致、非法清单路径、选择不存在的
   条目、目标已存在、目标位于快照目录内：预览退出码 2、标准输出为空、
   标准错误沿用现有原因，且不创建目标。
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
EMPTY_REL = "子目录/空文件.bin"
NOTE_BYTES = "旧内容".encode("utf-8")

BACKUP_FILES = {NOTE_REL: NOTE_BYTES, EMPTY_REL: b""}

ERROR_PREFIX = "错误"
REASON_NOT_IN_MANIFEST = "选择的路径未在快照清单中"
REASON_DATA_MISSING = "清单引用的数据缺失"
REASON_CHECKSUM_MISMATCH = "摘要校验不一致"


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


class RestoreDryRunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-dryrun-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

        write_files(self.source, BACKUP_FILES)
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

    def run_dry_run(self, dest, selections=None):
        argv = ["restore", str(self.snapshot), str(dest), "--dry-run"]
        if selections is not None:
            for sel in selections:
                argv.extend(["--file", sel])
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
            {"snapshot", "destination", "files", "paths"},
        )
        return doc

    def test_duplicate_selection_listed_once_and_dest_absent(self):
        dest = self.work / "restored"
        proc = self.run_dry_run(dest, [NOTE_REL, NOTE_REL])
        doc = self.parse_preview(proc)

        self.assertEqual(doc["snapshot"], str(self.snapshot.resolve()))
        self.assertEqual(doc["destination"], str(dest.resolve()))
        self.assertEqual(doc["files"], 1)
        self.assertEqual(doc["paths"], [NOTE_REL])

        self.assertFalse(
            os.path.lexists(dest), "预览不得创建目标目录"
        )
        # 父目录中不应出现目标或其他临时产物。
        self.assertEqual(
            sorted(p.name for p in self.work.iterdir()),
            ["snapshot", "source"],
        )

    def test_full_selection_sorted_by_codepoint(self):
        dest = self.work / "restored-all"
        # 逆序传入，paths 仍按 Unicode 码点升序。
        proc = self.run_dry_run(dest, [EMPTY_REL, NOTE_REL])
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], [NOTE_REL, EMPTY_REL])
        self.assertFalse(os.path.lexists(dest))

    def test_no_file_selects_everything(self):
        dest = self.work / "restored-default"
        proc = self.run_dry_run(dest)
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], [NOTE_REL, EMPTY_REL])
        self.assertFalse(os.path.lexists(dest))

    def test_empty_snapshot_preview(self):
        empty_source = self.work / "empty-source"
        empty_snapshot = self.work / "empty-snapshot"
        empty_dest = self.work / "empty-dest"
        empty_source.mkdir()
        proc = run_cmd(["backup", str(empty_source), str(empty_snapshot)])
        self.assertEqual(proc.returncode, 0, proc.stderr)

        proc = run_cmd(
            ["restore", str(empty_snapshot), str(empty_dest), "--dry-run"]
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 0)
        self.assertEqual(doc["paths"], [])
        self.assertFalse(os.path.lexists(empty_dest))

    def test_extra_data_file_not_listed(self):
        extra = self.snapshot / "data" / "extra.txt"
        extra.write_text("清单之外\n", encoding="utf-8")
        dest = self.work / "restored-extra"
        proc = self.run_dry_run(dest)
        doc = self.parse_preview(proc)
        self.assertEqual(doc["paths"], [NOTE_REL, EMPTY_REL])
        self.assertNotIn("extra.txt", doc["paths"])

    def test_preview_then_real_restore(self):
        """预览后去掉 --dry-run：实际恢复生成旧内容，输出保持原样。"""
        dest = self.work / "restored"
        proc = self.run_dry_run(dest, [NOTE_REL, NOTE_REL])
        self.parse_preview(proc)
        self.assertFalse(os.path.lexists(dest))

        proc = run_cmd(["restore", str(self.snapshot), str(dest)])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stdout = proc.stdout.decode("utf-8")
        self.assertIn("已创建恢复目录", stdout)
        self.assertIn("已恢复文件数: 2", stdout)
        self.assertEqual((dest / NOTE_REL).read_bytes(), NOTE_BYTES)
        self.assertEqual((dest / EMPTY_REL).read_bytes(), b"")

    def test_reject_unselected_data_missing(self):
        missing = self.snapshot / "data" / EMPTY_REL
        missing.unlink()
        dest = self.work / "restored-missing"
        proc = self.run_dry_run(dest, [NOTE_REL])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        stderr = proc.stderr.decode("utf-8")
        self.assertIn(ERROR_PREFIX, stderr)
        self.assertIn(REASON_DATA_MISSING, stderr)
        self.assertIn(EMPTY_REL, stderr)
        self.assertFalse(os.path.lexists(dest))

    def test_reject_unknown_selection(self):
        dest = self.work / "restored-unknown"
        proc = self.run_dry_run(dest, ["no-such.txt"])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn(
            REASON_NOT_IN_MANIFEST, proc.stderr.decode("utf-8")
        )
        self.assertFalse(os.path.lexists(dest))

    def test_reject_when_dest_exists(self):
        dest = self.work / "existing"
        dest.mkdir()
        proc = self.run_dry_run(dest)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("恢复目标已存在", proc.stderr.decode("utf-8"))

    def test_reject_dest_within_snapshot(self):
        dest = self.snapshot / "inside"
        proc = self.run_dry_run(dest)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("恢复目录不得位于快照目录内", proc.stderr.decode("utf-8"))
        self.assertFalse(os.path.lexists(dest))

    def test_reject_checksum_mismatch(self):
        ck_source = self.work / "ck-source"
        ck_snapshot = self.work / "ck-snapshot"
        ck_source.mkdir()
        (ck_source / "a.txt").write_text("aaa\n", encoding="utf-8")
        proc = run_cmd(
            ["backup", "--checksum", str(ck_source), str(ck_snapshot)]
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        (ck_snapshot / "data" / "a.txt").write_text("bbb\n", encoding="utf-8")

        dest = self.work / "ck-dest"
        proc = run_cmd(
            ["restore", str(ck_snapshot), str(dest), "--dry-run"]
        )
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        stderr = proc.stderr.decode("utf-8")
        self.assertIn(REASON_CHECKSUM_MISMATCH, stderr)
        self.assertIn("a.txt", stderr)
        self.assertFalse(os.path.lexists(dest))


if __name__ == "__main__":
    unittest.main()
