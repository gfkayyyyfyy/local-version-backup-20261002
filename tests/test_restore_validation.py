#!/usr/bin/env python3
"""restore 写入前整体拒绝行为的可执行回归测试。

验收方式完全基于 README 公开的命令行接口：

    python backup.py restore SNAPSHOT DEST

即通过子进程运行项目根目录下的 backup.py，观察退出码、标准输出、
标准错误以及磁盘上的文件结果；不直接调用任何内部校验函数。

每个用例使用独立的临时目录，快照目录与恢复目标为互不包含的兄弟目录，
且恢复目标事先不存在。仅依赖 Python 3 标准库与运行时本地构造的小文件。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = PROJECT_ROOT / "backup.py"

NOTE_REL = "note.txt"
BIN_REL = "嵌套 目录/二进制 文件.bin"
NOTE_BYTES = "这是恢复说明文件的内容。\n".encode("utf-8")

# 公开的成功摘要片段；任何拒绝用例的标准输出都不得出现。
SUCCESS_DIR_LINE = "已创建恢复目录:"
SUCCESS_COUNT_LINE = "已恢复文件数:"

# 各拒绝原因在标准错误中应出现的错误类别片段（取自面向用户的公开提示）。
REASON_JSON_BROKEN = "JSON 损坏"
REASON_BAD_VERSION = "不支持的清单版本"
REASON_BAD_VERSION_TYPE = "version 字段类型不符"
REASON_BAD_FILES_TYPE = "files 字段类型不符"
REASON_EMPTY_PATH = "路径必须是非空字符串"
REASON_DUPLICATE_PATH = "重复文件路径"
REASON_PARENT_COMPONENT = "上级目录分量"
REASON_ABSOLUTE_PATH = "绝对路径"
REASON_DATA_MISSING = "数据缺失"
REASON_DATA_NOT_FILE = "不是普通文件"


def _run_restore(snapshot, dest):
    """通过 README 公开命令执行恢复，返回 CompletedProcess。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), "restore",
         str(snapshot), str(dest)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def _run_backup(source, snapshot):
    """通过公开 backup 命令构造正常的版本 1 快照。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), "backup",
         str(source), str(snapshot)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def _record_tree(root):
    """记录目录树下全部相对文件（POSIX 风格路径 -> 字节）与目录集合。"""
    files = {}
    dirs = set()
    for current, dirnames, filenames in os.walk(root):
        cur_path = Path(current)
        for name in dirnames:
            dirs.add(str((cur_path / name).relative_to(root)))
        for name in filenames:
            fpath = cur_path / name
            rel = fpath.relative_to(root).as_posix()
            with open(fpath, "rb") as f:
                files[rel] = f.read()
    return files, dirs


class RestoreValidationTests(unittest.TestCase):
    """恢复前校验的黑盒回归测试。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore_test_")
        self.base = Path(self._tmp.name)
        self.source = self.base / "source"
        self.snapshot = self.base / "snapshot"
        self.dest = self.base / "restored"

        # 运行时构造源目录：普通文本文件 + 含零字节文件的嵌套目录。
        nested = self.source / Path(BIN_REL).parent
        nested.mkdir(parents=True)
        (self.source / NOTE_REL).write_bytes(NOTE_BYTES)
        (self.source / BIN_REL).write_bytes(b"")

        # 通过公开 backup 命令得到正常版本 1 快照。
        created = _run_backup(self.source, self.snapshot)
        self.assertEqual(
            created.returncode, 0,
            f"测试前置：构造快照失败，stderr={created.stderr!r}",
        )

        # 恢复目标与快照为兄弟目录，互不包含，且事先不存在。
        self.assertFalse(os.path.lexists(self.dest))

    def tearDown(self):
        self._tmp.cleanup()

    # ---- 辅助方法 ----

    def _manifest_path(self):
        return self.snapshot / "manifest.json"

    def _write_manifest(self, doc):
        with open(self._manifest_path(), "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)

    def _valid_manifest(self):
        with open(self._manifest_path(), "r", encoding="utf-8") as f:
            return json.load(f)

    def _assert_restore_rejected(
        self, reason, label, expect_extra_unchanged_tree=False, marker=None
    ):
        """统一断言：恢复被整体拒绝，且没有任何写入副作用。"""
        # 调用前快照状态与（越界用例的）整个临时目录状态。
        snap_files_before, snap_dirs_before = _record_tree(self.snapshot)
        if expect_extra_unchanged_tree:
            base_files_before, base_dirs_before = _record_tree(self.base)

        result = _run_restore(self.snapshot, self.dest)
        decoded_err = result.stderr.decode("utf-8", errors="replace")
        decoded_out = result.stdout.decode("utf-8", errors="replace")

        # 退出码统一为 2。
        self.assertEqual(
            result.returncode, 2,
            f"[{label}] 期望退出码 2，实际 {result.returncode}，"
            f"stdout={decoded_out!r} stderr={decoded_err!r}",
        )
        # 标准错误包含错误提示与对应的拒绝原因类别。
        self.assertIn("错误", decoded_err, f"[{label}] stderr 缺少错误提示")
        self.assertIn(
            reason, decoded_err,
            f"[{label}] stderr 缺少拒绝原因 {reason!r}: {decoded_err!r}",
        )
        # 标准输出不得出现恢复成功摘要。
        self.assertNotIn(
            SUCCESS_DIR_LINE, decoded_out,
            f"[{label}] 拒绝时 stdout 出现成功目录摘要: {decoded_out!r}",
        )
        self.assertNotIn(
            SUCCESS_COUNT_LINE, decoded_out,
            f"[{label}] 拒绝时 stdout 出现成功文件数摘要: {decoded_out!r}",
        )
        # 恢复目标仍不存在（先前的有效条目也不得被恢复出来）。
        self.assertFalse(
            os.path.lexists(self.dest),
            f"[{label}] 拒绝后恢复目标竟然存在: {self.dest}",
        )

        # 调用前后快照文件集合与内容完全相同。
        snap_files_after, snap_dirs_after = _record_tree(self.snapshot)
        self.assertEqual(
            snap_files_before, snap_files_after,
            f"[{label}] 拒绝前后快照文件集合/内容发生变化",
        )
        self.assertEqual(
            snap_dirs_before, snap_dirs_after,
            f"[{label}] 拒绝前后快照目录集合发生变化",
        )

        if expect_extra_unchanged_tree:
            # 越界用例：快照外不得生成任何文件/目录，标记文件字节不变。
            base_files_after, base_dirs_after = _record_tree(self.base)
            self.assertEqual(
                base_files_before, base_files_after,
                f"[{label}] 拒绝后临时目录树出现越界写入或文件改动",
            )
            self.assertEqual(
                base_dirs_before, base_dirs_after,
                f"[{label}] 拒绝后临时目录树出现新增目录",
            )
            if marker is not None:
                self.assertEqual(
                    marker.read_bytes(), MARKER_BYTES,
                    f"[{label}] 快照外标记文件字节发生变化",
                )

    # ---- 正常对照 ----

    def test_restore_valid_v1_snapshot_succeeds(self):
        """正常版本 1 快照恢复成功：退出码 0，输出与字节均正确。"""
        result = _run_restore(self.snapshot, self.dest)
        decoded_out = result.stdout.decode("utf-8", errors="replace")
        decoded_err = result.stderr.decode("utf-8", errors="replace")

        self.assertEqual(result.returncode, 0)
        self.assertEqual(decoded_err, "")

        # 输出目标的绝对路径与文件数 2（与具体临时目录名无关，动态比较）。
        self.assertIn(
            f"{SUCCESS_DIR_LINE} {self.dest.resolve()}", decoded_out
        )
        self.assertIn(f"{SUCCESS_COUNT_LINE} 2", decoded_out)

        # 恢复后的相对路径集合与字节内容与快照一致（含零字节文件）。
        restored_files, _ = _record_tree(self.dest)
        self.assertEqual(
            set(restored_files), {NOTE_REL, BIN_REL}
        )
        self.assertEqual(restored_files[NOTE_REL], NOTE_BYTES)
        self.assertEqual(restored_files[BIN_REL], b"")

        snap_files, _ = _record_tree(self.snapshot)
        self.assertEqual(
            restored_files,
            {rel: snap_files["data/" + rel]
             for rel in (NOTE_REL, BIN_REL)},
        )

    # ---- 拒绝用例：每种只引入一种无效条件 ----

    def test_restore_rejects_corrupt_json(self):
        with open(self._manifest_path(), "wb") as f:
            f.write(b"{ this is not valid json")
        self._assert_restore_rejected(
            REASON_JSON_BROKEN, "损坏 JSON")

    def test_restore_rejects_version_2(self):
        doc = self._valid_manifest()
        doc["version"] = 2
        self._write_manifest(doc)
        self._assert_restore_rejected(
            REASON_BAD_VERSION, "version 为 2")

    def test_restore_rejects_version_true(self):
        doc = self._valid_manifest()
        doc["version"] = True
        self._write_manifest(doc)
        self._assert_restore_rejected(
            REASON_BAD_VERSION_TYPE, "version 为 true")

    def test_restore_rejects_files_not_array(self):
        doc = self._valid_manifest()
        doc["files"] = NOTE_REL
        self._write_manifest(doc)
        self._assert_restore_rejected(
            REASON_BAD_FILES_TYPE, "files 不是数组")

    def test_restore_rejects_empty_path(self):
        # 先含一个有效文件，再含空路径条目，证明先前文件也不会落盘。
        doc = self._valid_manifest()
        doc["files"] = [
            {"path": NOTE_REL},
            {"path": ""},
        ]
        self._write_manifest(doc)
        self._assert_restore_rejected(
            REASON_EMPTY_PATH, "path 为空字符串")

    def test_restore_rejects_duplicate_path(self):
        doc = self._valid_manifest()
        doc["files"] = [
            {"path": NOTE_REL},
            {"path": NOTE_REL},
        ]
        self._write_manifest(doc)
        self._assert_restore_rejected(
            REASON_DUPLICATE_PATH, "重复文件路径")

    def test_restore_rejects_parent_component(self):
        # ../../marker.bin 相对数据目录解析后指向快照外的标记文件。
        marker = self.base / "marker.bin"
        marker.write_bytes(MARKER_BYTES)
        doc = self._valid_manifest()
        doc["files"] = [
            {"path": NOTE_REL},
            {"path": "../../" + marker.name},
        ]
        self._write_manifest(doc)
        self._assert_restore_rejected(
            REASON_PARENT_COMPONENT, "包含 .. 分量",
            expect_extra_unchanged_tree=True, marker=marker,
        )

    def test_restore_rejects_absolute_path(self):
        # 绝对路径（唯一无效条件），指向快照外标记文件。
        marker = self.base / "marker.bin"
        marker.write_bytes(MARKER_BYTES)
        doc = self._valid_manifest()
        doc["files"] = [
            {"path": NOTE_REL},
            {"path": str(marker)},
        ]
        self._write_manifest(doc)
        self._assert_restore_rejected(
            REASON_ABSOLUTE_PATH, "以 / 开头的绝对路径",
            expect_extra_unchanged_tree=True, marker=marker,
        )

    def test_restore_rejects_missing_referenced_file(self):
        # 先含一个有效文件，再引用缺失数据，证明不会留下先前文件。
        doc = self._valid_manifest()
        doc["files"] = [
            {"path": NOTE_REL},
            {"path": "missing.txt"},
        ]
        self._write_manifest(doc)
        self.assertFalse((self.snapshot / "data" / "missing.txt").exists())
        self._assert_restore_rejected(
            REASON_DATA_MISSING, "引用文件缺失")

    def test_restore_rejects_referenced_directory(self):
        # 先含一个有效文件，再引用实际为目录的数据条目。
        target_dir = self.snapshot / "data" / "adir"
        target_dir.mkdir()
        doc = self._valid_manifest()
        doc["files"] = [
            {"path": NOTE_REL},
            {"path": "adir"},
        ]
        self._write_manifest(doc)
        self._assert_restore_rejected(
            REASON_DATA_NOT_FILE, "引用实际为目录")


MARKER_BYTES = b"\x00\x01marker-content\xff\xfe"

if __name__ == "__main__":
    unittest.main()
