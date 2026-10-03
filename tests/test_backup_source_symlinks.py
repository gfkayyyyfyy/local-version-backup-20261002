#!/usr/bin/env python3
"""backup 拒绝源目录符号链接的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、链接目标与快照路径都在独立临时
目录中运行时准备，用例结束自动清理，不读写用户现有目录，也不依赖网络
或额外安装包。

覆盖既有约定（README“安全与失败语义”：源目录自身及内部不得含符号链接）：

1. 源目录参数本身是指向普通目录的符号链接 —— 拒绝，标准错误指出
   “源目录自身是符号链接”并回显输入源路径。
2. 源目录内有指向其自身普通文件的符号链接 —— 拒绝。
3. 嵌套目录内有指向源目录之外普通目录的符号链接 —— 拒绝。
4. 源目录内符号链接的目标不存在（悬空链接）—— 拒绝。

每个失败样例只放置一个违规链接，快照路径选择临时工作区中事先不存在的
同级路径；四种情况均要求退出码 2、标准输出为空、快照路径始终不存在。
标准错误须包含“源目录中包含符号链接”及以 ``/`` 分隔的相对路径。

调用前后逐字比较整个临时工作区：条目类型、符号链接指向与普通文件字节
均须一致（不跟随链接，不记录访问时间，因此访问时间变化不视为数据改变）。

另含一个不含任何链接的正常对照：文本文件、嵌套中文及空格目录中的含零
字节二进制文件和一个空文件，共 3 个文件，备份成功并可核对 data 字节与
版本 1 清单（不带 sha256 字段）。
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

# 正常对照的源目录固定内容：文本文件、嵌套中文及空格目录下含零字节
# （0x00）的二进制文件，以及一个空文件，共 3 个普通文件。
SUCCESS_FILES = {
    "note.txt": "笔记内容\n第二行\n".encode("utf-8"),
    "嵌套 目录/数据 备份.bin": b"\x00\xff\x10\x00" + bytes(range(256)),
    "空文件.dat": b"",
}

# 失败用例标准错误中的既有中文标记。
ERROR_PREFIX = "错误"
REASON_ROOT_SYMLINK = "源目录自身是符号链接"
REASON_CONTAINS_SYMLINK = "源目录中包含符号链接"


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
    记录链接目标字符串（os.walk 不跟随链接，也不进入链接指向的目录）。
    不记录修改/访问时间，因此访问时间变化不会被误判为数据改变。
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
    """递归记录普通文件：相对路径 -> 字节（不包含符号链接）。"""
    return {
        key: value[1]
        for key, value in capture_tree(root).items()
        if value[0] == "file"
    }


def _symlink_supported():
    """探测当前环境是否允许创建符号链接（某些平台需特权或被权限拒绝）。"""
    with tempfile.TemporaryDirectory(prefix="symlink-probe-") as probe:
        try:
            os.symlink("target", Path(probe) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINK_SUPPORTED = _symlink_supported()
SYMLINK_SKIP_REASON = "当前环境不支持创建符号链接或权限被拒绝，跳过该用例"


class BackupSourceSymlinkTests(unittest.TestCase):
    """backup 命令对源目录自身及内部符号链接的拒绝约定。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-symlink-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        # 每个用例的快照都选择事先不存在的同级路径。
        self.snapshot = self.work / "snapshot"
        self.assertFalse(os.path.lexists(self.snapshot))

    def run_backup(self, source):
        """以公开入口执行 backup，SOURCE 按用例构造（可能本身是链接）。"""
        return run_cmd(["backup", str(source), str(self.snapshot)])

    # ---- 失败用例公共断言 ----

    def assert_rejected(self, label, proc, reason_markers):
        """退出码 2、标准输出为空、标准错误包含全部指定片段。

        返回解码后的标准错误以便用例继续做更具体的断言。
        """
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for marker in reason_markers:
            self.assertIn(
                marker, stderr,
                f"标准错误缺少“{marker}”\n{context}",
            )
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"失败后快照路径仍被创建: {self.snapshot}\n{context}",
        )
        return stderr

    def assert_workspace_unchanged(self, work_before, label, proc):
        """整个临时工作区在调用前后逐字一致（类型、链接指向、文件字节）。"""
        context = (
            f"用例: {label}\nexit={proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"调用前后临时工作区出现新增、删除、类型变化、链接指向变化"
            f"或普通文件字节变化\n{context}",
        )

    def assert_symlink_intact(self, link, target_before, label):
        """链接本身仍为符号链接且指向字符串未被改动（不跟随链接）。"""
        self.assertTrue(
            link.is_symlink(),
            f"用例 {label}：调用后链接不再是符号链接: {link}",
        )
        self.assertEqual(
            os.readlink(link), target_before,
            f"用例 {label}：符号链接指向被改动: {link}",
        )

    # ---- 1. 源目录参数本身是指向普通目录的符号链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_source_root_that_is_symlink_to_directory(self):
        """源目录参数本身是指向普通目录的链接：拒绝并回显输入源路径。"""
        real_source = self.work / "real-source"
        write_files(real_source, {
            "note.txt": "真实目录中的笔记\n".encode("utf-8"),
            "深层/普通文件.bin": b"\x00\x01\x02regular",
        })
        source_link = self.work / "source-link"
        os.symlink(real_source, source_link, target_is_directory=True)
        link_target_before = os.readlink(source_link)
        work_before = capture_tree(self.work)

        proc = self.run_backup(source_link)
        self.assert_rejected(
            "源目录自身是符号链接",
            proc,
            [REASON_ROOT_SYMLINK, str(source_link)],
        )

        self.assert_symlink_intact(
            source_link, link_target_before, "源目录自身是符号链接",
        )
        self.assert_workspace_unchanged(
            work_before, "源目录自身是符号链接", proc,
        )
        # 链接指向的真实目录内容同样不得被改动。
        self.assertEqual(
            (real_source / "note.txt").read_bytes(),
            "真实目录中的笔记\n".encode("utf-8"),
            "链接指向的真实目录中普通文件字节被改动",
        )

    # ---- 2. 源目录内有指向其自身普通文件的符号链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_internal_symlink_to_own_regular_file(self):
        """源目录顶层有一个指向源内普通文件的链接：拒绝并报告相对路径。"""
        source = self.work / "source"
        write_files(source, {
            "note.txt": "你好，世界\n".encode("utf-8"),
            "real-file.txt": b"target bytes inside source\n",
            "普通目录/other.txt": b"other regular file\n",
        })
        link = source / "self-file-link"
        # 相对链接，目标为源目录自身的普通文件。
        os.symlink("real-file.txt", link)
        link_target_before = os.readlink(link)
        work_before = capture_tree(self.work)

        proc = self.run_backup(source)
        self.assert_rejected(
            "内部链接指向自身普通文件",
            proc,
            [REASON_CONTAINS_SYMLINK, "self-file-link"],
        )

        self.assert_symlink_intact(
            link, link_target_before, "内部链接指向自身普通文件",
        )
        self.assert_workspace_unchanged(
            work_before, "内部链接指向自身普通文件", proc,
        )
        # 已有链接目标（源内普通文件）的字节保持一致。
        self.assertEqual(
            (source / "real-file.txt").read_bytes(),
            b"target bytes inside source\n",
            "链接指向的源内普通文件字节被改动",
        )

    # ---- 3. 嵌套目录内有指向源目录外普通目录的符号链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_nested_symlink_to_outside_directory(self):
        """嵌套目录中的链接指向源外普通目录：拒绝并报告以 / 分隔的相对路径。"""
        source = self.work / "source"
        write_files(source, {
            "note.txt": "源目录笔记\n".encode("utf-8"),
            "子目录/keep.txt": b"nested regular file\n",
        })
        # 链接目标位于源目录之外的普通目录，其中已有普通文件。
        outside_dir = self.work / "outside" / "real-dir"
        write_files(outside_dir, {"outside.txt": b"outside bytes, do not touch\n"})

        link = source / "子目录" / "外部目录链接"
        os.symlink(outside_dir, link, target_is_directory=True)
        link_target_before = os.readlink(link)
        rel_link = "子目录/外部目录链接"
        work_before = capture_tree(self.work)

        proc = self.run_backup(source)
        self.assert_rejected(
            "嵌套链接指向源外目录",
            proc,
            [REASON_CONTAINS_SYMLINK, rel_link],
        )

        self.assert_symlink_intact(
            link, link_target_before, "嵌套链接指向源外目录",
        )
        self.assert_workspace_unchanged(
            work_before, "嵌套链接指向源外目录", proc,
        )
        # 源目录外的已有链接目标及其普通文件保持原样。
        self.assertTrue(
            outside_dir.is_dir() and not outside_dir.is_symlink(),
            "源目录外的链接目标目录类型发生变化",
        )
        self.assertEqual(
            (outside_dir / "outside.txt").read_bytes(),
            b"outside bytes, do not touch\n",
            "源目录外链接目标中的普通文件字节被改动",
        )

    # ---- 4. 源目录内符号链接的目标不存在 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_internal_dangling_symlink(self):
        """源目录内悬空链接（目标不存在）：拒绝、链接保留、目标不被创建。"""
        source = self.work / "source"
        write_files(source, {
            "note.txt": b"regular note\n",
            "目录/普通.txt": b"regular nested\n",
        })
        missing_name = "already-missing-target"
        missing_target = source / missing_name
        self.assertFalse(os.path.lexists(missing_target))

        link = source / "dangling-link"
        os.symlink(missing_name, link)
        link_target_before = os.readlink(link)
        work_before = capture_tree(self.work)

        proc = self.run_backup(source)
        self.assert_rejected(
            "内部悬空链接",
            proc,
            [REASON_CONTAINS_SYMLINK, "dangling-link"],
        )

        self.assert_symlink_intact(link, link_target_before, "内部悬空链接")
        self.assertFalse(
            os.path.lexists(missing_target),
            "失败后悬空链接原本不存在的目标被创建",
        )
        self.assert_workspace_unchanged(work_before, "内部悬空链接", proc)

    # ---- 正常对照：不含任何链接的源目录备份成功 ----

    def test_success_without_any_symlink(self):
        """无链接源目录：3 个文件备份成功，data 字节与版本 1 清单均可核对。"""
        source = self.work / "source"
        write_files(source, SUCCESS_FILES)
        source_before = capture_tree(source)

        proc = self.run_backup(source)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"正常对照应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"标准输出应给出快照绝对路径\n{context}",
        )
        self.assertIn(
            "已备份文件数: 3", stdout,
            f"标准输出应给出文件数 3\n{context}",
        )

        # data 中三个文件的相对路径与原始字节逐一同源文件一致。
        data_dir = self.snapshot / "data"
        self.assertEqual(
            file_tree(data_dir), SUCCESS_FILES,
            "快照 data 中的相对路径或字节与源文件不一致",
        )
        bin_bytes = (data_dir / "嵌套 目录" / "数据 备份.bin").read_bytes()
        self.assertIn(0, bin_bytes, "二进制文件应保留零字节")
        self.assertEqual(
            (data_dir / "空文件.dat").stat().st_size, 0,
            "空文件在快照中应仍为 0 字节",
        )

        # 版本 1 清单恰好列出这三个文件，且条目不携带 sha256 字段。
        manifest = json.loads(
            (self.snapshot / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest.get("version"), 1, "清单版本应为 1")
        entries = manifest.get("files")
        self.assertIsInstance(entries, list, "清单 files 应为数组")
        self.assertEqual(
            [entry.get("path") for entry in entries],
            sorted(SUCCESS_FILES),
            "清单应恰好列出三个源文件相对路径",
        )
        for entry in entries:
            self.assertNotIn(
                "sha256", entry,
                "不使用 --checksum 时清单条目不应携带 sha256",
            )

        # 源目录保持原样（条目类型与字节；无链接不受影响）。
        self.assertEqual(
            capture_tree(source), source_before,
            "备份成功后源目录内容发生变化",
        )


if __name__ == "__main__":
    unittest.main()
