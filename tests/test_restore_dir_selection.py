#!/usr/bin/env python3
"""restore 按 --dir 指定目录恢复的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--dir PATH]... [--file PATH]...
                                           [--dry-run]

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中
运行时准备，用例结束自动清理，不读写用户现有目录，也不依赖网络或
额外安装包。

夹具快照由公开 backup 命令生成，清单收录：

- ``note.txt``：用于 --file 合用的单文件；
- ``docs/a.txt`` 与 ``docs/sub/b.bin``：目录选择的全部后代（含嵌套子目录
  与零字节二进制）；
- ``docs-old/a.txt`` 与 ``docs.txt``：逐字分量匹配的对照，不得被
  ``--dir docs`` 选中；
- ``中文 目录/内 部.txt``：保留中文与空格的目录选择。

源目录中另有空目录 ``空目录``，不写入清单，用于验证未记录的空目录
按“选择的目录未包含快照清单文件”拒绝。备份完成后修改源目录
``docs/a.txt``，使恢复字节只能来自快照。

覆盖约定：

1. ``--dir docs`` 恰好选择 docs/a.txt 与 docs/sub/b.bin，保留相对快照的
   完整路径，不包含 docs-old/a.txt 与 docs.txt。
2. ``--dir docs --file note.txt`` 取并集：先 --dry-run 预览一行 JSON
   （files 为 3、paths 按 Unicode 码点升序、无新增字段、不创建目标及
   其父目录），再去掉 --dry-run 实际恢复，两次都选择三个文件且恢复
   字节与快照一致。
3. 重复目录、父子目录重叠及与 --file 的重复只恢复一次。
4. 目录选择仅依据清单路径：源目录被整体删除后仍可恢复；data/ 中未列入
   清单的额外文件不纳入结果。
5. 形态合法但没有清单后代的目录（含未记录的空目录、指向文件的目录
   参数）以退出码 2 拒绝，标准输出为空，标准错误包含“选择的目录未
   包含快照清单文件”及原始参数，不创建恢复目标。
6. 空字符串、以 / 开头、带 Windows 盘符、含反斜杠或含空/./.. 分量的
   目录参数以退出码 2 拒绝，标准输出为空，标准错误包含“目录选择路径
   无效”及原始参数。
7. 未选中文件的数据缺失时，即使只选择仍完好的目录也整体失败
   （退出码 2、标准输出为空、不创建目标）。

每次恢复前后都比较源目录与快照的完整目录树（路径集合、条目类型与
普通文件字节），确认恢复不改动它们；损坏样例以调用恢复前的状态为
比较基准。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = ROOT / "backup.py"

# 强制子进程按 UTF-8 输出，断言不依赖运行环境的区域设置。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

NOTE_REL = "note.txt"
DOC_A_REL = "docs/a.txt"
DOC_B_REL = "docs/sub/b.bin"
DOC_OLD_REL = "docs-old/a.txt"
DOC_TXT_REL = "docs.txt"
ZH_DIR_REL = "中文 目录"
ZH_REL = "中文 目录/内 部.txt"
EMPTY_DIR_REL = "空目录"

NOTE_BYTES = "note.txt 的原文\n".encode("utf-8")
DOC_A_BYTES = "docs/a.txt 备份时的原文\n".encode("utf-8")
DOC_B_BYTES = bytes([0x00, 0xFF, 0x10, 0x00, 0x7F])
DOC_OLD_BYTES = "docs-old/a.txt 不应被 --dir docs 选中\n".encode("utf-8")
DOC_TXT_BYTES = "docs.txt 不应被 --dir docs 选中\n".encode("utf-8")
ZH_BYTES = "中文目录内容\n".encode("utf-8")

# 备份完成后对源目录 docs/a.txt 的修改，必须与备份字节明确不同。
DOC_A_MODIFIED_BYTES = "源目录中的 docs/a.txt 已被修改\n".encode("utf-8")

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    DOC_A_REL: DOC_A_BYTES,
    DOC_B_REL: DOC_B_BYTES,
    DOC_OLD_REL: DOC_OLD_BYTES,
    DOC_TXT_REL: DOC_TXT_BYTES,
    ZH_REL: ZH_BYTES,
}

SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"

REASON_DIR_INVALID = "目录选择路径无效"
REASON_DIR_NO_DESCENDANT = "选择的目录未包含快照清单文件"
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
    """递归记录目录树：相对路径 -> (类型, 字节或链接目标)，与遍历顺序无关。"""
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


class RestoreDirSelectionTests(unittest.TestCase):
    """--dir 目录选择恢复的成功路径、并集去重、确定性失败与现场不变性。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-dir-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

        write_files(self.source, BACKUP_FILES)
        # 源目录中的空目录不写入清单，用于“未记录的空目录”用例。
        (self.source / EMPTY_DIR_REL).mkdir(parents=True)

        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 备份后再改动源目录：恢复出旧字节才能证明数据取自快照。
        (self.source / DOC_A_REL).write_bytes(DOC_A_MODIFIED_BYTES)

    # ---- 通用执行与断言 ----

    def run_restore(self, dest, dirs=None, files=None, dry_run=False):
        """以公开入口执行 restore；dirs/files 为 None 时不带对应选项。"""
        argv = ["restore", str(self.snapshot), str(dest)]
        for sel in files or []:
            argv.extend(["--file", sel])
        for sel in dirs or []:
            argv.extend(["--dir", sel])
        if dry_run:
            argv.append("--dry-run")
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
        self, dest, dirs, files, expected_count, expected_tree, label,
    ):
        """成功用例公共流程：预检输出、结果目录树，并核对源目录与快照不变。"""
        self.assertFalse(
            os.path.lexists(dest),
            f"用例前提：恢复目标必须事先不存在: {dest}（{label}）",
        )
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest, dirs=dirs, files=files)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n--dir: {dirs!r}\n--file: {files!r}\n"
            f"目标 DEST: {dest}\n"
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
        self, dest, dirs, files, reasons, label, *, literal=True,
    ):
        """失败用例公共流程：退出码 2、标准输出为空、报错原因、目标不存在，
        且源目录与快照相对调用前保持不变。reasons 为须全部出现的原因片段。
        """
        self.assertFalse(
            os.path.lexists(dest),
            f"用例前提：恢复目标必须事先不存在: {dest}（{label}）",
        )
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_restore(dest, dirs=dirs, files=files)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n--dir: {dirs!r}\n--file: {files!r}\n"
            f"目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少拒绝原因“{reason}”\n{context}",
            )
        if literal and dirs:
            # 拒绝时报错应复述原始目录参数（空串无可复述内容）。
            for sel in dirs:
                if sel != "":
                    self.assertIn(
                        sel, stderr,
                        f"标准错误应逐字复述目录参数: {sel!r}\n{context}",
                    )
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )

        self.assert_ground_unchanged(source_before, snapshot_before, context)
        return stdout, stderr, context

    # ---- 成功：目录选择恰好覆盖全部后代 ----

    def test_dir_selects_all_descendants_only(self):
        """--dir docs：只恢复 docs/a.txt 与 docs/sub/b.bin，不含前缀相似项。"""
        dest = self.work / "restored-docs"
        expected_tree = {
            "docs": ("dir", None),
            DOC_A_REL: ("file", DOC_A_BYTES),
            "docs/sub": ("dir", None),
            DOC_B_REL: ("file", DOC_B_BYTES),
        }
        _, context = self.run_and_assert_success(
            dest, ["docs"], None, 2, expected_tree,
            "目录选择全部后代",
        )
        # 字节必须等于备份时数据，而非源目录中已被修改的当前内容。
        self.assertEqual(
            (dest / DOC_A_REL).read_bytes(), DOC_A_BYTES,
            f"docs/a.txt 未按备份时字节恢复\n{context}",
        )
        self.assertNotEqual(
            DOC_A_BYTES, DOC_A_MODIFIED_BYTES,
            "测试前提：备份字节与修改后字节必须不同",
        )
        bin_bytes = (dest / DOC_B_REL).read_bytes()
        self.assertEqual(bin_bytes, DOC_B_BYTES,
                         f"嵌套二进制文件未按备份字节恢复\n{context}")
        self.assertIn(0, bin_bytes, f"二进制文件应保留零字节\n{context}")

        # 逐字分量匹配：docs-old/a.txt 与 docs.txt 不得被选中。
        self.assertFalse(
            (dest / DOC_OLD_REL).exists(),
            f"docs-old/a.txt 不应被 --dir docs 选中\n{context}",
        )
        self.assertFalse(
            (dest / DOC_TXT_REL).exists(),
            f"docs.txt 不应被 --dir docs 选中\n{context}",
        )
        self.assertFalse(
            (dest / NOTE_REL).exists(),
            f"未选择的 note.txt 不应出现\n{context}",
        )

    def test_dir_with_chinese_and_space(self):
        """--dir 保留中文与空格：逐字匹配中文 目录 下的全部后代。"""
        dest = self.work / "restored-zh"
        expected_tree = {
            ZH_DIR_REL: ("dir", None),
            ZH_REL: ("file", ZH_BYTES),
        }
        self.run_and_assert_success(
            dest, [ZH_DIR_REL], None, 1, expected_tree,
            "中文与空格目录选择",
        )

    # ---- 成功：--dir 与 --file 并集，先预览再实际恢复 ----

    def test_dir_file_union_dry_run_then_restore(self):
        """--dir docs --file note.txt：预览与实际恢复都选择三个文件。"""
        dest = self.work / "new-parent" / "restored-union"
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        # ---- 预览：一行 JSON，files/paths 反映去重结果，不创建目标 ----
        proc = self.run_restore(
            dest, dirs=["docs"], files=[NOTE_REL], dry_run=True,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 目录与单文件并集预览\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )
        self.assertEqual(proc.returncode, 0, f"预览退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"预览标准错误应为空\n{context}")
        self.assertTrue(
            stdout.endswith("\n") and stdout.count("\n") == 1,
            f"预览应恰好输出一行 JSON 加末尾换行\n{context}",
        )
        preview = json.loads(stdout)
        self.assertEqual(
            set(preview.keys()),
            {"snapshot", "destination", "files", "paths"},
            f"预览 JSON 不应新增字段\n{context}",
        )
        self.assertEqual(
            preview["snapshot"], str(self.snapshot.resolve()),
            f"预览 snapshot 应为解析后的快照绝对路径\n{context}",
        )
        self.assertEqual(
            preview["destination"], str(dest.resolve()),
            f"预览 destination 应为解析后的目标绝对路径\n{context}",
        )
        self.assertEqual(preview["files"], 3, f"预览文件数应为 3\n{context}")
        self.assertEqual(
            preview["paths"], [DOC_A_REL, DOC_B_REL, NOTE_REL],
            f"预览 paths 应为去重后按 Unicode 码点升序的三个路径\n{context}",
        )
        self.assertEqual(
            preview["paths"], sorted(preview["paths"]),
            f"预览 paths 应按 Unicode 码点升序排列\n{context}",
        )
        # 预览不创建目标及其父目录。
        self.assertFalse(
            os.path.lexists(dest), f"预览不应创建目标\n{context}",
        )
        self.assertFalse(
            os.path.lexists(dest.parent),
            f"预览不应创建目标的父目录\n{context}",
        )
        self.assert_ground_unchanged(source_before, snapshot_before, context)

        # ---- 实际恢复：同样三个文件，字节与快照一致 ----
        # 预览不创建父目录；实际恢复前由本用例自行备好父目录。
        dest.parent.mkdir(parents=True)
        expected_tree = {
            "docs": ("dir", None),
            DOC_A_REL: ("file", DOC_A_BYTES),
            "docs/sub": ("dir", None),
            DOC_B_REL: ("file", DOC_B_BYTES),
            NOTE_REL: ("file", NOTE_BYTES),
        }
        self.run_and_assert_success(
            dest, ["docs"], [NOTE_REL], 3, expected_tree,
            "目录与单文件并集实际恢复",
        )

    # ---- 成功：重复目录、父子目录重叠与重复文件只恢复一次 ----

    def test_duplicate_and_overlapping_selections_deduped(self):
        """重复 --dir、父子目录重叠及与 --file 重复：并集只恢复一次。"""
        dest = self.work / "restored-dedup"
        expected_tree = {
            "docs": ("dir", None),
            DOC_A_REL: ("file", DOC_A_BYTES),
            "docs/sub": ("dir", None),
            DOC_B_REL: ("file", DOC_B_BYTES),
        }
        stdout, context = self.run_and_assert_success(
            dest,
            ["docs", "docs", "docs/sub"],
            [DOC_A_REL],
            2, expected_tree,
            "重复与重叠选择去重",
        )
        self.assertEqual(
            stdout.count("已恢复文件数:"), 1,
            f"文件数摘要应恰好出现一次\n{context}",
        )

    # ---- 成功：目录选择仅依据清单，不依赖源目录与 data/ 额外文件 ----

    def test_dir_selection_uses_manifest_only(self):
        """删除源目录并向 data/ 塞入未列入清单的文件后，--dir 仍按清单恢复。"""
        extra_data = self.snapshot / "data" / "docs" / "extra.txt"
        extra_data.write_bytes(b"not in manifest\n")
        self.assertTrue(extra_data.is_file(), "测试前提：额外文件已写入 data/")

        # 源目录对目录选择毫无影响：整体删除后恢复仍应成功。
        shutil.rmtree(self.source)
        self.assertFalse(self.source.exists(), "测试前提：源目录已删除")

        snapshot_before = capture_tree(self.snapshot)
        dest = self.work / "restored-manifest-only"
        proc = self.run_restore(dest, dirs=["docs"])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 目录选择仅依据清单\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )
        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已恢复文件数: 2", stdout, f"应只恢复两个清单文件\n{context}")
        expected_tree = {
            "docs": ("dir", None),
            DOC_A_REL: ("file", DOC_A_BYTES),
            "docs/sub": ("dir", None),
            DOC_B_REL: ("file", DOC_B_BYTES),
        }
        self.assertEqual(
            capture_tree(dest), expected_tree,
            f"恢复结果应只含清单收录的后代文件\n{context}",
        )
        self.assertFalse(
            (dest / "docs" / "extra.txt").exists(),
            f"data/ 中未列入清单的额外文件不应被恢复\n{context}",
        )
        self.assertEqual(
            capture_tree(self.snapshot), snapshot_before,
            f"恢复后快照目录树发生变化\n{context}",
        )

    # ---- 成功对照：不带 --dir 与 --file 恢复全部文件 ----

    def test_restore_without_dir_or_file_restores_all(self):
        """不带 --dir 与 --file：仍恢复清单中的全部文件，行为不变。"""
        dest = self.work / "restored-all"
        expected_tree = {
            "docs": ("dir", None),
            DOC_A_REL: ("file", DOC_A_BYTES),
            "docs/sub": ("dir", None),
            DOC_B_REL: ("file", DOC_B_BYTES),
            "docs-old": ("dir", None),
            DOC_OLD_REL: ("file", DOC_OLD_BYTES),
            DOC_TXT_REL: ("file", DOC_TXT_BYTES),
            NOTE_REL: ("file", NOTE_BYTES),
            ZH_DIR_REL: ("dir", None),
            ZH_REL: ("file", ZH_BYTES),
        }
        self.run_and_assert_success(
            dest, None, None, 6, expected_tree,
            "不带选择恢复全部",
        )

    # ---- 失败：形态合法但没有任何清单后代 ----

    def test_reject_dir_without_manifest_descendant(self):
        """清单中不存在的目录：按“选择的目录未包含快照清单文件”拒绝。"""
        dest = self.work / "restored-nosuch"
        self.run_and_assert_rejected(
            dest, ["no-such-dir"], None,
            [REASON_DIR_NO_DESCENDANT],
            "清单中不存在的目录",
        )

    def test_reject_unrecorded_empty_dir(self):
        """源目录存在但清单未记录的空目录：同样按无清单后代拒绝。"""
        self.assertTrue(
            (self.source / EMPTY_DIR_REL).is_dir(),
            "测试前提：源目录中的空目录应存在",
        )
        dest = self.work / "restored-emptydir"
        self.run_and_assert_rejected(
            dest, [EMPTY_DIR_REL], None,
            [REASON_DIR_NO_DESCENDANT],
            "未记录的空目录",
        )

    def test_reject_dir_that_names_a_file(self):
        """目录参数指向清单中的文件（docs.txt）：没有后代，整体拒绝。"""
        dest = self.work / "restored-filedir"
        self.run_and_assert_rejected(
            dest, [DOC_TXT_REL], None,
            [REASON_DIR_NO_DESCENDANT],
            "目录参数指向文件",
        )

    def test_reject_one_bad_dir_fails_whole_restore(self):
        """任一目录不匹配即整体失败：合法目录也不产生部分恢复。"""
        dest = self.work / "restored-partial"
        # 报错只复述不匹配的目录参数，故关闭逐字复述核对并显式核对。
        self.run_and_assert_rejected(
            dest, ["docs", "no-such-dir"], None,
            [REASON_DIR_NO_DESCENDANT, "no-such-dir"],
            "合法目录与不匹配目录混用",
            literal=False,
        )

    def test_reject_dir_with_missing_file_selection(self):
        """与 --file 合用时，不存在的单文件仍按原有拒绝结果整体失败。"""
        dest = self.work / "restored-badfile"
        self.run_and_assert_rejected(
            dest, ["docs"], ["no-such-file.txt"],
            ["选择的路径未在快照清单中", "no-such-file.txt"],
            "目录合用不存在的单文件",
            literal=False,
        )

    # ---- 失败：非法目录形态 ----

    def test_reject_invalid_dir_forms(self):
        """空串、绝对路径、盘符、反斜杠、空/./.. 分量：统一按无效拒绝。"""
        invalid = [
            "",
            "/docs",
            "C:/docs",
            "C:docs",
            "docs\\sub",
            "docs//sub",
            "docs/./a",
            "docs/../a",
            "./docs",
            "../docs",
            ".",
            "..",
        ]
        for index, raw in enumerate(invalid):
            with self.subTest(raw=raw):
                dest = self.work / f"restored-invalid-{index}"
                self.run_and_assert_rejected(
                    dest, [raw], None,
                    [REASON_DIR_INVALID],
                    f"非法目录形态 {raw!r}",
                )

    # ---- 失败：未选中文件的数据缺失仍整体失败 ----

    def test_reject_when_unselected_data_missing(self):
        """删除快照中的 docs.txt 后只选 docs 目录：整单校验失败。"""
        missing_data = self.snapshot / "data" / DOC_TXT_REL
        self.assertTrue(
            missing_data.is_file(),
            "测试前提：docs.txt 的快照数据应事先存在",
        )
        missing_data.unlink()
        self.assertFalse(missing_data.exists())

        dest = self.work / "restored-damaged"
        # 该报错来自整单校验，不复述目录参数，故关闭逐字复述核对。
        self.run_and_assert_rejected(
            dest, ["docs"], None,
            [REASON_DATA_MISSING, DOC_TXT_REL],
            "未选择数据缺失",
            literal=False,
        )

    # ---- 失败：预览同样执行目录校验，不创建目标及其父目录 ----

    def test_dry_run_reject_invalid_dir_creates_nothing(self):
        """--dry-run 下非法目录与无后代目录同样拒绝，且不创建目标父目录。"""
        dest = self.work / "new-parent" / "restored-preview"
        for dirs, reasons in (
            (["/docs"], [REASON_DIR_INVALID]),
            (["no-such-dir"], [REASON_DIR_NO_DESCENDANT]),
        ):
            with self.subTest(dirs=dirs):
                source_before = capture_tree(self.source)
                snapshot_before = capture_tree(self.snapshot)
                proc = self.run_restore(dest, dirs=dirs, dry_run=True)
                stdout = proc.stdout.decode("utf-8", errors="replace")
                stderr = proc.stderr.decode("utf-8", errors="replace")
                context = (
                    f"用例: 预览拒绝 {dirs!r}\n"
                    f"exit={proc.returncode}\n"
                    f"stdout={stdout!r}\nstderr={stderr!r}"
                )
                self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
                self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
                for reason in reasons:
                    self.assertIn(reason, stderr,
                                  f"标准错误缺少“{reason}”\n{context}")
                self.assertFalse(
                    os.path.lexists(dest.parent),
                    f"预览失败不应创建目标的父目录\n{context}",
                )
                self.assert_ground_unchanged(
                    source_before, snapshot_before, context,
                )


if __name__ == "__main__":
    unittest.main()
