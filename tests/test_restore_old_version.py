#!/usr/bin/env python3
"""源文件改动后恢复旧版本的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数，
也不以内部函数返回值替代公开行为。仅依赖 Python 3 标准库；全部源目录、
快照与恢复目标在独立临时目录中运行时准备，相关父目录预先存在，
用例结束自动清理，不读写用户现有目录，不依赖符号链接权限、
网络或第三方包。

覆盖的回归约定：

1. 备份把三个普通文件（UTF-8 文本、含 0x00/0xFF 的二进制、零字节文件）
   的原始字节存入快照 data，清单恰好记录三个相对路径，且备份不改动源目录。
2. 备份之后改动源目录（改写文本、删除二进制、新增文件）不影响恢复结果：
   恢复只取决于已生成的快照，得到的是改动前的旧版本，新增文件不出现。
3. 恢复不改动编辑后的源目录，也不改动快照内容。
4. 向同一 DEST 再次恢复以退出码 2 拒绝覆盖，标准错误说明原因，
   标准输出不含成功摘要，目标中原有路径与字节保持不变。
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

# 强制子进程按 UTF-8 输出，断言不依赖运行环境的区域设置。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# 源目录初始内容：UTF-8 文本、四个字节的二进制、零字节文件。
OLD_NOTE = "旧版本\n".encode("utf-8")
BIN_BYTES = bytes([0x00, 0xFF, 0x10, 0x00])
SOURCE_FILES = {
    "note.txt": OLD_NOTE,
    "子目录/数据.bin": BIN_BYTES,
    "empty.txt": b"",
}

# 备份之后对源目录的改动：改写文本、删除二进制、新增文件。
NEW_NOTE = "新版本\n".encode("utf-8")
LATER_FILE = "later.txt"
LATER_BYTES = "后来新增".encode("utf-8")

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
BACKUP_SUMMARY_MARKERS = ("已创建快照目录", "已备份文件数")
RESTORE_SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"
REASON_OVERWRITE = "恢复目标已存在，拒绝覆盖"


def run_cmd(argv):
    """通过公开命令行执行 backup.py，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), *argv],
        cwd=str(ROOT),
        env=CHILD_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_files(base, files):
    """在 base 下按 ``相对POSIX路径 -> 字节`` 写入小文件。"""
    base = Path(base)
    for rel, data in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def capture_tree(root):
    """递归记录目录树：相对路径 -> (类型, 字节)，与遍历顺序无关。

    类型为 "dir" / "file"；普通文件记录完整字节。本测试只创建普通
    文件与目录，若意外出现其他类型会在此暴露为未知条目。
    """
    root = Path(root)
    tree = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_dir = Path(dirpath).relative_to(root)
        for name in dirnames:
            key = (rel_dir / name).as_posix()
            tree[key] = ("dir", None)
        for name in filenames:
            path = Path(dirpath) / name
            key = (rel_dir / name).as_posix()
            tree[key] = ("file", path.read_bytes())
    return tree


def file_tree(root):
    """递归记录普通文件：相对路径 -> 字节。"""
    return {
        key: value[1]
        for key, value in capture_tree(root).items()
        if value[0] == "file"
    }


def proc_context(label, proc):
    """把一次公开命令调用的可观察结果整理成失败信息上下文。"""
    return (
        f"操作: {label}\n"
        f"exit={proc.returncode}\n"
        f"stdout={proc.stdout.decode('utf-8', errors='replace')!r}\n"
        f"stderr={proc.stderr.decode('utf-8', errors='replace')!r}"
    )


class RestoreOldVersionTests(unittest.TestCase):
    """备份后改动源目录，恢复结果仍等于快照生成时的旧版本。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-old-version-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"
        self.dest = self.work / "dest"
        write_files(self.source, SOURCE_FILES)
        self.source_initial = capture_tree(self.source)

    # ---- 流程步骤（全部通过公开命令入口）----

    def run_backup(self):
        """执行 backup 并做成功断言，返回 CompletedProcess。"""
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        context = proc_context("backup SOURCE SNAPSHOT", proc)

        self.assertEqual(proc.returncode, 0, f"backup 应返回 0\n{context}")
        self.assertEqual(
            proc.stderr, b"", f"backup 成功时标准错误应为空\n{context}",
        )
        stdout = proc.stdout.decode("utf-8")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"backup 标准输出应包含新建快照目录的绝对路径 "
            f"{self.snapshot.resolve()}\n{context}",
        )
        self.assertIn(
            "已备份文件数: 3", stdout,
            f"backup 标准输出应包含文件数 3\n{context}",
        )
        return proc

    def edit_source(self):
        """仅在演示源目录制造改动：改写文本、删除二进制、新增文件。"""
        (self.source / "note.txt").write_bytes(NEW_NOTE)
        (self.source / "子目录" / "数据.bin").unlink()
        (self.source / LATER_FILE).write_bytes(LATER_BYTES)
        self.source_edited = capture_tree(self.source)

    def run_restore(self):
        """执行 restore 并做成功断言，返回 CompletedProcess。"""
        proc = run_cmd(["restore", str(self.snapshot), str(self.dest)])
        context = proc_context("restore SNAPSHOT DEST", proc)

        self.assertEqual(proc.returncode, 0, f"restore 应返回 0\n{context}")
        self.assertEqual(
            proc.stderr, b"", f"restore 成功时标准错误应为空\n{context}",
        )
        stdout = proc.stdout.decode("utf-8")
        self.assertIn(
            str(self.dest.resolve()), stdout,
            f"restore 标准输出应包含新建恢复目录的绝对路径 "
            f"{self.dest.resolve()}\n{context}",
        )
        self.assertIn(
            "已恢复文件数: 3", stdout,
            f"restore 标准输出应包含文件数 3\n{context}",
        )
        return proc

    def backup_and_restore_with_edited_source(self):
        """完整流程：备份 -> 改动源目录 -> 恢复到独立新目录。"""
        self.run_backup()
        self.snapshot_before_restore = capture_tree(self.snapshot)
        self.edit_source()
        self.run_restore()

    # ---- 用例 ----

    def test_backup_stores_exact_bytes_and_manifest(self):
        """备份：data 字节与准备数据一致，清单恰好三个相对路径，源目录不变。"""
        self.run_backup()

        # data 下的相对路径与字节逐一等于准备数据。
        self.assertEqual(
            file_tree(self.snapshot / "data"), SOURCE_FILES,
            "快照 data 中的相对路径或字节与准备数据不一致\n"
            f"快照目录: {self.snapshot}\n"
            f"预期: {sorted(SOURCE_FILES)}\n"
            f"实际: {sorted(file_tree(self.snapshot / 'data'))}",
        )
        # 逐个文件核对字节，失败信息指向具体路径。
        for rel, expected in SOURCE_FILES.items():
            actual = (self.snapshot / "data" / rel).read_bytes()
            self.assertEqual(
                actual, expected,
                f"快照数据字节不一致: {self.snapshot / 'data' / rel}\n"
                f"预期: {expected!r}\n实际: {actual!r}",
            )

        # 清单恰好记录三个相对路径，版本保持 1。
        manifest_path = self.snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest.get("version"), 1,
            f"清单版本应为 1: {manifest_path}",
        )
        self.assertEqual(
            [entry.get("path") for entry in manifest.get("files", [])],
            sorted(SOURCE_FILES),
            f"清单应恰好记录三个相对路径: {manifest_path}\n"
            f"预期: {sorted(SOURCE_FILES)}\n"
            f"实际: {manifest.get('files')}",
        )

        # 备份不改动初始源数据。
        self.assertEqual(
            capture_tree(self.source), self.source_initial,
            f"备份后源目录内容发生变化: {self.source}",
        )

    def test_restore_recovers_old_version_after_source_changes(self):
        """源目录改动后恢复：恰好得到旧版本三个文件，新增文件不出现。"""
        self.backup_and_restore_with_edited_source()

        # 恢复结果与初始准备数据逐字节一致（只取决于快照）。
        self.assertEqual(
            file_tree(self.dest), SOURCE_FILES,
            "恢复结果的相对路径或字节与旧版本不一致\n"
            f"恢复目录: {self.dest}\n"
            f"预期: {sorted(SOURCE_FILES)}\n"
            f"实际: {sorted(file_tree(self.dest))}",
        )
        note_path = self.dest / "note.txt"
        self.assertEqual(
            note_path.read_bytes(), OLD_NOTE,
            f"文本应恢复为旧版本: {note_path}\n"
            f"预期: {OLD_NOTE!r}\n实际: {note_path.read_bytes()!r}",
        )
        bin_path = self.dest / "子目录" / "数据.bin"
        self.assertEqual(
            bin_path.read_bytes(), BIN_BYTES,
            f"二进制字节应保持原样: {bin_path}\n"
            f"预期: {BIN_BYTES!r}\n实际: {bin_path.read_bytes()!r}",
        )
        empty_path = self.dest / "empty.txt"
        self.assertTrue(empty_path.is_file(), f"空文件应被恢复: {empty_path}")
        self.assertEqual(
            empty_path.read_bytes(), b"",
            f"空文件应保持零字节: {empty_path}",
        )
        self.assertFalse(
            os.path.lexists(self.dest / LATER_FILE),
            f"备份后新增的 {LATER_FILE} 不应出现在恢复目录: {self.dest}",
        )

    def test_restore_leaves_edited_source_and_snapshot_untouched(self):
        """恢复不改动编辑后的源目录，也不改动快照内容。"""
        self.backup_and_restore_with_edited_source()

        self.assertEqual(
            capture_tree(self.source), self.source_edited,
            f"恢复后编辑过的源目录内容发生变化: {self.source}",
        )
        self.assertEqual(
            capture_tree(self.snapshot), self.snapshot_before_restore,
            f"恢复后快照内容发生变化: {self.snapshot}",
        )

    def test_second_restore_to_same_dest_rejected(self):
        """再次向同一 DEST 恢复：退出码 2，拒绝覆盖，目标内容不变。"""
        self.backup_and_restore_with_edited_source()
        dest_before = capture_tree(self.dest)

        proc = run_cmd(["restore", str(self.snapshot), str(self.dest)])
        context = proc_context("再次 restore SNAPSHOT DEST（目标已存在）", proc)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")

        self.assertEqual(proc.returncode, 2, f"重复恢复应返回 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        self.assertIn(
            REASON_OVERWRITE, stderr,
            f"标准错误应说明目标已存在并拒绝覆盖\n{context}",
        )
        for marker in RESTORE_SUMMARY_MARKERS:
            self.assertNotIn(
                marker, stdout,
                f"拒绝覆盖时标准输出不应含恢复成功摘要\n{context}",
            )
        self.assertEqual(
            capture_tree(self.dest), dest_before,
            f"拒绝覆盖后目标中的路径或字节发生变化: {self.dest}",
        )
        # 目标中原有内容仍是旧版本。
        self.assertEqual(
            (self.dest / "note.txt").read_bytes(), OLD_NOTE,
            f"拒绝覆盖后目标中的文本被改动: {self.dest / 'note.txt'}",
        )


if __name__ == "__main__":
    unittest.main()
