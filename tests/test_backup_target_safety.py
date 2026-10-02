#!/usr/bin/env python3
"""backup 目标路径安全边界的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录与目标在临时目录中运行时准备，
用例结束自动清理，不读写用户现有目录，也不依赖网络或额外安装包。

覆盖两项既有约定：

1. 拒绝覆盖：目标已存在（普通文件、含标记文件的目录、有效符号链接、
   指向不存在路径的符号链接）时以退出码 2 拒绝，既有目标保持原样。
2. 禁止向源目录内备份：目标（含 ``..`` 分量解析后）落入源目录时
   同样以退出码 2 拒绝，且目标始终不被创建。

另含一个正常对照：同级、名称前缀相近的 ``source-copy`` 目标不得被
误判为位于源目录内，备份应完整成功。
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

# 源目录固定内容：文本文件与嵌套中文目录下含零字节（0x00）的二进制文件。
SOURCE_FILES = {
    "note.txt": "笔记内容\n第二行\n".encode("utf-8"),
    "嵌套 目录/二进制 文件.bin": bytes(range(256)),
}

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建快照目录", "已备份文件数")
ERROR_PREFIX = "错误"

REASON_OVERWRITE = "快照路径已存在，拒绝覆盖"
REASON_INSIDE_SOURCE = "快照目录不得位于源目录内"


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


class BackupTargetSafetyTests(unittest.TestCase):
    """backup 命令对目标路径的两项安全约定：拒绝覆盖、禁止源内备份。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-target-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        write_files(self.source, SOURCE_FILES)
        self.source_before = capture_tree(self.source)

    # ---- 通用断言 ----

    def run_backup(self, target):
        """以公开入口执行 backup，目标可传入带 ``..`` 分量的原始路径。"""
        return run_cmd(["backup", str(self.source), str(target)])

    def assert_workspace_unchanged(self, work_before, context):
        """比较调用前后整个临时工作区：相对路径、条目类型、普通文件字节。

        任何新增文件、覆盖或删除（包括源目录与既有目标在内）都会在此暴露。
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

    # ---- 拒绝覆盖：目标已存在 ----

    def test_reject_existing_regular_file(self):
        """目标已是普通文件：拒绝覆盖，文件字节不变。"""
        target = self.work / "snapshot"
        target.write_bytes(b"existing file, do not touch\n")
        work_before = capture_tree(self.work)

        proc = self.run_backup(target)
        self.assert_rejected("目标是普通文件", proc, REASON_OVERWRITE, work_before)
        self.assertEqual(
            target.read_bytes(), b"existing file, do not touch\n",
            "既有普通文件的内容被改动",
        )

    def test_reject_existing_directory_with_marker(self):
        """目标已是含标记文件的目录：拒绝覆盖，目录及其内容不变。"""
        target = self.work / "snapshot"
        write_files(target, {"marker.txt": b"marker inside existing dir\n"})
        work_before = capture_tree(self.work)

        proc = self.run_backup(target)
        self.assert_rejected("目标是含标记文件的目录", proc, REASON_OVERWRITE, work_before)
        self.assertEqual(
            (target / "marker.txt").read_bytes(), b"marker inside existing dir\n",
            "既有目录中的标记文件被改动",
        )

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_existing_symlink_to_directory(self):
        """目标是指向真实目录的有效符号链接：拒绝覆盖，链接与被指向内容都不变。"""
        real_dir = self.work / "real_target"
        write_files(real_dir, {"marker.txt": b"behind symlink, do not touch\n"})
        target = self.work / "snapshot"
        os.symlink(real_dir, target, target_is_directory=True)
        link_before = os.readlink(target)
        work_before = capture_tree(self.work)

        proc = self.run_backup(target)
        self.assert_rejected("目标是有效符号链接", proc, REASON_OVERWRITE, work_before)
        self.assertTrue(target.is_symlink(), "失败后符号链接本身被替换")
        self.assertEqual(os.readlink(target), link_before, "符号链接目标被改动")
        self.assertEqual(
            (real_dir / "marker.txt").read_bytes(), b"behind symlink, do not touch\n",
            "符号链接指向的文件被改动",
        )

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_existing_dangling_symlink(self):
        """目标是指向不存在路径的悬空符号链接：同样拒绝覆盖，链接不变。"""
        missing = self.work / "nonexistent"
        target = self.work / "snapshot"
        os.symlink(missing, target)
        link_before = os.readlink(target)
        work_before = capture_tree(self.work)

        proc = self.run_backup(target)
        self.assert_rejected("目标是悬空符号链接", proc, REASON_OVERWRITE, work_before)
        self.assertTrue(target.is_symlink(), "失败后悬空符号链接本身被替换")
        self.assertEqual(os.readlink(target), link_before, "符号链接目标被改动")
        self.assertFalse(
            os.path.lexists(missing),
            "失败后悬空链接指向的路径被创建",
        )

    # ---- 禁止向源目录内备份 ----

    def assert_rejected_inside_source(self, label, proc, target, work_before):
        """源内目标用例的公共断言：拒绝原因 + 目标始终不存在。"""
        self.assert_rejected(label, proc, REASON_INSIDE_SOURCE, work_before)
        self.assertFalse(
            os.path.lexists(target),
            f"失败后源内目标仍被创建: {target}",
        )

    def test_reject_direct_child_of_source(self):
        """目标是源目录内尚不存在的直接子目录：拒绝且目标始终不存在。"""
        target = self.source / "snapshot"
        self.assertFalse(os.path.lexists(target))
        work_before = capture_tree(self.work)

        proc = self.run_backup(target)
        self.assert_rejected_inside_source(
            "源目录直接子目录", proc, target, work_before,
        )

    def test_reject_nested_inside_source(self):
        """目标是源目录内的嵌套目录（父目录事先存在）：拒绝且目标始终不存在。"""
        parent = self.source / "nested"
        parent.mkdir()
        target = parent / "snapshot"
        self.assertFalse(os.path.lexists(target))
        work_before = capture_tree(self.work)

        proc = self.run_backup(target)
        self.assert_rejected_inside_source(
            "源目录嵌套子目录", proc, target, work_before,
        )

    def test_reject_dotdot_path_resolving_into_source(self):
        """目标含 .. 分量、解析后落入源目录：与字面上的源内路径同样拒绝。"""
        subdir = self.source / "sub"
        subdir.mkdir()
        # 字面上先下后上，解析结果 source/snapshot 仍位于源目录内。
        target = subdir / ".." / "snapshot"
        resolved = self.source / "snapshot"
        self.assertFalse(os.path.lexists(target))
        self.assertFalse(os.path.lexists(resolved))
        work_before = capture_tree(self.work)

        proc = self.run_backup(target)
        self.assert_rejected_inside_source(
            "含 .. 分量解析后落入源目录", proc, resolved, work_before,
        )

    # ---- 正常对照：前缀相近的同级目录不是源内路径 ----

    def test_success_sibling_with_similar_name_prefix(self):
        """目标 source-copy 与源目录 source 同级且前缀相近：备份成功。"""
        target = self.work / "source-copy"
        self.assertFalse(os.path.lexists(target))

        proc = self.run_backup(target)
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"正常对照应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(target.resolve()), stdout,
            f"标准输出应包含目标绝对路径\n{context}",
        )
        self.assertIn(
            "已备份文件数: 2", stdout,
            f"标准输出应包含文件数 2\n{context}",
        )

        # data 中的相对路径与原始字节逐一同源文件一致。
        self.assertEqual(
            file_tree(target / "data"), SOURCE_FILES,
            "快照 data 中的相对路径或字节与源文件不一致",
        )
        bin_bytes = (target / "data" / "嵌套 目录" / "二进制 文件.bin").read_bytes()
        self.assertIn(0, bin_bytes, "二进制文件应保留零字节")

        # manifest.json 保持版本 1 且只列出这两个路径。
        manifest = json.loads(
            (target / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest.get("version"), 1, "清单版本应为 1")
        self.assertEqual(
            [entry.get("path") for entry in manifest.get("files", [])],
            sorted(SOURCE_FILES),
            "清单应恰好按序列出两个源文件相对路径",
        )

        # 源目录内容不变。
        self.assertEqual(
            capture_tree(self.source), self.source_before,
            "备份成功后源目录内容发生变化",
        )


if __name__ == "__main__":
    unittest.main()
