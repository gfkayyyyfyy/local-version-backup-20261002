#!/usr/bin/env python3
"""源文件改动后恢复旧版本的端到端回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部函数。
仅依赖 Python 3 标准库；全部目录在临时目录中运行时准备，用例结束
自动清理，不读写用户现有目录，也不依赖符号链接、网络或第三方包。

覆盖的回归约定：

1. 备份生成的新快照与源数据逐字节一致，清单恰好记录三个相对路径，
   且备份本身不改动源目录。
2. 备份之后源目录被编辑（改写、删除、新增文件）时，恢复结果只取决
   于已生成的快照：恰好还原旧的三个文件，不读取源目录现状，也不
   改动编辑后的源目录与快照内容。
3. 向已存在的恢复目标再次恢复时以退出码 2 拒绝覆盖，目标保持原样。
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

# 初始源目录：UTF-8 文本、含零字节的四字节二进制、零字节空文件。
INITIAL_FILES = {
    "note.txt": "旧版本\n".encode("utf-8"),
    "子目录/数据.bin": bytes([0x00, 0xFF, 0x10, 0x00]),
    "empty.txt": b"",
}

# 备份后对源目录的编辑内容。
EDITED_NOTE = "新版本\n".encode("utf-8")
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


def file_tree(root):
    """递归记录目录下的普通文件：相对 POSIX 路径 -> 完整字节。"""
    root = Path(root)
    tree = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            path = Path(dirpath) / name
            tree[(rel_dir / name).as_posix()] = path.read_bytes()
    return tree


class RestoreOldVersionTests(unittest.TestCase):
    """备份后源目录被改动，恢复仍应精确还原快照生成时的旧版本。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-old-version-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"
        self.dest = self.work / "dest"
        write_files(self.source, INITIAL_FILES)

    def test_restore_recovers_snapshot_state_after_source_changes(self):
        # ---- 第一步：备份初始源目录到源目录之外的新快照 ----
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"操作: backup {self.source} {self.snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"backup 应返回 0\n{context}")
        self.assertEqual(stderr, "", f"backup 成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"backup 标准输出应包含新建快照目录的绝对路径\n{context}",
        )
        self.assertIn(
            "已备份文件数: 3", stdout,
            f"backup 标准输出应包含文件数 3\n{context}",
        )

        # data 下对应文件的字节与准备数据完全一致。
        self.assertEqual(
            file_tree(self.snapshot / "data"), INITIAL_FILES,
            "快照 data 中的相对路径或字节与准备的源数据不一致\n"
            f"预期: {sorted(INITIAL_FILES)}\n"
            f"实际: {sorted(file_tree(self.snapshot / 'data'))}",
        )

        # 清单为版本 1，且恰好记录三个相对路径。
        manifest = json.loads(
            (self.snapshot / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest.get("version"), 1, "清单版本应为 1")
        self.assertEqual(
            [entry.get("path") for entry in manifest.get("files", [])],
            sorted(INITIAL_FILES),
            "清单应恰好按序记录三个源文件相对路径",
        )

        # 备份不改动初始源数据。
        self.assertEqual(
            file_tree(self.source), INITIAL_FILES,
            "backup 之后源目录内容发生变化",
        )

        # 记录快照现场，用于之后证明 restore 不改动快照。
        snapshot_before_restore = file_tree(self.snapshot)

        # ---- 第二步：仅在演示源目录中改动文件 ----
        (self.source / "note.txt").write_bytes(EDITED_NOTE)
        os.remove(self.source / "子目录" / "数据.bin")
        (self.source / LATER_FILE).write_bytes(LATER_BYTES)
        edited_source = file_tree(self.source)
        self.assertEqual(
            edited_source,
            {"note.txt": EDITED_NOTE, "empty.txt": b"", LATER_FILE: LATER_BYTES},
            "测试准备失败：编辑后的源目录内容不符合预期",
        )

        # ---- 第三步：从快照恢复到独立的新目录 ----
        proc = run_cmd(["restore", str(self.snapshot), str(self.dest)])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"操作: restore {self.snapshot} {self.dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"restore 应返回 0\n{context}")
        self.assertEqual(stderr, "", f"restore 成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.dest.resolve()), stdout,
            f"restore 标准输出应包含新建恢复目录的绝对路径\n{context}",
        )
        self.assertIn(
            "已恢复文件数: 3", stdout,
            f"restore 标准输出应包含文件数 3\n{context}",
        )

        # 恢复结果恰好是快照生成时的三个旧文件，与源目录现状无关。
        restored = file_tree(self.dest)
        self.assertEqual(
            restored, INITIAL_FILES,
            "恢复结果应恰好为快照中的三个旧文件\n"
            f"预期路径: {sorted(INITIAL_FILES)}\n"
            f"实际路径: {sorted(restored)}",
        )
        self.assertEqual(
            restored["note.txt"], "旧版本\n".encode("utf-8"),
            f"{self.dest / 'note.txt'} 应恢复为旧版本文本",
        )
        self.assertEqual(
            restored["子目录/数据.bin"], bytes([0x00, 0xFF, 0x10, 0x00]),
            f"{self.dest / '子目录' / '数据.bin'} 的二进制字节应保持原样",
        )
        self.assertEqual(
            restored["empty.txt"], b"",
            f"{self.dest / 'empty.txt'} 应为零字节文件",
        )
        self.assertNotIn(
            LATER_FILE, restored,
            f"备份后新增的 {LATER_FILE} 不应出现在恢复结果中",
        )

        # 恢复不改动编辑后的源目录，也不改动快照内容。
        self.assertEqual(
            file_tree(self.source), edited_source,
            "restore 之后编辑过的源目录内容发生变化",
        )
        self.assertEqual(
            file_tree(self.snapshot), snapshot_before_restore,
            "restore 之后快照内容发生变化",
        )

        # ---- 第四步：再次向同一 DEST 恢复，应拒绝覆盖 ----
        dest_before_retry = file_tree(self.dest)
        proc = run_cmd(["restore", str(self.snapshot), str(self.dest)])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"操作: restore {self.snapshot} {self.dest}（目标已存在）\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"重复恢复应返回 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        self.assertIn(
            REASON_OVERWRITE, stderr,
            f"标准错误缺少拒绝原因“{REASON_OVERWRITE}”\n{context}",
        )
        for marker in RESTORE_SUMMARY_MARKERS:
            self.assertNotIn(
                marker, stdout,
                f"拒绝覆盖时标准输出不应出现恢复成功摘要\n{context}",
            )
        self.assertEqual(
            file_tree(self.dest), dest_before_retry,
            "拒绝覆盖后恢复目标中的路径或字节发生变化",
        )


if __name__ == "__main__":
    unittest.main()
