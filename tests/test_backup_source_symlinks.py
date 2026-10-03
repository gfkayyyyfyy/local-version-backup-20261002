#!/usr/bin/env python3
"""backup 拒绝源目录符号链接的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、链接目标与快照均在独立临时目录中
运行时准备，用例结束自动清理，不读写用户现有目录，也不依赖网络或额外
安装包。

覆盖既有约定（README“安全与失败语义”：源目录自身及内部不得含符号链接）：

1. 源目录参数本身是指向普通目录的符号链接：退出码 2，标准错误包含
   “源目录自身是符号链接”与输入源路径。
2. 源目录内有指向自身普通文件的符号链接：退出码 2，标准错误包含
   “源目录中包含符号链接”与以 / 分隔的相对路径。
3. 嵌套目录内有指向源目录外普通目录的符号链接：拒绝原因与相对路径同上。
4. 内部符号链接的目标不存在（悬空链接）：拒绝原因与相对路径同上。

四个失败样例各自只放置一个违规链接，快照均为临时工作区中事先不存在的
同级路径；共同要求：标准输出为空、快照路径始终不存在，调用前后源目录、
链接本身及已有链接目标的条目类型、链接指向与普通文件字节保持一致
（比较时不跟随链接，访问时间变化不视为数据改变）。

另含一个不含任何链接的正常对照：note.txt、嵌套中文及空格目录中含零
字节的二进制文件与一个空文件，共 3 个文件完整备份成功。

若系统不支持创建符号链接或当前权限拒绝创建，仅四个符号链接用例明确
跳过并注明原因；正常对照始终执行。产品命令自身的失败一律按断言失败
处理，不作为跳过理由。
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

# 源目录固定内容：文本文件、嵌套中文及空格目录下含零字节（0x00）的
# 二进制文件，以及一个空文件。
SOURCE_FILES = {
    "note.txt": "笔记内容\n第二行\n".encode("utf-8"),
    "嵌套 目录/二进制 文件.bin": bytes(range(256)),
    "空 文件.dat": b"",
}

# 标准错误中的既有公开拒绝原因。
REASON_ROOT_SYMLINK = "源目录自身是符号链接"
REASON_INSIDE_SYMLINK = "源目录中包含符号链接"


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
    记录链接目标字符串（基于 lstat，不跟随链接，也不进入链接指向的
    目录），因此访问时间变化不出现在比较结果中。
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
    """递归记录普通文件：相对路径 -> 字节（不记录符号链接）。"""
    return {
        key: value[1]
        for key, value in capture_tree(root).items()
        if value[0] == "file"
    }


def _symlink_supported():
    """探测当前环境是否允许创建符号链接（某些平台需特权）。"""
    with tempfile.TemporaryDirectory(prefix="symlink-probe-") as probe:
        try:
            os.symlink("target", Path(probe) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINK_SUPPORTED = _symlink_supported()
SYMLINK_SKIP_REASON = "当前环境不支持或无权限创建符号链接，跳过该用例"


class BackupSourceSymlinkTests(unittest.TestCase):
    """backup 命令对源目录自身及内部符号链接的拒绝约定，外加正常对照。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-src-symlink-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        # 失败用例与正常对照共用的真实源内容；各失败用例另行只放置一个
        # 违规符号链接。
        self.source = self.work / "source"
        write_files(self.source, SOURCE_FILES)

    # ---- 通用断言 ----

    def run_backup(self, source_arg, snapshot):
        """以 README 公开入口执行 backup。"""
        return run_cmd(["backup", str(source_arg), str(snapshot)])

    def assert_rejected(
        self, label, proc, snapshot, work_before, expected_reason, expected_hint,
    ):
        """失败用例公共断言。

        - 退出码为 2；
        - 标准输出为空（不打印成功摘要）；
        - 标准错误同时包含既有拒绝原因与提示信息（输入源路径或链接的
          / 分隔相对路径）；
        - 快照路径（含悬空符号链接形态）始终不存在；
        - 整个临时工作区调用前后逐字节、逐类型一致（不跟随链接、
          不比较访问时间）。
        """
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        self.assertIn(
            expected_reason, stderr,
            f"标准错误缺少拒绝原因“{expected_reason}”\n{context}",
        )
        self.assertIn(
            expected_hint, stderr,
            f"标准错误缺少提示信息“{expected_hint}”\n{context}",
        )
        self.assertFalse(
            os.path.lexists(snapshot),
            f"失败后快照路径仍被创建: {snapshot}\n{context}",
        )
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"失败后临时工作区出现新增、覆盖或删除\n{context}",
        )
        return stderr

    def assert_link_intact(self, link, target_before, label):
        """链接本身仍是符号链接且指向不变（不跟随链接比较）。"""
        self.assertTrue(
            link.is_symlink(),
            f"用例 {label}：调用后违规链接不再是符号链接: {link}",
        )
        self.assertEqual(
            os.readlink(link), target_before,
            f"用例 {label}：符号链接指向被改动: {link}",
        )

    # ---- 失败样例 1：源目录参数本身是指向普通目录的链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_root_source_being_symlink_to_directory(self):
        """源参数本身为符号链接：报错含“源目录自身是符号链接”与输入路径。"""
        real_source = self.work / "real-source"
        os.rename(str(self.source), str(real_source))
        source_link = self.work / "source-link"
        os.symlink("real-source", source_link, target_is_directory=True)
        link_target_before = os.readlink(source_link)

        snapshot = self.work / "snapshot"
        self.assertFalse(os.path.lexists(snapshot))
        work_before = capture_tree(self.work)

        proc = self.run_backup(source_link, snapshot)
        self.assert_rejected(
            "源目录自身是符号链接",
            proc, snapshot, work_before,
            REASON_ROOT_SYMLINK, str(source_link),
        )

        # 输入的链接保持原样；被指向的真实目录及其文件字节不变。
        self.assert_link_intact(source_link, link_target_before, "根链接")
        self.assertTrue(real_source.is_dir(), "被指向的真实目录类型发生变化")
        self.assertEqual(
            file_tree(real_source), SOURCE_FILES,
            "被链接指向的真实源目录中文件字节发生变化",
        )

    # ---- 失败样例 2：源目录内有指向自身普通文件的链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_inner_symlink_to_own_regular_file(self):
        """源目录根部的链接指向源内普通文件：报错含 / 分隔相对路径。"""
        link_rel = "自身文件链接"
        link = self.source / link_rel
        os.symlink("note.txt", link)
        link_target_before = os.readlink(link)

        snapshot = self.work / "snapshot"
        self.assertFalse(os.path.lexists(snapshot))
        work_before = capture_tree(self.work)

        proc = self.run_backup(self.source, snapshot)
        self.assert_rejected(
            "内部链接指向自身普通文件",
            proc, snapshot, work_before,
            REASON_INSIDE_SYMLINK, link_rel,
        )

        self.assert_link_intact(link, link_target_before, "指向自身文件")
        # 被指向的普通文件仍是普通文件且字节不变。
        self.assertTrue(
            (self.source / "note.txt").is_file(),
            "链接指向的普通文件类型发生变化",
        )
        self.assertEqual(
            (self.source / "note.txt").read_bytes(), SOURCE_FILES["note.txt"],
            "链接指向的普通文件字节被改动",
        )

    # ---- 失败样例 3：嵌套目录内有指向源目录外普通目录的链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_nested_symlink_to_outside_directory(self):
        """嵌套目录中的链接指向源外普通目录：报错含该链接的 / 分隔相对路径。"""
        outside_dir = self.work / "outside-dir"
        outside_files = {"外部 文件.txt": "源目录外的内容，不得改动\n".encode("utf-8")}
        write_files(outside_dir, outside_files)

        link_rel = "嵌套 目录/外部目录链接"
        link = self.source / link_rel
        os.symlink(outside_dir, link, target_is_directory=True)
        link_target_before = os.readlink(link)

        snapshot = self.work / "snapshot"
        self.assertFalse(os.path.lexists(snapshot))
        work_before = capture_tree(self.work)

        proc = self.run_backup(self.source, snapshot)
        self.assert_rejected(
            "嵌套链接指向源外普通目录",
            proc, snapshot, work_before,
            REASON_INSIDE_SYMLINK, link_rel,
        )

        self.assert_link_intact(link, link_target_before, "指向外部目录")
        # 源外被指向的目录与其中普通文件保持原样。
        self.assertTrue(outside_dir.is_dir(), "源外被指向目录的类型发生变化")
        self.assertEqual(
            file_tree(outside_dir), outside_files,
            "源外被指向目录中的文件字节发生变化",
        )

    # ---- 失败样例 4：内部链接的目标不存在 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_inner_dangling_symlink(self):
        """嵌套目录中的悬空链接（目标不存在）：同样拒绝且链接不被改动。"""
        link_rel = "子目录/悬空链接"
        link = self.source / link_rel
        link.parent.mkdir(parents=True, exist_ok=True)
        missing_target = "../不存在的目标"
        os.symlink(missing_target, link)
        link_target_before = os.readlink(link)
        # 解析后目标应确实不存在，保证本用例真正覆盖“悬空链接”。
        self.assertFalse(os.path.lexists(link.parent / missing_target))

        snapshot = self.work / "snapshot"
        self.assertFalse(os.path.lexists(snapshot))
        work_before = capture_tree(self.work)

        proc = self.run_backup(self.source, snapshot)
        self.assert_rejected(
            "内部悬空链接",
            proc, snapshot, work_before,
            REASON_INSIDE_SYMLINK, link_rel,
        )

        self.assert_link_intact(link, link_target_before, "悬空链接")
        self.assertFalse(
            os.path.lexists(link.parent / missing_target),
            "调用后悬空链接原本不存在的目标被创建",
        )

    # ---- 正常对照：不含任何符号链接的源目录完整备份 ----

    def test_success_without_any_symlink(self):
        """无链接源目录（3 个文件，含中文/空格路径、零字节与空文件）备份成功。"""
        # 明确保证源目录中不存在任何符号链接。
        source_tree = capture_tree(self.source)
        self.assertFalse(
            any(kind == "symlink" for kind, _ in source_tree.values()),
            "正常对照的源目录不应包含符号链接",
        )
        source_before = dict(source_tree)

        snapshot = self.work / "snapshot"
        self.assertFalse(os.path.lexists(snapshot))

        proc = self.run_backup(self.source, snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"正常对照应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(snapshot.resolve()), stdout,
            f"标准输出应包含快照绝对路径\n{context}",
        )
        self.assertIn(
            "已备份文件数: 3", stdout,
            f"标准输出应包含文件数 3\n{context}",
        )

        # data 中三个文件的相对路径与原始字节和源逐字节一致。
        self.assertEqual(
            file_tree(snapshot / "data"), SOURCE_FILES,
            "快照 data 中的相对路径或字节与源文件不一致",
        )
        bin_bytes = (snapshot / "data" / "嵌套 目录" / "二进制 文件.bin").read_bytes()
        self.assertIn(0, bin_bytes, "二进制文件应保留零字节 0x00")
        self.assertEqual(
            (snapshot / "data" / "空 文件.dat").read_bytes(), b"",
            "空文件备份后应仍为空",
        )

        # 版本 1 清单仅列出这三个文件，且不携带 sha256 字段。
        manifest = json.loads(
            (snapshot / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest.get("version"), 1, "清单版本应为 1")
        files = manifest.get("files")
        self.assertIsInstance(files, list, "清单 files 应为数组")
        self.assertEqual(
            [entry.get("path") for entry in files],
            sorted(SOURCE_FILES),
            "清单应恰好按序列出三个源文件相对路径",
        )
        for entry in files:
            self.assertEqual(
                set(entry.keys()), {"path"},
                f"不带 --checksum 时条目不应携带 sha256 等额外字段: {entry}",
            )
            self.assertNotIn("sha256", entry)

        # 源目录保持原样（类型、字节一致；不比较访问时间）。
        self.assertEqual(
            capture_tree(self.source), source_before,
            "备份成功后源目录内容发生变化",
        )


if __name__ == "__main__":
    unittest.main()
