#!/usr/bin/env python3
"""restore 目标路径安全边界的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中
运行时准备，用例结束自动清理，不读写用户现有目录，也不依赖网络或
额外安装包；不支持符号链接的环境显式跳过符号链接用例，不要求提权。

夹具快照由公开 backup 命令从两个普通文件生成：含旧版文本的
``note.txt`` 与嵌套中文目录下含零字节（0x00）的二进制文件。

覆盖两项既有约定：

1. 拒绝覆盖：DEST 已存在（普通文件、含标记文件的目录、指向真实目录
   的符号链接、指向不存在路径的悬空符号链接）时以退出码 2 拒绝，
   标准错误说明恢复目标已存在，原目标的类型、相对路径与字节不变。
2. 禁止恢复进快照目录：DEST 尚不存在但解析后位于快照内（快照的直接
   子目录，或含 ``..`` 分量解析后仍在快照内）时同样以退出码 2 拒绝，
   目标始终不存在，快照清单与数据字节保持原样。

另含一个正常对照：与快照同级、名称前缀相近的 ``snapshot-copy`` 目录
不得被误判为位于快照目录内，恢复应完整成功且恰好得到清单中的两个文件。
"""

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

# 源目录固定内容：含旧版文本的普通文件，与嵌套中文目录下含零字节的二进制文件。
OLD_NOTE_BYTES = "这是旧版文本\n第二行旧内容\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x7F, 0x00, 0x80])
SOURCE_FILES = {
    "note.txt": OLD_NOTE_BYTES,
    "嵌套目录/二进制 文件.bin": BINARY_BYTES,
}

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"

REASON_OVERWRITE = "恢复目标已存在，拒绝覆盖"
REASON_INSIDE_SNAPSHOT = "恢复目录不得位于快照目录内"


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
    """递归记录目录树：相对路径 -> (类型, 字节或链接目标)，与遍历顺序无关。

    类型为 "dir" / "file" / "symlink"；普通文件记录完整字节，符号链接
    记录链接目标字符串（不跟随链接，也不进入链接指向的目录）。
    """
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


def file_tree(root):
    """递归记录普通文件：相对路径 -> 字节。"""
    return {
        key: value[1]
        for key, value in capture_tree(root).items()
        if value[0] == "file"
    }


def _symlink_supported():
    """探测当前环境是否允许创建符号链接（Windows 上常需特权）。"""
    with tempfile.TemporaryDirectory(prefix="symlink-probe-") as probe:
        try:
            os.symlink("target", Path(probe) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINK_SUPPORTED = _symlink_supported()
SYMLINK_SKIP_REASON = "当前环境不支持创建符号链接，跳过该用例"


class RestoreTargetSafetyTests(unittest.TestCase):
    """restore 命令对目标路径的两项安全约定：拒绝覆盖、禁止恢复进快照。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-target-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        write_files(self.source, SOURCE_FILES)
        self.snapshot = self.work / "snapshot"

        # 夹具快照必须由公开 backup 命令生成；失败则直接中止本用例。
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

    # ---- 通用断言 ----

    def run_restore(self, dest):
        """以公开入口执行 restore，dest 可传入带 ``..`` 分量的原始路径。"""
        return run_cmd(["restore", str(self.snapshot), str(dest)])

    def assert_workspace_unchanged(self, work_before, context):
        """比较调用前后整个临时工作区：相对路径、条目类型、普通文件字节。

        源目录与快照都在工作区内，任何新增、覆盖或删除都会在此暴露。
        """
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"失败后临时工作区（含源目录与快照）出现新增、覆盖或删除\n{context}",
        )

    def assert_rejected(self, label, dest, proc, reason, work_before):
        """失败用例的公共断言：退出码、标准错误原因、无成功摘要、现场不变。"""
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n"
            f"输入目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        self.assertIn(reason, stderr, f"标准错误缺少拒绝原因“{reason}”\n{context}")
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        self.assert_workspace_unchanged(work_before, context)
        return stdout, stderr, context

    # ---- 拒绝覆盖：DEST 已存在 ----

    def test_reject_existing_regular_file(self):
        """DEST 已是普通文件：拒绝覆盖，类型、相对路径与字节不变。"""
        dest = self.work / "restored"
        original_bytes = b"existing regular file, do not touch\n"
        dest.write_bytes(original_bytes)
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        _, _, context = self.assert_rejected(
            "DEST 是普通文件", dest, proc, REASON_OVERWRITE, work_before,
        )

        self.assertTrue(
            dest.is_file() and not dest.is_symlink(),
            f"既有普通文件的类型被改变\n{context}",
        )
        self.assertEqual(
            dest.read_bytes(), original_bytes,
            f"既有普通文件的字节被改动\n{context}",
        )

    def test_reject_existing_directory_with_marker(self):
        """DEST 已是含标记文件的目录：拒绝覆盖，目录与标记字节不变。"""
        dest = self.work / "restored"
        marker_rel = "marker.txt"
        marker_bytes = b"marker inside existing dest dir\n"
        write_files(dest, {marker_rel: marker_bytes})
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        _, _, context = self.assert_rejected(
            "DEST 是含标记文件的目录", dest, proc, REASON_OVERWRITE, work_before,
        )

        self.assertTrue(
            dest.is_dir() and not dest.is_symlink(),
            f"既有目录的类型被改变\n{context}",
        )
        self.assertEqual(
            (dest / marker_rel).read_bytes(), marker_bytes,
            f"既有目录内标记文件的字节被改动\n{context}",
        )

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_existing_symlink_to_directory(self):
        """DEST 是指向真实目录的符号链接：拒绝覆盖，链接与被指向内容都不变。"""
        real_dir = self.work / "real_dest"
        marker_bytes = b"behind symlink, do not touch\n"
        write_files(real_dir, {"marker.txt": marker_bytes})
        dest = self.work / "restored"
        os.symlink(real_dir, dest, target_is_directory=True)
        link_before = os.readlink(dest)
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        _, _, context = self.assert_rejected(
            "DEST 是指向真实目录的符号链接", dest, proc,
            REASON_OVERWRITE, work_before,
        )

        self.assertTrue(dest.is_symlink(), f"失败后符号链接本身被替换\n{context}")
        self.assertEqual(
            os.readlink(dest), link_before,
            f"符号链接目标被改动\n{context}",
        )
        self.assertEqual(
            (real_dir / "marker.txt").read_bytes(), marker_bytes,
            f"符号链接指向的目录内容被改动\n{context}",
        )

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_existing_dangling_symlink(self):
        """DEST 是指向不存在路径的悬空符号链接：同样拒绝，链接不变且目标不被创建。"""
        missing = self.work / "nonexistent_target"
        dest = self.work / "restored"
        os.symlink(missing, dest)
        link_before = os.readlink(dest)
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        _, _, context = self.assert_rejected(
            "DEST 是悬空符号链接", dest, proc, REASON_OVERWRITE, work_before,
        )

        self.assertTrue(dest.is_symlink(), f"失败后悬空符号链接本身被替换\n{context}")
        self.assertEqual(
            os.readlink(dest), link_before,
            f"悬空符号链接目标被改动\n{context}",
        )
        self.assertFalse(
            os.path.lexists(missing),
            f"失败后悬空链接指向的路径被创建: {missing}\n{context}",
        )

    # ---- 禁止恢复进快照目录：DEST 尚不存在但解析后位于快照内 ----

    def test_reject_nonexistent_direct_child_of_snapshot(self):
        """DEST 是快照内尚不存在的直接子目录：拒绝且目标始终不存在。"""
        dest = self.snapshot / "inside-restored"
        self.assertFalse(os.path.lexists(dest))
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        _, _, context = self.assert_rejected(
            "DEST 是快照直接子目录", dest, proc,
            REASON_INSIDE_SNAPSHOT, work_before,
        )

        self.assertFalse(
            os.path.lexists(dest),
            f"失败后快照内目标仍被创建: {dest}\n{context}",
        )

    def test_reject_nonexistent_dotdot_path_resolving_into_snapshot(self):
        """DEST 含 .. 分量、解析后仍位于快照内：与字面上的快照内路径同样拒绝。"""
        # snapshot/data 事先存在；字面上先下后上，解析结果为 snapshot/inside-restored。
        dest = self.snapshot / "data" / ".." / "inside-restored"
        resolved = self.snapshot / "inside-restored"
        self.assertTrue((self.snapshot / "data").is_dir())
        self.assertFalse(os.path.lexists(dest))
        self.assertFalse(os.path.lexists(resolved))
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        _, _, context = self.assert_rejected(
            "DEST 含 .. 分量解析后落入快照", dest, proc,
            REASON_INSIDE_SNAPSHOT, work_before,
        )

        self.assertFalse(
            os.path.lexists(resolved),
            f"失败后解析出的快照内目标仍被创建: {resolved}\n{context}",
        )
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后字面上的目标路径被创建: {dest}\n{context}",
        )

    # ---- 正常对照：前缀相近的同级目录不是快照内路径 ----

    def test_success_sibling_with_similar_name_prefix(self):
        """snapshot-copy 与快照同级且前缀相近：恢复成功，恰好得到清单中的两个文件。"""
        dest = self.work / "snapshot-copy"
        self.assertFalse(os.path.lexists(dest))
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest)
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"输入目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"正常对照应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(dest.resolve()), stdout,
            f"标准输出应包含恢复目标绝对路径\n{context}",
        )
        self.assertIn(
            "已恢复文件数: 2", stdout,
            f"标准输出应包含文件数 2\n{context}",
        )

        # 恢复结果只含清单中的两个文件，相对路径与字节逐一等于快照数据。
        expected = file_tree(self.snapshot / "data")
        restored = file_tree(dest)
        self.assertEqual(
            set(restored), set(expected),
            f"恢复后的相对路径集合与快照清单不一致\n{context}",
        )
        self.assertEqual(
            len(restored), 2,
            f"恢复结果应恰好含两个文件\n{context}",
        )
        for rel, data in expected.items():
            self.assertEqual(
                restored[rel], data,
                f"恢复文件字节与快照数据不一致: {rel}\n{context}",
            )

        # 显式固定公开语义：旧版文本与含零字节的嵌套中文路径文件逐字节保留。
        self.assertEqual(
            (dest / "note.txt").read_bytes(), OLD_NOTE_BYTES,
            f"note.txt 未按快照旧版字节恢复\n{context}",
        )
        bin_path = dest / "嵌套目录" / "二进制 文件.bin"
        bin_bytes = bin_path.read_bytes()
        self.assertEqual(
            bin_bytes, BINARY_BYTES,
            f"嵌套二进制文件未按快照字节恢复\n{context}",
        )
        self.assertIn(0, bin_bytes, f"二进制文件应保留零字节\n{context}")

        # 源目录与快照在成功恢复后均保持原样。
        self.assertEqual(
            file_tree(self.source), SOURCE_FILES,
            f"恢复成功后源目录内容发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(self.snapshot), snapshot_before,
            f"恢复成功后快照内容发生变化\n{context}",
        )


if __name__ == "__main__":
    unittest.main()
