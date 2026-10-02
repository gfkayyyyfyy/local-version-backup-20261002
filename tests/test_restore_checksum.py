#!/usr/bin/env python3
"""restore 对 --checksum 快照摘要校验的可重复回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT --checksum
    python backup.py restore SNAPSHOT DEST [--file PATH]...

只观察退出码、标准输出、标准错误与文件原始字节，不调用任何内部校验
函数；清单本身也仅作为快照产物按原始 JSON 读取核对。仅依赖 Python 3
标准库；全部源目录、快照与恢复目标在独立临时目录中运行时准备，互不
嵌套，恢复目标的父目录预先存在而目标本身调用前不存在，用例结束自动
清理，不依赖公网、管理员权限或文件权限差异。

夹具源目录始终包含三个文件：

- ``note.txt``：明确的 UTF-8 文本；
- ``嵌套 目录/二进制 文件.bin``：嵌套中文目录下含零字节（0x00）的二进制；
- ``空文件.dat``：零字节文件（其摘要是空字节串的 SHA-256）。

成功覆盖：

1. ``backup --checksum`` 生成的清单为三个条目记录 64 位小写十六进制
   摘要，零字节文件的摘要固定为 e3b0c4…b855；全量恢复退出码 0、
   标准错误为空、标准输出含目标绝对路径与“已恢复文件数: 3”，恢复
   路径集合与字节逐字等于快照数据。
2. ``--file note.txt`` 只恢复一个文件：退出码 0、标准错误为空、
   输出含目标绝对路径与“已恢复文件数: 1”，字节准确。
3. 同一版本 1 清单中摘要条目与完全省略 ``sha256`` 字段的条目并存时，
   全量恢复成功，不把缺省摘要误判为格式错误。

失败覆盖（每种样例只引入一种错误，其余数据保持有效）：

4. 只改变快照数据字节、保留合法摘要：被改动文件分别位于 ``--file``
   选中与未选中位置，均退出码 2，标准错误含“摘要校验不一致”及被
   改动文件的相对路径；未选中文件同样参与整单校验。
5. ``sha256`` 分别为 null、整数、空字符串、长度不是 64、含大写
   字母、含非十六进制字符：均退出码 2，标准错误含“摘要格式错误”
   及对应相对路径；其中大写字母样例放在 ``--file`` 未选中的文件上。

所有失败样例：标准输出为空、恢复目标不被创建；源目录与调用前快照的
路径集合、条目类型和文件字节保持不变。
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

# 夹具中的三个相对路径（斜杠分隔，目录名含空格，与 README 示例一致）。
NOTE_REL = "note.txt"
BIN_DIR_REL = "嵌套 目录"
BIN_REL = "嵌套 目录/二进制 文件.bin"
EMPTY_REL = "空文件.dat"

# 固定内容：明确文本、含零字节的二进制、零字节文件。
NOTE_BYTES = "note.txt 的原文\n第二行\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x7F, 0x00, 0x80, 0x00])
EMPTY_BYTES = b""

FIXTURE_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

# 空字节串的 SHA-256（README 公开值），用于核对零字节文件的摘要产物。
EMPTY_SHA256 = (
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
)

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_MISMATCH = "摘要校验不一致"
REASON_BAD_FORMAT = "摘要格式错误"

# 字段缺省哨兵：区别于显式 null（null 必须作为格式错误被拒绝）。
_OMIT = object()


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


def expected_tree(files):
    """由 ``相对POSIX路径 -> 字节`` 构造恢复结果应有的完整目录树。"""
    tree = {}
    for rel in files:
        for parent in Path(rel).parents:
            if str(parent) != ".":
                tree[parent.as_posix()] = ("dir", None)
    for rel, data in files.items():
        tree[rel] = ("file", data)
    return tree


class ChecksumRestoreTests(unittest.TestCase):
    """--checksum 快照恢复的成功路径、摘要拒绝与现场不变性。"""

    def setUp(self):
        # 每个用例独立临时工作区，源目录、快照、恢复目标相互独立。
        self._tmp = tempfile.TemporaryDirectory(prefix="checksum-restore-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

        write_files(self.source, FIXTURE_FILES)

        # 夹具快照必须由公开 backup --checksum 命令生成；失败则中止用例。
        proc = run_cmd(
            ["backup", str(self.source), str(self.snapshot), "--checksum"]
        )
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：带 --checksum 的基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 恢复目标父目录预先存在；每个目标本身在用例调用前均不存在。
        self.restore_area = self.work / "restore_area"
        self.restore_area.mkdir()

    # ---- 夹具与清单操作 ----

    def new_dest(self, label):
        """返回父目录已存在、自身尚不存在的恢复目标路径。"""
        dest = self.restore_area / f"restored-{label}"
        self.assertFalse(
            os.path.lexists(dest),
            f"用例前提：恢复目标必须事先不存在: {dest}（{label}）",
        )
        return dest

    def read_manifest(self):
        return json.loads(
            (self.snapshot / "manifest.json").read_text(encoding="utf-8")
        )

    def write_manifest(self, doc):
        (self.snapshot / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def set_entry_checksum(self, rel, value):
        """把清单中 rel 条目的 sha256 改成 value；_OMIT 表示完全省略字段。

        只动这一个字段，其他条目与数据保持原样。
        """
        doc = self.read_manifest()
        matched = False
        for entry in doc["files"]:
            if entry["path"] == rel:
                matched = True
                if value is _OMIT:
                    entry.pop("sha256", None)
                else:
                    entry["sha256"] = value
        self.assertTrue(
            matched, f"测试前提：清单中应存在条目 {rel!r}"
        )
        self.write_manifest(doc)

    def valid_checksum(self, rel):
        """读取夹具清单中某条目当前合法的摘要字符串。"""
        for entry in self.read_manifest()["files"]:
            if entry["path"] == rel:
                return entry["sha256"]
        raise AssertionError(f"测试前提：清单中应存在条目 {rel!r}")

    def tamper_data(self, rel, suffix=b"\x01tampered"):
        """只改变快照数据文件的字节，清单中的合法摘要保持不变。"""
        data_file = self.snapshot / "data" / rel
        original = data_file.read_bytes()
        data_file.write_bytes(original + suffix)
        self.assertNotEqual(
            data_file.read_bytes(), original,
            f"测试前提：篡改后字节必须变化: {rel}",
        )

    def run_restore(self, dest, selections=None):
        """以公开入口执行 restore；selections 为 None 时不带任何 --file。"""
        argv = ["restore", str(self.snapshot), str(dest)]
        if selections is not None:
            for sel in selections:
                argv.extend(["--file", sel])
        return run_cmd(argv)

    # ---- 通用断言 ----

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

    def assert_restore_success(
        self, dest, selections, expected_count, expected_files, label,
    ):
        """成功用例公共流程：预检输出、结果目录树，并核对源目录与快照不变。"""
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
            f"标准输出应包含实际恢复文件数 {expected_count}\n{context}",
        )
        self.assertEqual(
            stdout.count("已恢复文件数:"), 1,
            f"文件数摘要应恰好出现一次\n{context}",
        )

        self.assertTrue(dest.is_dir(), f"恢复后目标应为目录\n{context}")
        self.assertEqual(
            capture_tree(dest), expected_tree(expected_files),
            f"恢复结果目录树（路径集合/类型/字节）与预期不符\n{context}",
        )

        # 恢复字节必须同时逐字等于快照数据与源目录原始字节。
        for rel, data in expected_files.items():
            restored_bytes = (dest / rel).read_bytes()
            self.assertEqual(
                restored_bytes, data,
                f"恢复字节与夹具预期不符: {rel}\n{context}",
            )
            self.assertEqual(
                restored_bytes,
                (self.snapshot / "data" / rel).read_bytes(),
                f"恢复字节与快照数据不一致: {rel}\n{context}",
            )
            self.assertEqual(
                restored_bytes,
                (self.source / rel).read_bytes(),
                f"恢复字节与源目录原始字节不一致: {rel}\n{context}",
            )

        self.assert_ground_unchanged(source_before, snapshot_before, context)
        return stdout, context

    def assert_restore_rejected(
        self, dest, selections, reasons, label, forbidden=(),
    ):
        """失败用例公共流程。

        退出码 2；标准错误包含全部 reasons（含被改动文件的相对路径）且
        不含 forbidden 片段；标准输出为空；恢复目标不存在；源目录与
        调用前快照的路径集合、类型与字节保持不变。
        """
        # 基线在调用前一瞬间拍摄（损坏样例已在更早完成唯一一处破坏）。
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
        for text in forbidden:
            self.assertNotIn(
                text, stderr,
                f"标准错误不应出现“{text}”\n{context}",
            )
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        self.assertEqual(
            proc.stdout, b"", f"失败时标准输出应为空\n{context}",
        )
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )

        self.assert_ground_unchanged(source_before, snapshot_before, context)
        return stderr, context

    # ---- 成功：--checksum 产物 ----

    def test_checksum_backup_records_valid_digests(self):
        """--checksum 清单为三个文件记录 64 位小写十六进制摘要。"""
        doc = self.read_manifest()
        label = "核对 --checksum 清单产物"
        context = f"用例: {label}\n清单: {doc!r}"

        self.assertEqual(doc.get("version"), 1, f"版本应为 1\n{context}")
        entries = doc.get("files")
        self.assertEqual(
            sorted(item["path"] for item in entries),
            sorted(FIXTURE_FILES),
            f"清单路径集合应与源目录一致\n{context}",
        )

        hex_digits = set("0123456789abcdef")
        by_path = {item["path"]: item for item in entries}
        for rel in FIXTURE_FILES:
            entry = by_path[rel]
            digest = entry.get("sha256")
            self.assertIsInstance(
                digest, str, f"{rel} 的 sha256 应为字符串\n{context}",
            )
            self.assertEqual(
                len(digest), 64, f"{rel} 的摘要长度应为 64\n{context}",
            )
            self.assertTrue(
                set(digest) <= hex_digits,
                f"{rel} 的摘要应只含小写十六进制字符\n{context}",
            )

        # 零字节文件按空字节串计算（README 公开值）。
        self.assertEqual(
            by_path[EMPTY_REL]["sha256"], EMPTY_SHA256,
            f"零字节文件摘要应为空字节串的 SHA-256\n{context}",
        )

        # 数据区文件原始字节与源目录逐字一致。
        for rel, data in FIXTURE_FILES.items():
            self.assertEqual(
                (self.snapshot / "data" / rel).read_bytes(), data,
                f"快照数据字节与源目录不一致: {rel}\n{context}",
            )

    # ---- 成功：全量恢复 ----

    def test_restore_all_files_with_checksums(self):
        """带摘要快照全量恢复：退出码 0、路径与字节准确、现场不变。"""
        dest = self.new_dest("all")
        _, context = self.assert_restore_success(
            dest, None, 3, FIXTURE_FILES, "带摘要全量恢复",
        )

        # 零字节文件必须作为普通空文件恢复，而不是被跳过或写成目录。
        empty_path = dest / EMPTY_REL
        self.assertTrue(
            empty_path.is_file(), f"零字节文件应恢复为普通文件\n{context}",
        )
        self.assertEqual(
            empty_path.stat().st_size, 0,
            f"零字节文件恢复后大小应为 0\n{context}",
        )
        # 嵌套二进制中的零字节逐字保留。
        bin_bytes = (dest / BIN_DIR_REL / "二进制 文件.bin").read_bytes()
        self.assertIn(0, bin_bytes, f"二进制文件应保留零字节\n{context}")

    # ---- 成功：--file 仅恢复一个 ----

    def test_restore_single_file_with_file_option(self):
        """--file note.txt：只恢复一个文件，计数为 1，字节准确。"""
        dest = self.new_dest("single")
        _, context = self.assert_restore_success(
            dest, [NOTE_REL], 1, {NOTE_REL: NOTE_BYTES},
            "--file 仅恢复 note.txt",
        )
        # 未选中的两个文件不应出现。
        self.assertFalse(
            (dest / BIN_REL).exists(),
            f"未选择的嵌套二进制文件不应出现\n{context}",
        )
        self.assertFalse(
            (dest / EMPTY_REL).exists(),
            f"未选择的零字节文件不应出现\n{context}",
        )
        self.assertEqual(
            sorted(
                key for key, entry in capture_tree(dest).items()
                if entry[0] == "file"
            ),
            [NOTE_REL],
            f"目标应恰好含一个被选择文件\n{context}",
        )

    # ---- 成功：有摘要与无摘要条目并存 ----

    def test_mixed_manifest_with_and_without_checksum(self):
        """版本 1 清单中完全省略某条目 sha256：仍成功恢复所有文件。"""
        # note.txt 完全省略 sha256 字段；另外两个条目保留合法摘要。
        self.set_entry_checksum(NOTE_REL, _OMIT)
        doc = self.read_manifest()
        by_path = {item["path"]: item for item in doc["files"]}
        self.assertNotIn(
            "sha256", by_path[NOTE_REL],
            "测试前提：note.txt 条目应完全省略 sha256 字段",
        )
        self.assertIn("sha256", by_path[BIN_REL])
        self.assertIn("sha256", by_path[EMPTY_REL])

        dest = self.new_dest("mixed")
        stdout, context = self.assert_restore_success(
            dest, None, 3, FIXTURE_FILES, "摘要与无摘要条目并存",
        )
        # 缺省摘要不得被误判为格式错误（成功时 stderr 已断言为空，
        # 这里再固定 stdout 也不出现任何错误字样）。
        self.assertNotIn(
            REASON_BAD_FORMAT, stdout,
            f"缺省摘要不应被报为格式错误\n{context}",
        )
        # 省略摘要的文件同样逐字节恢复。
        self.assertEqual(
            (dest / NOTE_REL).read_bytes(), NOTE_BYTES,
            f"无摘要条目 note.txt 未按原始字节恢复\n{context}",
        )

    # ---- 失败：数据字节与合法摘要不一致 ----

    def test_reject_tampered_data_of_selected_file(self):
        """改动 --file 选中文件（note.txt）的快照字节：摘要不一致，退出码 2。"""
        self.tamper_data(NOTE_REL)
        dest = self.new_dest("tamper-selected")
        self.assert_restore_rejected(
            dest, [NOTE_REL],
            [REASON_MISMATCH, NOTE_REL],
            "篡改选中文件数据",
            forbidden=[REASON_BAD_FORMAT],
        )

    def test_reject_tampered_data_of_unselected_file(self):
        """改动未选中文件的快照字节：整单校验仍失败，并指出该文件路径。"""
        self.tamper_data(BIN_REL)
        dest = self.new_dest("tamper-unselected")
        self.assert_restore_rejected(
            dest, [NOTE_REL],
            [REASON_MISMATCH, BIN_REL],
            "篡改未选中文件数据",
            forbidden=[REASON_BAD_FORMAT, NOTE_REL],
        )

    # ---- 失败：sha256 字段形态错误（每种样例只引入一种错误）----

    def test_reject_null_checksum(self):
        """sha256 显式为 null：格式错误，区别于字段缺省。"""
        self.set_entry_checksum(BIN_REL, None)
        dest = self.new_dest("null")
        self.assert_restore_rejected(
            dest, None, [REASON_BAD_FORMAT, BIN_REL],
            "sha256 为 null", forbidden=[REASON_MISMATCH],
        )

    def test_reject_integer_checksum(self):
        """sha256 为整数：格式错误。"""
        self.set_entry_checksum(BIN_REL, 12345)
        dest = self.new_dest("integer")
        self.assert_restore_rejected(
            dest, None, [REASON_BAD_FORMAT, BIN_REL],
            "sha256 为整数", forbidden=[REASON_MISMATCH],
        )

    def test_reject_empty_string_checksum(self):
        """sha256 为空字符串：格式错误。"""
        self.set_entry_checksum(BIN_REL, "")
        dest = self.new_dest("empty-string")
        self.assert_restore_rejected(
            dest, None, [REASON_BAD_FORMAT, BIN_REL],
            "sha256 为空字符串", forbidden=[REASON_MISMATCH],
        )

    def test_reject_wrong_length_checksum(self):
        """sha256 只含合法十六进制字符但长度为 63：格式错误。"""
        self.set_entry_checksum(BIN_REL, "a" * 63)
        dest = self.new_dest("wrong-length")
        self.assert_restore_rejected(
            dest, None, [REASON_BAD_FORMAT, BIN_REL],
            "sha256 长度不是 64", forbidden=[REASON_MISMATCH],
        )

    def test_reject_uppercase_checksum(self):
        """sha256 含大写字母（64 字符）：格式错误。"""
        self.set_entry_checksum(BIN_REL, self.valid_checksum(BIN_REL).upper())
        dest = self.new_dest("uppercase")
        self.assert_restore_rejected(
            dest, None, [REASON_BAD_FORMAT, BIN_REL],
            "sha256 含大写字母", forbidden=[REASON_MISMATCH],
        )

    def test_reject_non_hex_char_checksum(self):
        """sha256 含非十六进制字符 g：格式错误。"""
        self.set_entry_checksum(BIN_REL, "g" + "0" * 63)
        dest = self.new_dest("non-hex")
        self.assert_restore_rejected(
            dest, None, [REASON_BAD_FORMAT, BIN_REL],
            "sha256 含非十六进制字符", forbidden=[REASON_MISMATCH],
        )

    def test_reject_malformed_checksum_on_unselected_file(self):
        """格式错误位于 --file 未选中文件：整单校验失败并指出其路径。"""
        # 选择完好的 note.txt；大写字母摘要挂在未选中的嵌套二进制上。
        self.set_entry_checksum(BIN_REL, self.valid_checksum(BIN_REL).upper())
        dest = self.new_dest("format-unselected")
        self.assert_restore_rejected(
            dest, [NOTE_REL],
            [REASON_BAD_FORMAT, BIN_REL],
            "未选中文件的 sha256 含大写字母",
            forbidden=[REASON_MISMATCH, NOTE_REL],
        )


if __name__ == "__main__":
    unittest.main()
