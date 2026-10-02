#!/usr/bin/env python3
"""restore 按 --file 指定文件恢复的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH]...

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中
运行时准备，用例结束自动清理，不读写用户现有目录，也不依赖网络或
额外安装包。

夹具快照由公开 backup 命令从三个普通文件生成：

- ``note.txt``：明确的 UTF-8 文本；
- ``嵌套 目录/二进制 文件.bin``：嵌套中文目录下含零字节（0x00）的二进制；
- ``other.txt``：用于对照的未选择文件。

快照生成后再修改源目录中的 ``note.txt``，使源目录当前内容与快照内容
不同，从而证明恢复字节来自快照而非源目录。

覆盖约定：

1. 选择两个文件恢复到全新目标：退出码 0、标准错误为空、标准输出包含
   目标绝对路径与“已恢复文件数: 2”；目标只含这两个文件及必要父目录，
   字节逐字等于备份时数据，``other.txt`` 不出现。
2. 重复选择同一路径只恢复一次，计数不增加。
3. 不带 --file 的恢复作为对照：仍恢复清单中的全部三个文件。
4. 选择失败结果确定：清单中不存在的路径、仅大小写不同的 ``Note.txt``、
   目录名、``*.txt`` 通配符均按逐字匹配失败，不展开目录、不忽略大小写、
   不做通配匹配；空字符串、绝对路径、含空/`.`/`..` 分量的选择作为非法
   选择拒绝。上述情况一律退出码 2，标准错误说明原因，标准输出没有成功
   摘要，原先不存在的目标仍不存在。
5. 快照中未被选择的 ``other.txt`` 数据缺失时，即使只选择仍完好的
   ``note.txt`` 也整体失败（退出码 2，指出缺失路径），不创建目标、
   不返回部分成功。

每次恢复前后都比较源目录与快照的完整目录树（路径集合、条目类型与
普通文件字节），确认恢复不改动它们；损坏样例以调用恢复前的状态为
比较基准。
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

# 夹具中的三个相对路径（斜杠分隔，目录名含空格，与 README 示例一致）。
NOTE_REL = "note.txt"
BIN_DIR_REL = "嵌套 目录"
BIN_REL = "嵌套 目录/二进制 文件.bin"
OTHER_REL = "other.txt"

# 备份时的固定内容：明确文本、含零字节的二进制、未选择文件内容。
NOTE_BACKUP_BYTES = "note.txt 备份时的原文\n第二行原文\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x7F, 0x00, 0x80, 0x00])
OTHER_BYTES = "未被选择的 other.txt 内容\n最后一行\n".encode("utf-8")

# 备份完成后对源目录 note.txt 的修改，必须与备份字节明确不同。
NOTE_MODIFIED_BYTES = "源目录中的 note.txt 已被修改\n与快照不同\n".encode("utf-8")

BACKUP_FILES = {
    NOTE_REL: NOTE_BACKUP_BYTES,
    BIN_REL: BINARY_BYTES,
    OTHER_REL: OTHER_BYTES,
}

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_NOT_IN_MANIFEST = "选择的路径未在快照清单中"
REASON_EMPTY = "选择不能为空字符串"
REASON_ABSOLUTE = "选择不能是绝对路径"
REASON_BAD_COMPONENT = "选择包含无效路径分量"
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

    类型为 "dir" / "file" / "symlink"；普通文件记录完整字节。本夹具不含
    符号链接，保留类型区分是为了让路径集合与条目类型的任何变化都暴露。
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


class RestoreSelectionTests(unittest.TestCase):
    """--file 选择恢复的成功路径、确定性失败与现场不变性。"""

    def setUp(self):
        # 每个用例独立临时工作区，样例之间互不污染。
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-selection-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

        write_files(self.source, BACKUP_FILES)

        # 夹具快照必须由公开 backup 命令生成；失败则直接中止本用例。
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 备份后再改动源目录 note.txt：恢复出旧字节才能证明数据取自快照。
        (self.source / NOTE_REL).write_bytes(NOTE_MODIFIED_BYTES)

    # ---- 通用执行与断言 ----

    def run_restore(self, dest, selections=None):
        """以公开入口执行 restore；selections 为 None 时不带任何 --file。"""
        argv = ["restore", str(self.snapshot), str(dest)]
        if selections is not None:
            for sel in selections:
                argv.extend(["--file", sel])
        return run_cmd(argv)

    def assert_ground_unchanged(self, source_before, snapshot_before, context):
        """恢复（无论成败）不得改动源目录与快照的路径集合、类型与字节。"""
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"恢复后源目录目录树发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(self.snapshot), snapshot_before,
            f"恢复后快照目录树发生变化\n{context}",
        )

    def run_and_assert_success(
        self, dest, selections, expected_count, expected_tree, label,
    ):
        """成功用例公共流程：预检输出、结果目录树，并核对源目录与快照不变。"""
        self.assertFalse(
            os.path.lexists(dest),
            f"用例前提：恢复目标必须事先不存在: {dest}（{label}）",
        )
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest, selections)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n选择: {selections!r}\n目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(dest.resolve()), stdout,
            f"标准输出应包含恢复目标绝对路径\n{context}",
        )
        self.assertIn(
            f"已恢复文件数: {expected_count}", stdout,
            f"标准输出应包含恢复文件数 {expected_count}\n{context}",
        )

        self.assertTrue(dest.is_dir(), f"恢复后目标应为目录\n{context}")
        self.assertEqual(
            capture_tree(dest), expected_tree,
            f"恢复结果目录树（路径集合/类型/字节）与预期不符\n{context}",
        )

        self.assert_ground_unchanged(source_before, snapshot_before, context)
        return stdout, context

    def run_and_assert_rejected(
        self, dest, selections, reasons, label, *, literal=True,
    ):
        """失败用例公共流程：退出码 2、报错原因、无成功摘要、目标不存在，
        且源目录与快照相对调用前保持不变。reasons 为须全部出现的原因片段。
        """
        self.assertFalse(
            os.path.lexists(dest),
            f"用例前提：恢复目标必须事先不存在: {dest}（{label}）",
        )
        # 基线在调用 restore 前一瞬间拍摄（损坏样例已在更早完成破坏）。
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest, selections)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n选择: {selections!r}\n目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少拒绝原因“{reason}”\n{context}",
            )
        if literal and selections:
            # 逐字匹配失败时，报错应复述原始选择值（空串无可复述内容）。
            for sel in selections:
                if sel != "":
                    self.assertIn(
                        sel, stderr,
                        f"标准错误应逐字复述非法选择值: {sel!r}\n{context}",
                    )
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )

        self.assert_ground_unchanged(source_before, snapshot_before, context)
        return stdout, stderr, context

    # ---- 成功：选择两个文件 ----

    def test_restore_two_selected_files(self):
        """选择 note.txt 与嵌套二进制文件：只恢复这两个文件及必要父目录。"""
        dest = self.work / "restored-selected"
        expected_tree = {
            NOTE_REL: ("file", NOTE_BACKUP_BYTES),
            BIN_DIR_REL: ("dir", None),
            BIN_REL: ("file", BINARY_BYTES),
        }
        _, context = self.run_and_assert_success(
            dest, [NOTE_REL, BIN_REL], 2, expected_tree,
            "选择两个文件恢复",
        )

        # 字节必须等于备份时数据，而非源目录中已被修改的当前内容。
        self.assertEqual(
            (dest / NOTE_REL).read_bytes(), NOTE_BACKUP_BYTES,
            f"note.txt 未按备份时字节恢复\n{context}",
        )
        self.assertNotEqual(
            NOTE_BACKUP_BYTES, NOTE_MODIFIED_BYTES,
            "测试前提：备份字节与修改后字节必须不同",
        )
        bin_bytes = (dest / BIN_DIR_REL / "二进制 文件.bin").read_bytes()
        self.assertEqual(
            bin_bytes, BINARY_BYTES,
            f"嵌套二进制文件未按备份字节恢复\n{context}",
        )
        self.assertIn(0, bin_bytes, f"二进制文件应保留零字节\n{context}")

        # 未选择文件不出现；目标只含两个文件加一个必要父目录。
        self.assertFalse(
            (dest / OTHER_REL).exists(),
            f"未选择的 other.txt 不应出现\n{context}",
        )
        self.assertEqual(
            sorted(
                key for key, entry in capture_tree(dest).items()
                if entry[0] == "file"
            ),
            [NOTE_REL, BIN_REL],
            f"目标应恰好含两个被选择文件\n{context}",
        )

        # 源目录保持修改后的状态：恢复不得回写源目录。
        self.assertEqual(
            (self.source / NOTE_REL).read_bytes(), NOTE_MODIFIED_BYTES,
            f"恢复改动了源目录中的 note.txt\n{context}",
        )

    # ---- 成功：重复选择只恢复一次 ----

    def test_duplicate_selection_restored_once(self):
        """重复选择同一路径只恢复一次，计数为 1 且不重复写入。"""
        dest = self.work / "restored-duplicate"
        expected_tree = {NOTE_REL: ("file", NOTE_BACKUP_BYTES)}
        _, context = self.run_and_assert_success(
            dest, [NOTE_REL, NOTE_REL], 1, expected_tree,
            "重复选择同一路径",
        )
        self.assertTrue(
            (dest / NOTE_REL).is_file(),
            f"note.txt 应恰好恢复一次\n{context}",
        )
        self.assertFalse(
            (dest / OTHER_REL).exists(),
            f"未选择文件不应出现\n{context}",
        )

    # ---- 成功对照：不带 --file 恢复全部三个文件 ----

    def test_restore_without_file_restores_all_three(self):
        """不带 --file：仍恢复清单中的全部三个文件，计数为 3。"""
        dest = self.work / "restored-all"
        expected_tree = {
            NOTE_REL: ("file", NOTE_BACKUP_BYTES),
            BIN_DIR_REL: ("dir", None),
            BIN_REL: ("file", BINARY_BYTES),
            OTHER_REL: ("file", OTHER_BYTES),
        }
        stdout, context = self.run_and_assert_success(
            dest, None, 3, expected_tree,
            "不带 --file 恢复全部",
        )
        # 计数行只能出现一次且为 3，不能残留选择场景下的计数。
        self.assertEqual(
            stdout.count("已恢复文件数:"), 1,
            f"文件数摘要应恰好出现一次\n{context}",
        )
        self.assertEqual(
            (dest / OTHER_REL).read_bytes(), OTHER_BYTES,
            f"全量恢复应包含 other.txt 且字节一致\n{context}",
        )
        # 已被修改的 note.txt 仍按备份字节恢复。
        self.assertEqual(
            (dest / NOTE_REL).read_bytes(), NOTE_BACKUP_BYTES,
            f"全量恢复应得到备份时的 note.txt\n{context}",
        )

    # ---- 失败：逐字匹配，不做任何形式的模糊匹配 ----

    def test_reject_path_not_in_manifest(self):
        """清单中不存在的普通路径：逐字匹配失败，不返回部分成功。"""
        dest = self.work / "restored-missing"
        self.run_and_assert_rejected(
            dest, ["no-such-file.txt"],
            [REASON_NOT_IN_MANIFEST],
            "清单中不存在的路径",
        )

    def test_reject_case_different_path(self):
        """仅大小写不同的 Note.txt 不匹配 note.txt：不忽略大小写。"""
        dest = self.work / "restored-case"
        self.run_and_assert_rejected(
            dest, ["Note.txt"],
            [REASON_NOT_IN_MANIFEST],
            "仅大小写不同",
        )

    def test_reject_directory_name_not_expanded(self):
        """选择目录名本身：按逐字路径匹配失败，不展开目录内文件。"""
        dest = self.work / "restored-dir"
        self.run_and_assert_rejected(
            dest, [BIN_DIR_REL],
            [REASON_NOT_IN_MANIFEST],
            "选择目录名",
        )

    def test_reject_glob_pattern_literal(self):
        """*.txt 作为字面值匹配失败：不做通配符展开。"""
        dest = self.work / "restored-glob"
        # 以参数原样传入（不经 shell），星号必须被当作普通字符。
        self.run_and_assert_rejected(
            dest, ["*.txt"],
            [REASON_NOT_IN_MANIFEST],
            "通配符字面量",
        )

    # ---- 失败：非法选择形态 ----

    def test_reject_empty_string_selection(self):
        """空字符串选择：作为非法选择拒绝。"""
        dest = self.work / "restored-empty"
        self.run_and_assert_rejected(
            dest, [""],
            [REASON_EMPTY],
            "空字符串",
            literal=False,
        )

    def test_reject_absolute_selection(self):
        """/note.txt：绝对路径选择被拒绝，不与清单中的 note.txt 混淆。"""
        dest = self.work / "restored-absolute"
        self.run_and_assert_rejected(
            dest, ["/note.txt"],
            [REASON_ABSOLUTE],
            "绝对路径",
        )

    def test_reject_empty_path_component(self):
        """a//b：含空分量的不规范路径被拒绝。"""
        dest = self.work / "restored-empty-component"
        self.run_and_assert_rejected(
            dest, ["a//b"],
            [REASON_BAD_COMPONENT],
            "含空分量",
        )

    def test_reject_dot_component(self):
        """./note.txt：含当前目录分量，不归一化为 note.txt。"""
        dest = self.work / "restored-dot"
        self.run_and_assert_rejected(
            dest, ["./note.txt"],
            [REASON_BAD_COMPONENT],
            "含点分量",
        )

    def test_reject_parent_component(self):
        """../note.txt：含上级目录分量，直接拒绝。"""
        dest = self.work / "restored-parent"
        self.run_and_assert_rejected(
            dest, ["../note.txt"],
            [REASON_BAD_COMPONENT],
            "含上级目录分量",
        )

    # ---- 失败：快照缺失未被选择的数据仍整体失败 ----

    def test_reject_when_unselected_data_missing(self):
        """删除快照中的 other.txt 后只选 note.txt：整单校验失败，无部分成功。"""
        missing_data = self.snapshot / "data" / OTHER_REL
        self.assertTrue(
            missing_data.is_file(),
            "测试前提：other.txt 的快照数据应事先存在",
        )
        missing_data.unlink()
        self.assertFalse(missing_data.exists())

        dest = self.work / "restored-damaged"
        # 即使选择值只有仍完好的 note.txt，也必须指出缺失的 other.txt。
        # 该报错来自整单校验，不复述选择值，故关闭逐字复述核对。
        self.run_and_assert_rejected(
            dest, [NOTE_REL],
            [REASON_DATA_MISSING, OTHER_REL],
            "未选择数据缺失",
            literal=False,
        )


if __name__ == "__main__":
    unittest.main()
