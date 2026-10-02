#!/usr/bin/env python3
"""restore 目标路径安全边界的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py restore SNAPSHOT DEST

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部快照与目录在临时目录中运行时准备，
用例结束自动清理，不读写用户现有目录，也不依赖网络或额外安装包。

覆盖两项既有约定：

1. 拒绝覆盖：恢复目标已存在（普通文件、含标记文件的目录、指向真实目录的
   符号链接、指向不存在路径的悬空符号链接）时以退出码 2 拒绝，既有目标的
   类型、相对路径与字节保持原样，悬空链接指向的路径不会被创建。
2. 禁止向快照内恢复：目标（直接子目录，或含 ``..`` 分量解析后）落入快照
   目录时同样以退出码 2 拒绝，目标始终不被创建，快照清单与数据字节不变。

另含一个正常对照：同级、名称前缀相近的 ``snapshot-copy`` 目标不得被
误判为位于快照目录内，恢复应完整成功。

所有失败用例同时核对源目录与快照没有新增、覆盖或删除。
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

# 源目录固定内容：含旧版文本的 note.txt 与嵌套中文目录下含零字节（0x00）
# 的二进制文件。
SOURCE_FILES = {
    "note.txt": "旧版笔记内容\n历史第二行\n".encode("utf-8"),
    "嵌套 目录/二进制 文件.bin": bytes(range(256)),
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
    """restore 命令对目标路径的两项安全约定：拒绝覆盖、禁止快照内恢复。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-target-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)

        # 用公开 backup 命令从运行时准备的源目录生成版本 1 快照；
        # 源目录保留在原地，用于失败用例核对“源目录无改动”。
        self.source = self.work / "source"
        write_files(self.source, SOURCE_FILES)
        self.snapshot = self.work / "snapshot"
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

    # ---- 通用断言 ----

    def run_restore(self, dest):
        """以公开入口执行 restore，目标可传入带 ``..`` 分量的原始路径。"""
        return run_cmd(["restore", str(self.snapshot), str(dest)])

    def assert_workspace_unchanged(self, work_before, context):
        """比较调用前后整个临时工作区：相对路径、条目类型、普通文件字节。

        源目录、快照（清单与数据）及既有目标的任何新增、覆盖或删除
        都会在此暴露。
        """
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"失败后临时工作区出现新增、覆盖或删除\n{context}",
        )

    def assert_rejected(self, label, proc, reason, work_before):
        """失败用例的公共断言：退出码、标准错误原因、无成功摘要、现场不变。"""
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        self.assertIn(reason, stderr, f"标准错误缺少拒绝原因“{reason}”\n{context}")
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        self.assert_workspace_unchanged(work_before, context)
        return stdout, stderr

    # ---- 拒绝覆盖：恢复目标已存在 ----

    def test_reject_existing_regular_file(self):
        """目标已是普通文件：拒绝覆盖，文件类型、路径与字节不变。"""
        dest = self.work / "restored"
        dest.write_bytes(b"existing file, do not touch\n")
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        self.assert_rejected("目标是普通文件", proc, REASON_OVERWRITE, work_before)
        self.assertTrue(dest.is_file() and not dest.is_symlink(),
                        "既有普通文件的类型被改动")
        self.assertEqual(
            dest.read_bytes(), b"existing file, do not touch\n",
            "既有普通文件的内容被改动",
        )

    def test_reject_existing_directory_with_marker(self):
        """目标已是含标记文件的目录：拒绝覆盖，目录及其内容不变。"""
        dest = self.work / "restored"
        write_files(dest, {"marker.txt": b"marker inside existing dir\n"})
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        self.assert_rejected("目标是含标记文件的目录", proc, REASON_OVERWRITE, work_before)
        self.assertTrue(dest.is_dir() and not dest.is_symlink(),
                        "既有目录的类型被改动")
        self.assertEqual(
            (dest / "marker.txt").read_bytes(), b"marker inside existing dir\n",
            "既有目录中的标记文件被改动",
        )

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_existing_symlink_to_directory(self):
        """目标是指向真实目录的有效符号链接：拒绝覆盖，链接与被指向内容都不变。"""
        real_dir = self.work / "real_target"
        write_files(real_dir, {"marker.txt": b"behind symlink, do not touch\n"})
        dest = self.work / "restored"
        os.symlink(real_dir, dest, target_is_directory=True)
        link_before = os.readlink(dest)
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        self.assert_rejected("目标是有效符号链接", proc, REASON_OVERWRITE, work_before)
        self.assertTrue(dest.is_symlink(), "失败后符号链接本身被替换")
        self.assertEqual(os.readlink(dest), link_before, "符号链接目标被改动")
        self.assertEqual(
            (real_dir / "marker.txt").read_bytes(), b"behind symlink, do not touch\n",
            "符号链接指向的文件被改动",
        )

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_existing_dangling_symlink(self):
        """目标是指向不存在路径的悬空符号链接：同样拒绝覆盖，链接不变。"""
        missing = self.work / "nonexistent"
        dest = self.work / "restored"
        os.symlink(missing, dest)
        link_before = os.readlink(dest)
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        self.assert_rejected("目标是悬空符号链接", proc, REASON_OVERWRITE, work_before)
        self.assertTrue(dest.is_symlink(), "失败后悬空符号链接本身被替换")
        self.assertEqual(os.readlink(dest), link_before, "符号链接目标被改动")
        self.assertFalse(
            os.path.lexists(missing),
            "失败后悬空链接指向的路径被创建",
        )

    # ---- 禁止向快照目录内恢复 ----

    def assert_rejected_inside_snapshot(self, label, proc, dest, work_before):
        """快照内目标用例的公共断言：拒绝原因 + 目标始终不存在。"""
        self.assert_rejected(label, proc, REASON_INSIDE_SNAPSHOT, work_before)
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后快照内目标仍被创建: {dest}",
        )

    def test_reject_direct_child_of_snapshot(self):
        """目标是快照内尚不存在的直接子目录：拒绝且目标始终不存在。"""
        dest = self.snapshot / "restored"
        self.assertFalse(os.path.lexists(dest))
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        self.assert_rejected_inside_snapshot(
            "快照目录直接子目录", proc, dest, work_before,
        )

    def test_reject_dotdot_path_resolving_into_snapshot(self):
        """目标含 .. 分量、解析后仍落入快照目录：与字面快照内路径同样拒绝。"""
        subdir = self.snapshot / "sub"
        subdir.mkdir()
        # 字面上先下后上，解析结果 snapshot/restored 仍位于快照目录内。
        dest = subdir / ".." / "restored"
        resolved = self.snapshot / "restored"
        self.assertFalse(os.path.lexists(dest))
        self.assertFalse(os.path.lexists(resolved))
        work_before = capture_tree(self.work)

        proc = self.run_restore(dest)
        self.assert_rejected_inside_snapshot(
            "含 .. 分量解析后落入快照目录", proc, resolved, work_before,
        )

    # ---- 正常对照：前缀相近的同级目录不是快照内路径 ----

    def test_success_sibling_with_similar_name_prefix(self):
        """目标 snapshot-copy 与快照 snapshot 同级且前缀相近：恢复成功。"""
        dest = self.work / "snapshot-copy"
        self.assertFalse(os.path.lexists(dest))
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest)
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
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

        # 恢复结果仅含清单中的两个文件，且逐字节等于快照数据。
        self.assertEqual(
            file_tree(dest), file_tree(self.snapshot / "data"),
            "恢复结果的相对路径集合或字节与快照数据不一致",
        )
        self.assertEqual(
            (dest / "note.txt").read_bytes(), SOURCE_FILES["note.txt"],
            "note.txt 的字节与源文件不一致",
        )
        bin_bytes = (dest / "嵌套 目录" / "二进制 文件.bin").read_bytes()
        self.assertEqual(
            bin_bytes, SOURCE_FILES["嵌套 目录/二进制 文件.bin"],
            "嵌套二进制文件的字节与源文件不一致",
        )
        self.assertIn(0, bin_bytes, "二进制文件应保留零字节")

        # 成功后源目录与快照（清单与数据）内容不变。
        self.assertEqual(
            capture_tree(self.source), source_before,
            "恢复成功后源目录内容发生变化",
        )
        self.assertEqual(
            capture_tree(self.snapshot), snapshot_before,
            "恢复成功后快照清单或数据发生变化",
        )


if __name__ == "__main__":
    unittest.main()
