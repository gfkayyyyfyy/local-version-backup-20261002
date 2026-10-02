#!/usr/bin/env python3
"""restore 使用 --file 选择指定文件恢复的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH]...

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中
运行时准备，用例结束自动清理，不读写用户现有目录，不依赖符号链接
权限、网络或第三方包。

夹具由公开 backup 命令从三个普通文件生成版本 1 快照：明确的文本
``note.txt``、嵌套中文目录下含零字节（0x00）的
``嵌套 目录/二进制 文件.bin``、以及未被选择的 ``other.txt``。
备份完成后源目录中的 ``note.txt`` 会被改写，以证明选择恢复取的是
备份时的字节而非源目录现状。

覆盖的回归约定：

1. 用 --file 选择前两个文件恢复到全新目标：退出码 0、标准错误为空、
   标准输出含目标绝对路径与“已恢复文件数: 2”；目标只含这两个文件
   及必要的父目录，字节等于备份时数据，``other.txt`` 不出现；源目录
   （含已改写的 note.txt）与快照在恢复前后均不变。
2. 重复选择同一路径只恢复一次，文件数不重复计数。
3. 不带 --file 的恢复恢复清单中的全部三个文件（对照组）。
4. 选择值与清单逐字匹配：清单中不存在的路径、仅大小写不同的
   ``Note.txt``、目录名、``*.txt`` 通配符一律以退出码 2 失败，
   不展开目录、不忽略大小写、不匹配通配符，目标不被创建。
5. 非法选择形态（空字符串、绝对路径、空分量、"."、".." 分量）
   以退出码 2 拒绝并说明对应原因，目标不被创建。
6. 快照中未被选择的 ``other.txt`` 数据被删除时，即便只选择仍完好的
   ``note.txt`` 也整体失败（退出码 2，指出缺失路径），不创建目标、
   不返回部分成功；损坏样例以调用前状态为基准，失败后快照与源目录
   不得继续被改动。
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

# 备份时的三份明确内容。
NOTE_REL = "note.txt"
BIN_REL = "嵌套 目录/二进制 文件.bin"
OTHER_REL = "other.txt"

NOTE_BYTES = "这是备份时的笔记内容\n第二行原文\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x7F, 0x00, 0x80])
OTHER_BYTES = "未被选择的文件内容，选择恢复时不应出现\n".encode("utf-8")

# 备份之后对源目录 note.txt 的改写；选择恢复必须仍得到 NOTE_BYTES。
MODIFIED_NOTE_BYTES = "备份之后源目录里的新内容\n".encode("utf-8")

SOURCE_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
    OTHER_REL: OTHER_BYTES,
}

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"

# 各类失败在标准错误中的原因片段。
REASON_NOT_IN_MANIFEST = "选择的路径未在快照清单中"
REASON_EMPTY_SELECTION = "不能为空字符串"
REASON_ABSOLUTE_SELECTION = "不能是绝对路径"
REASON_BAD_COMPONENT = "无效路径分量"
REASON_DATA_MISSING = "清单引用的数据缺失"


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


def build_fixture(work):
    """在临时工作区中准备源目录、公开 backup 生成快照，并改写源 note.txt。

    返回 (source, snapshot)。夹具失败直接中止用例。
    """
    work = Path(work)
    source = work / "source"
    snapshot = work / "snapshot"
    write_files(source, SOURCE_FILES)

    proc = run_cmd(["backup", str(source), str(snapshot)])
    if proc.returncode != 0 or not snapshot.is_dir():
        raise RuntimeError(
            "测试夹具：基线快照创建失败\n"
            f"exit={proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )

    # 备份后改写源文件：选择恢复的结果必须仍是备份时的旧字节。
    (source / NOTE_REL).write_bytes(MODIFIED_NOTE_BYTES)
    return source, snapshot


class RestoreSelectionTests(unittest.TestCase):
    """--file 选择恢复的成功语义、逐字匹配与失败原子性。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-selection-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source, self.snapshot = build_fixture(self.work)

    # ---- 通用辅助 ----

    def run_restore(self, dest, selections=None):
        """以公开入口执行 restore；selections 为 --file 值列表，None 表示全量。"""
        argv = ["restore", str(self.snapshot), str(dest)]
        for sel in selections or ():
            argv.extend(["--file", sel])
        return run_cmd(argv)

    def assert_unchanged(self, before, root, label, context):
        """断言某目录（源目录或快照）调用前后的路径集合与字节完全不变。"""
        self.assertEqual(
            capture_tree(root), before,
            f"{label}在恢复前后发生变化\n{context}",
        )

    def assert_selection_rejected(
        self, label, selections, dest, reason, source, snapshot,
        extra_terms=(),
    ):
        """失败用例公共断言。

        以“调用恢复前的状态”为比较基准：退出码 2、标准错误含原因、
        标准输出无成功摘要、原先不存在的目标仍不存在、快照与源目录
        不得继续被改动。
        """
        self.assertFalse(
            os.path.lexists(dest),
            f"用例前置条件不成立：目标已存在: {dest}",
        )
        snapshot_before = capture_tree(snapshot)
        source_before = capture_tree(source)

        argv = ["restore", str(snapshot), str(dest)]
        for sel in selections:
            argv.extend(["--file", sel])
        proc = run_cmd(argv)

        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n选择值: {selections!r}\n目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        self.assertIn(reason, stderr, f"标准错误缺少拒绝原因“{reason}”\n{context}")
        for term in extra_terms:
            self.assertIn(term, stderr, f"标准错误缺少“{term}”\n{context}")
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )
        self.assert_unchanged(
            snapshot_before, snapshot, "快照", context,
        )
        self.assert_unchanged(
            source_before, source, "源目录", context,
        )
        return stdout, stderr, context

    # ---- 成功：选择两个文件 ----

    def test_select_two_files_restores_only_those(self):
        """选择 note.txt 与嵌套二进制文件：只恢复这两个，字节为备份时数据。"""
        dest = self.work / "restored-selected"
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest, [NOTE_REL, BIN_REL])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"选择恢复应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(dest.resolve()), stdout,
            f"标准输出应包含恢复目标绝对路径\n{context}",
        )
        self.assertIn(
            "已恢复文件数: 2", stdout,
            f"标准输出应包含文件数 2\n{context}",
        )

        tree = capture_tree(dest)
        restored_files = {
            key: value[1] for key, value in tree.items() if value[0] == "file"
        }
        restored_dirs = {
            key for key, value in tree.items() if value[0] == "dir"
        }

        # 目标只含被选择的两个文件，other.txt 不出现。
        self.assertEqual(
            set(restored_files), {NOTE_REL, BIN_REL},
            f"恢复文件路径集合应恰好为被选择的两个\n{context}",
        )
        self.assertEqual(
            len(restored_files), 2,
            f"恢复结果应恰好含两个文件\n{context}",
        )
        # 只保留嵌套文件所必需的父目录。
        self.assertEqual(
            restored_dirs, {"嵌套 目录"},
            f"目标中出现了多余或缺失的目录\n{context}",
        )

        # 字节等于备份时的数据：note 取旧版字节而非源目录现状。
        self.assertEqual(
            restored_files[NOTE_REL], NOTE_BYTES,
            f"note.txt 未按备份时字节恢复\n{context}",
        )
        bin_bytes = restored_files[BIN_REL]
        self.assertEqual(
            bin_bytes, BINARY_BYTES,
            f"嵌套二进制文件未按备份时字节恢复\n{context}",
        )
        self.assertIn(0, bin_bytes, f"二进制文件应保留零字节\n{context}")
        self.assertNotIn(
            OTHER_REL, restored_files,
            f"未选择的 other.txt 不应出现\n{context}",
        )

        # 源目录保留备份后的改写状态，快照原样不动。
        self.assertEqual(
            (self.source / NOTE_REL).read_bytes(), MODIFIED_NOTE_BYTES,
            f"恢复改动了源目录中的 note.txt\n{context}",
        )
        self.assert_unchanged(source_before, self.source, "源目录", context)
        self.assert_unchanged(snapshot_before, self.snapshot, "快照", context)

    # ---- 成功：重复选择去重 ----

    def test_repeated_selection_restored_once(self):
        """重复选择同一路径只恢复一次且计数不增加。"""
        # 同一路径连续重复三次：文件数为 1。
        dest_one = self.work / "restored-repeat-one"
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest_one, [NOTE_REL, NOTE_REL, NOTE_REL])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"目标 DEST: {dest_one}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"重复选择恢复应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            "已恢复文件数: 1", stdout,
            f"重复选择同一路径时计数不应增加\n{context}",
        )
        one_files = file_tree(dest_one)
        self.assertEqual(
            set(one_files), {NOTE_REL},
            f"只选择 note.txt 时不应恢复其他文件\n{context}",
        )
        self.assertEqual(
            one_files[NOTE_REL], NOTE_BYTES,
            f"note.txt 未按备份时字节恢复\n{context}",
        )
        self.assert_unchanged(source_before, self.source, "源目录", context)
        self.assert_unchanged(snapshot_before, self.snapshot, "快照", context)

        # 两个不同路径、其中一个重复：去重后文件数仍为 2。
        dest_two = self.work / "restored-repeat-two"
        proc = self.run_restore(dest_two, [NOTE_REL, BIN_REL, BIN_REL])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"目标 DEST: {dest_two}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"选择恢复应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            "已恢复文件数: 2", stdout,
            f"其中一个选择重复时计数不应增加\n{context}",
        )
        self.assertEqual(
            set(file_tree(dest_two)), {NOTE_REL, BIN_REL},
            f"去重后应恰好恢复两个不同文件\n{context}",
        )
        self.assert_unchanged(capture_tree(self.source), self.source, "源目录", context)
        self.assert_unchanged(
            capture_tree(self.snapshot), self.snapshot, "快照", context,
        )

    # ---- 对照：不带 --file 恢复全部 ----

    def test_restore_without_file_restores_all_files(self):
        """不带 --file：仍恢复清单中的全部三个文件。"""
        dest = self.work / "restored-all"
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest)
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = (
            f"目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"全量恢复应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(dest.resolve()), stdout,
            f"标准输出应包含恢复目标绝对路径\n{context}",
        )
        self.assertIn(
            "已恢复文件数: 3", stdout,
            f"全量恢复的文件数应为 3\n{context}",
        )

        expected = file_tree(self.snapshot / "data")
        restored = file_tree(dest)
        self.assertEqual(
            set(restored), {NOTE_REL, BIN_REL, OTHER_REL},
            f"全量恢复应得到清单中的全部三个文件\n{context}",
        )
        self.assertEqual(
            set(restored), set(expected),
            f"全量恢复的路径集合与快照数据不一致\n{context}",
        )
        for rel, data in expected.items():
            self.assertEqual(
                restored[rel], data,
                f"恢复文件字节与快照数据不一致: {rel}\n{context}",
            )
        # 未选文件 other.txt 与备份时字节一致。
        self.assertEqual(
            restored[OTHER_REL], OTHER_BYTES,
            f"other.txt 未按备份时字节恢复\n{context}",
        )

        self.assert_unchanged(source_before, self.source, "源目录", context)
        self.assert_unchanged(snapshot_before, self.snapshot, "快照", context)

    # ---- 失败：逐字匹配（每个样例独立临时目录）----

    def test_unmatched_selections_fail_verbatim(self):
        """不存在路径、大小写不同、目录名、通配符：逐字匹配失败，不做任何展开。"""
        cases = [
            ("清单中不存在的路径", "no-such-file.txt"),
            ("仅大小写不同的 Note.txt", "Note.txt"),
            ("清单中文件所在的目录名", "嵌套 目录"),
            ("通配符字面量 *.txt", "*.txt"),
        ]
        for label, selection in cases:
            with self.subTest(label):
                with tempfile.TemporaryDirectory(
                    prefix="restore-selection-nomatch-"
                ) as work:
                    source, snapshot = build_fixture(work)
                    dest = Path(work) / "restored"
                    self.assert_selection_rejected(
                        label, [selection], dest,
                        REASON_NOT_IN_MANIFEST, source, snapshot,
                        extra_terms=(selection,),
                    )

    # ---- 失败：非法选择形态（每个样例独立临时目录）----

    def test_illegal_selection_forms_rejected(self):
        """空串、绝对路径、空/./.. 路径分量：在匹配清单前拒绝。"""
        cases = [
            ("空字符串", "", REASON_EMPTY_SELECTION),
            ("以 / 开头的绝对路径", "/note.txt", REASON_ABSOLUTE_SELECTION),
            ("含空分量的 a//b", "a//b", REASON_BAD_COMPONENT),
            ("含当前目录分量的 ./note.txt", "./note.txt", REASON_BAD_COMPONENT),
            ("含上级目录分量的 ../note.txt", "../note.txt", REASON_BAD_COMPONENT),
        ]
        for label, selection, reason in cases:
            with self.subTest(label):
                with tempfile.TemporaryDirectory(
                    prefix="restore-selection-illegal-"
                ) as work:
                    source, snapshot = build_fixture(work)
                    dest = Path(work) / "restored"
                    self.assert_selection_rejected(
                        label, [selection], dest,
                        reason, source, snapshot,
                    )

    # ---- 失败：未选择的数据缺失也整体失败（独立损坏样例）----

    def test_missing_unselected_data_fails_whole_restore(self):
        """删除快照中未选的 other.txt 后仅选 note.txt：仍整体失败、无部分成功。"""
        with tempfile.TemporaryDirectory(
            prefix="restore-selection-corrupt-"
        ) as work:
            source, snapshot = build_fixture(work)
            dest = Path(work) / "restored"

            # 唯一的损坏：删除清单中存在、但本次未选择的 other.txt 数据。
            other_data = snapshot / "data" / OTHER_REL
            self.assertTrue(other_data.is_file())
            other_data.unlink()
            self.assertFalse(os.path.lexists(dest))

            # 比较基准为调用恢复前（已删除 other.txt）的状态。
            snapshot_before = capture_tree(snapshot)
            source_before = capture_tree(source)

            argv = [
                "restore", str(snapshot), str(dest),
                "--file", NOTE_REL,
            ]
            proc = run_cmd(argv)
            stdout = proc.stdout.decode("utf-8", errors="replace")
            stderr = proc.stderr.decode("utf-8", errors="replace")
            context = (
                "用例: 未选择的数据缺失\n"
                f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
            )

            self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
            self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
            # 必须指出真正缺失的路径，而不是被选择的 note.txt。
            self.assertIn(
                REASON_DATA_MISSING, stderr,
                f"标准错误应指出清单引用的数据缺失\n{context}",
            )
            self.assertIn(
                OTHER_REL, stderr,
                f"标准错误应指出缺失的是未选择的 {OTHER_REL}\n{context}",
            )
            for marker in SUMMARY_MARKERS:
                self.assertNotIn(
                    marker, stdout,
                    f"失败时标准输出不得出现成功摘要（部分成功）\n{context}",
                )

            # 不创建目标，不留下任何部分恢复结果。
            self.assertFalse(
                os.path.lexists(dest),
                f"失败后恢复目标仍被创建: {dest}\n{context}",
            )

            # 以调用前状态为基准：快照不得继续被改动，仍完好的数据保留；
            # 源目录保持备份后的改写状态。
            self.assertEqual(
                capture_tree(snapshot), snapshot_before,
                f"失败后快照被继续改动\n{context}",
            )
            self.assertEqual(
                capture_tree(source), source_before,
                f"失败后源目录被改动\n{context}",
            )
            self.assertEqual(
                (snapshot / "data" / NOTE_REL).read_bytes(), NOTE_BYTES,
                f"被选择且仍完好的 note.txt 快照数据不得受损\n{context}",
            )
            self.assertEqual(
                (source / NOTE_REL).read_bytes(), MODIFIED_NOTE_BYTES,
                f"源目录 note.txt 不得被恢复改动\n{context}",
            )


if __name__ == "__main__":
    unittest.main()
