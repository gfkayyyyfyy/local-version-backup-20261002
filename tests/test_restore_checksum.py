#!/usr/bin/env python3
"""restore 对快照 SHA-256 摘要校验的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT --checksum
    python backup.py restore SNAPSHOT DEST [--file PATH]...

只观察退出码、标准输出、标准错误与文件原始字节，不调用 backup.py 的
任何内部校验函数。仅依赖 Python 3 标准库；全部源目录、快照与恢复目标
在独立临时目录中运行时准备，父目录预先存在而恢复目标在调用前不存在，
用例结束自动清理，不读写用户现有目录，不依赖网络、管理员权限或文件
权限差异。

夹具快照由公开 backup --checksum 命令从三个普通文件生成：

- ``note.txt``：明确的 UTF-8 文本；
- ``嵌套 目录/二进制 文件.bin``：嵌套目录下含零字节（0x00）的二进制；
- ``空文件.bin``：零字节文件。

覆盖约定：

1. 带摘要快照的成功恢复：恢复全部文件与 ``--file note.txt`` 仅恢复一个
   文件均返回退出码 0、标准错误为空，标准输出包含目标绝对路径与实际
   恢复文件数，恢复结果的路径与字节准确；源目录与调用前快照保持不变。
2. 同一版本 1 清单中有摘要与无摘要条目并存：完全省略某条目的 sha256
   字段后仍能成功恢复全部文件，无摘要条目不被误判为格式错误。
3. 摘要校验不一致在目标创建前阻止整个恢复：分别改动选中文件与未选中
   文件的快照数据（只变字节、清单保留合法摘要），restore 均返回退出码
   2，标准错误包含“摘要校验不一致”及被改动文件的相对路径。
4. 清单 sha256 字段格式错误逐一覆盖：null、整数、空字符串、长度不是
   64、含大写字母、含非十六进制字符；均返回退出码 2，标准错误包含
   “摘要格式错误”及对应相对路径，其中至少一个格式错误位于 --file 未
   选中的文件。

每个失败样例只引入一种错误，其他数据保持有效；失败后标准输出为空、
恢复目标不存在，源目录与调用前快照的路径集合和文件字节均不变。
"""

import hashlib
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

# 夹具中的三个相对路径（斜杠分隔，目录名含空格，与 README 示例一致）。
NOTE_REL = "note.txt"
BIN_DIR_REL = "嵌套 目录"
BIN_REL = "嵌套 目录/二进制 文件.bin"
EMPTY_REL = "空文件.bin"

# 备份时的固定内容：明确文本、含零字节的二进制、零字节文件。
NOTE_BYTES = "note.txt 的原始内容\n第二行\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x7F, 0x00, 0x80, 0x00])
EMPTY_BYTES = b""

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_MISMATCH = "摘要校验不一致"
REASON_BAD_FORMAT = "摘要格式错误"


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


def sha256_hex(data):
    """用标准库计算字节的 SHA-256，作为与实现无关的公开预期值。"""
    return hashlib.sha256(data).hexdigest()


class RestoreChecksumTests(unittest.TestCase):
    """带摘要快照的成功恢复、摘要不一致与摘要格式错误的确定性结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照与恢复目标相互独立，
        # 样例之间互不污染。
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-checksum-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

        write_files(self.source, BACKUP_FILES)

        # 夹具快照必须由公开 backup --checksum 命令生成；失败则中止本用例。
        proc = run_cmd(
            ["backup", str(self.source), str(self.snapshot), "--checksum"]
        )
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：带摘要快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 夹具前提：清单为版本 1，且每个条目都带有合法 sha256 字段。
        manifest = self.read_manifest()
        self.assertEqual(manifest["version"], 1, "测试前提：清单版本应为 1")
        entries = {item["path"]: item for item in manifest["files"]}
        self.assertEqual(
            set(entries), set(BACKUP_FILES),
            "测试前提：清单应恰好包含夹具中的三个文件",
        )
        for rel, data in BACKUP_FILES.items():
            self.assertEqual(
                entries[rel].get("sha256"), sha256_hex(data),
                f"测试前提：{rel} 的清单摘要应等于标准库计算结果",
            )

    # ---- 通用工具与断言 ----

    def read_manifest(self):
        """读取快照清单（JSON 是公开快照格式的一部分）。"""
        return json.loads(
            (self.snapshot / "manifest.json").read_text(encoding="utf-8")
        )

    def write_manifest(self, doc):
        """整体重写快照清单，用于在样例中引入唯一一种错误。"""
        (self.snapshot / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def corrupt_data(self, rel):
        """只改变快照中某文件的数据字节，清单中的合法摘要保持不变。"""
        data_file = self.snapshot / "data" / rel
        original = data_file.read_bytes()
        self.assertTrue(original, f"测试前提：{rel} 应为非空文件")
        corrupted = bytes([original[0] ^ 0xFF]) + original[1:]
        self.assertNotEqual(corrupted, original)
        data_file.write_bytes(corrupted)

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
        self, dest, selections, reasons, label,
    ):
        """失败用例公共流程：退出码 2、报错原因、标准输出为空、目标不存在，
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
        self.assertEqual(stdout, "", f"失败后标准输出应为空\n{context}")
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )

        self.assert_ground_unchanged(source_before, snapshot_before, context)
        return stdout, stderr, context

    def reject_bad_sha256(self, rel, bad_value, label, selections=None):
        """把清单中 rel 条目的 sha256 替换为唯一一种非法值后验证整体拒绝。"""
        manifest = self.read_manifest()
        replaced = False
        for item in manifest["files"]:
            if item["path"] == rel:
                item["sha256"] = bad_value
                replaced = True
        self.assertTrue(replaced, f"测试前提：清单应包含条目 {rel}")
        self.write_manifest(manifest)

        dest = self.work / "restored"
        self.run_and_assert_rejected(
            dest, selections,
            [REASON_BAD_FORMAT, rel],
            label,
        )

    # ---- 成功：带摘要快照恢复全部文件 ----

    def test_restore_all_with_checksum(self):
        """带摘要快照恢复全部三个文件：路径与字节准确，计数为 3。"""
        dest = self.work / "restored-all"
        expected_tree = {
            NOTE_REL: ("file", NOTE_BYTES),
            BIN_DIR_REL: ("dir", None),
            BIN_REL: ("file", BINARY_BYTES),
            EMPTY_REL: ("file", EMPTY_BYTES),
        }
        stdout, context = self.run_and_assert_success(
            dest, None, 3, expected_tree,
            "带摘要恢复全部文件",
        )
        self.assertEqual(
            stdout.count("已恢复文件数:"), 1,
            f"文件数摘要应恰好出现一次\n{context}",
        )
        # 显式固定公开语义：文本、嵌套二进制与零字节文件逐字节保留。
        self.assertEqual(
            (dest / NOTE_REL).read_bytes(), NOTE_BYTES,
            f"note.txt 字节不准确\n{context}",
        )
        bin_bytes = (dest / BIN_DIR_REL / "二进制 文件.bin").read_bytes()
        self.assertEqual(bin_bytes, BINARY_BYTES, f"二进制文件字节不准确\n{context}")
        self.assertIn(0, bin_bytes, f"二进制文件应保留零字节\n{context}")
        self.assertEqual(
            (dest / EMPTY_REL).read_bytes(), b"",
            f"零字节文件应恢复为空文件\n{context}",
        )

    # ---- 成功：--file 仅恢复一个文件 ----

    def test_restore_single_file_with_checksum(self):
        """--file note.txt 仅恢复一个文件：计数为 1，其余文件不出现。"""
        dest = self.work / "restored-one"
        expected_tree = {NOTE_REL: ("file", NOTE_BYTES)}
        _, context = self.run_and_assert_success(
            dest, [NOTE_REL], 1, expected_tree,
            "--file 仅恢复 note.txt",
        )
        self.assertFalse(
            (dest / EMPTY_REL).exists(),
            f"未选择的零字节文件不应出现\n{context}",
        )
        self.assertFalse(
            (dest / BIN_DIR_REL).exists(),
            f"未选择的嵌套目录不应出现\n{context}",
        )

    # ---- 成功：有摘要与无摘要条目并存 ----

    def test_restore_mixed_manifest_with_and_without_sha256(self):
        """完全省略零字节文件条目的 sha256 字段：其余条目保留合法摘要，
        恢复全部文件仍成功，无摘要条目不被误判为格式错误。"""
        manifest = self.read_manifest()
        removed = False
        for item in manifest["files"]:
            if item["path"] == EMPTY_REL:
                del item["sha256"]
                removed = True
            else:
                # 其余条目必须仍带合法摘要：本样例是“并存”而非“全无摘要”。
                self.assertIn("sha256", item, "测试前提：其余条目应保留摘要")
        self.assertTrue(removed, "测试前提：清单应包含零字节文件条目")
        self.write_manifest(manifest)

        dest = self.work / "restored-mixed"
        expected_tree = {
            NOTE_REL: ("file", NOTE_BYTES),
            BIN_DIR_REL: ("dir", None),
            BIN_REL: ("file", BINARY_BYTES),
            EMPTY_REL: ("file", EMPTY_BYTES),
        }
        self.run_and_assert_success(
            dest, None, 3, expected_tree,
            "有/无摘要条目并存恢复全部",
        )

    # ---- 失败：摘要校验不一致（只变数据字节，清单保留合法摘要）----

    def test_reject_mismatch_on_selected_file(self):
        """改动被选中的 note.txt 的快照数据：恢复前整体拒绝并指出该路径。"""
        self.corrupt_data(NOTE_REL)
        dest = self.work / "restored-mismatch-selected"
        self.run_and_assert_rejected(
            dest, [NOTE_REL],
            [REASON_MISMATCH, NOTE_REL],
            "选中文件摘要校验不一致",
        )

    def test_reject_mismatch_on_unselected_file(self):
        """改动未被选择的嵌套二进制文件的快照数据：即使只选 note.txt，
        整单校验仍失败并指出被改动文件的相对路径。"""
        self.corrupt_data(BIN_REL)
        dest = self.work / "restored-mismatch-unselected"
        self.run_and_assert_rejected(
            dest, [NOTE_REL],
            [REASON_MISMATCH, BIN_REL],
            "未选中文件摘要校验不一致",
        )

    # ---- 失败：sha256 字段格式错误（每例只引入一种错误）----

    def test_reject_sha256_null(self):
        """sha256 显式为 null：格式错误，不等同于字段缺省。"""
        self.reject_bad_sha256(NOTE_REL, None, "sha256 为 null")

    def test_reject_sha256_integer(self):
        """sha256 为整数：类型不符。"""
        self.reject_bad_sha256(NOTE_REL, 123, "sha256 为整数")

    def test_reject_sha256_empty_string(self):
        """sha256 为空字符串：长度不符。"""
        self.reject_bad_sha256(NOTE_REL, "", "sha256 为空字符串")

    def test_reject_sha256_wrong_length(self):
        """sha256 为 63 位小写十六进制：长度不是 64。"""
        self.reject_bad_sha256(
            NOTE_REL, sha256_hex(NOTE_BYTES)[:-1], "sha256 长度不是 64",
        )

    def test_reject_sha256_uppercase(self):
        """sha256 全大写：含大写字母，不属于合法小写十六进制。"""
        # 64 位但全为大写字母：必然含大写字符，不属于合法小写十六进制。
        self.reject_bad_sha256(
            NOTE_REL, "AB" * 32, "sha256 含大写字母",
        )

    def test_reject_sha256_non_hex_char(self):
        """sha256 含非十六进制字符 g。"""
        bad = "g" + sha256_hex(NOTE_BYTES)[1:]
        self.reject_bad_sha256(NOTE_REL, bad, "sha256 含非十六进制字符")

    def test_reject_sha256_bad_format_on_unselected_file(self):
        """格式错误位于 --file 未选中的文件：只选 note.txt 时，嵌套二进制
        条目的非法摘要仍导致整单拒绝并指出该条目路径。"""
        self.reject_bad_sha256(
            BIN_REL, None, "未选中文件 sha256 格式错误",
            selections=[NOTE_REL],
        )


if __name__ == "__main__":
    unittest.main()
