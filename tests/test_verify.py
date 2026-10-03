#!/usr/bin/env python3
"""verify 只读校验的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum]
    python backup.py verify SNAPSHOT

只观察退出码、标准输出、标准错误与文件原始字节，不调用 backup.py 的
任何内部校验函数，也不以修改产品行为来使断言成立。仅依赖 Python 3
标准库；全部源目录与快照在独立临时目录中运行时准备，用例结束自动
清理，不读写用户现有目录，不依赖网络、第三方库或管理员权限。

非空夹具快照由公开 backup 命令从三个普通文件生成：

- ``note.txt``：明确的 UTF-8 文本；
- ``嵌套 目录/二进制 文件.bin``：嵌套目录下含零字节（0x00）的二进制；
- ``空文件.dat``：零字节文件。

覆盖约定：

1. 成功用例：无摘要旧快照（3/0/3）、全部带摘要快照（3/3/0）、仅空文件
   缺省 sha256 的混合快照（3/2/1）、空清单快照（0/0/0）。成功时退出码
   为 0，标准错误为空，标准输出只有一个可解析的 JSON 对象，snapshot
   等于快照解析后的绝对路径。计数只涉及清单引用的文件：额外放入 data
   的未引用普通文件不增加计数。
2. 失败用例（每个样例只引入一种错误）：清单 JSON 损坏、清单引用的数据
   缺失、sha256 为 null、内容与合法摘要不一致。退出码均为 2，标准输出
   为空，标准错误分别包含 JSON、数据缺失、摘要格式错误或摘要校验不
   一致；后三种还包含对应相对路径。错误项排在一个有效项之后时，也不
   输出局部成功结果。
3. 所有成功与失败用例都比较命令执行前后的源目录与快照：相对路径集合、
   条目类型与普通文件字节一致，没有新增报告、恢复目录或其他文件；
   读取引起的访问时间变化不纳入比较。
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
EMPTY_REL = "空文件.dat"

# 备份时的固定内容：明确文本、含零字节的二进制、零字节文件。
NOTE_BYTES = "note.txt 的原始内容\n第二行\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x7F, 0x00, 0x80, 0x00])
EMPTY_BYTES = b""

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

# 公开 JSON 结果中应恰好出现的键。
RESULT_KEYS = {"snapshot", "files", "verified", "unchecked"}

ERROR_PREFIX = "错误"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_JSON = "JSON"
REASON_MISSING = "数据缺失"
REASON_BAD_FORMAT = "摘要格式错误"
REASON_MISMATCH = "摘要校验不一致"


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

    只记录路径集合、条目类型与普通文件字节，不记录访问时间等元数据，
    因此读取引起的 atime 变化不影响比较。
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


def sha256_hex(data):
    """用标准库计算字节的 SHA-256，作为与实现无关的公开预期值。"""
    return hashlib.sha256(data).hexdigest()


class VerifyTests(unittest.TestCase):
    """verify 只读校验的成功计数、失败拒绝与无副作用约定。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录与快照相互独立，样例之间互不污染。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

    # ---- 通用工具与断言 ----

    def make_snapshot(self, files=BACKUP_FILES, checksum=False):
        """用公开 backup 命令生成夹具快照；失败则中止本用例。"""
        self.source.mkdir(parents=True, exist_ok=True)
        write_files(self.source, files)
        argv = ["backup", str(self.source), str(self.snapshot)]
        if checksum:
            argv.append("--checksum")
        proc = run_cmd(argv)
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

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

    def run_verify(self):
        """以公开入口执行 verify SNAPSHOT。"""
        return run_cmd(["verify", str(self.snapshot)])

    def assert_ground_unchanged(self, source_before, snapshot_before, context):
        """verify（无论成败）不得改动源目录与快照的路径集合、类型与字节，
        也不得新增报告、恢复目录或其他文件。"""
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"verify 后源目录目录树发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(self.snapshot), snapshot_before,
            f"verify 后快照目录树发生变化\n{context}",
        )
        self.assertEqual(
            sorted(p.name for p in self.work.iterdir()),
            ["snapshot", "source"],
            f"verify 后工作目录出现新增条目\n{context}",
        )

    def run_and_assert_success(self, expected_counts, label):
        """成功用例公共流程：退出码 0、标准错误为空、标准输出为单个 JSON
        对象且计数与快照路径符合预期，源目录与快照保持不变。"""
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_verify()
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {self.snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")

        # 标准输出整体必须恰好是一个可解析的 JSON 对象（单行）。
        self.assertEqual(
            len(stdout.splitlines()), 1,
            f"标准输出应只有一行 JSON\n{context}",
        )
        try:
            result = json.loads(stdout)
        except json.JSONDecodeError as exc:
            self.fail(f"标准输出不是可解析的 JSON: {exc}\n{context}")
        self.assertIsInstance(result, dict, f"JSON 结果应为对象\n{context}")
        self.assertEqual(
            set(result), RESULT_KEYS,
            f"JSON 结果应恰好包含 {sorted(RESULT_KEYS)}\n{context}",
        )
        self.assertEqual(
            result["snapshot"], str(self.snapshot.resolve()),
            f"snapshot 应等于快照解析后的绝对路径\n{context}",
        )
        for key, expected in expected_counts.items():
            self.assertEqual(
                result[key], expected,
                f"计数 {key} 应为 {expected}\n{context}",
            )

        self.assert_ground_unchanged(source_before, snapshot_before, context)
        return result, context

    def run_and_assert_rejected(self, reasons, label):
        """失败用例公共流程：退出码 2、标准输出为空（无局部成功结果）、
        标准错误包含全部原因片段，源目录与快照相对调用前保持不变。"""
        # 基线在调用 verify 前一瞬间拍摄（损坏样例已在更早完成破坏）。
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(self.snapshot)

        proc = self.run_verify()
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {self.snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少拒绝原因“{reason}”\n{context}",
            )
        self.assertEqual(
            stdout, "",
            f"失败后标准输出应为空，不得输出局部成功结果\n{context}",
        )

        self.assert_ground_unchanged(source_before, snapshot_before, context)
        return stdout, stderr, context

    # ---- 成功：无摘要旧快照 ----

    def test_verify_snapshot_without_checksums(self):
        """不带 --checksum 生成的旧快照：三个文件均无摘要，3/0/3。"""
        self.make_snapshot(checksum=False)
        manifest = self.read_manifest()
        self.assertEqual(
            len(manifest["files"]), 3, "测试前提：清单应包含三个条目",
        )
        for item in manifest["files"]:
            self.assertNotIn(
                "sha256", item, "测试前提：旧快照条目不应带 sha256 字段",
            )

        self.run_and_assert_success(
            {"files": 3, "verified": 0, "unchecked": 3},
            "无摘要旧快照",
        )

    # ---- 成功：全部带摘要的快照 ----

    def test_verify_snapshot_all_checksummed(self):
        """--checksum 快照：三个文件摘要均合法且匹配，3/3/0。"""
        self.make_snapshot(checksum=True)
        manifest = self.read_manifest()
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

        self.run_and_assert_success(
            {"files": 3, "verified": 3, "unchecked": 0},
            "全部带摘要的快照",
        )

    # ---- 成功：混合摘要快照 ----

    def test_verify_mixed_manifest(self):
        """仅空文件条目缺省 sha256：其余两条保留合法摘要，3/2/1。"""
        self.make_snapshot(checksum=True)
        manifest = self.read_manifest()
        removed = False
        for item in manifest["files"]:
            if item["path"] == EMPTY_REL:
                del item["sha256"]
                removed = True
            else:
                # 其余条目必须仍带合法摘要：本样例是“混合”而非“全无摘要”。
                self.assertIn("sha256", item, "测试前提：其余条目应保留摘要")
        self.assertTrue(removed, "测试前提：清单应包含空文件条目")
        self.write_manifest(manifest)

        self.run_and_assert_success(
            {"files": 3, "verified": 2, "unchecked": 1},
            "仅空文件缺省 sha256 的混合快照",
        )

    # ---- 成功：空快照 ----

    def test_verify_empty_snapshot(self):
        """空源目录生成的空清单快照：三个计数均为 0。"""
        self.make_snapshot(files={}, checksum=True)
        manifest = self.read_manifest()
        self.assertEqual(manifest["files"], [], "测试前提：清单应为空数组")

        self.run_and_assert_success(
            {"files": 0, "verified": 0, "unchecked": 0},
            "空清单快照",
        )

    # ---- 成功：未引用的 data 文件不计数 ----

    def test_verify_ignores_unreferenced_data_files(self):
        """额外放入 data 的未引用普通文件不增加任何计数，仍为 3/3/0。"""
        self.make_snapshot(checksum=True)
        extra_rel = "额外 未引用.bin"
        write_files(self.snapshot / "data", {extra_rel: b"unreferenced"})
        self.assertTrue((self.snapshot / "data" / extra_rel).is_file())

        self.run_and_assert_success(
            {"files": 3, "verified": 3, "unchecked": 0},
            "data 中含未引用文件的快照",
        )

    # ---- 失败：清单 JSON 损坏 ----

    def test_reject_corrupt_manifest_json(self):
        """清单不是合法 JSON：退出码 2，标准错误包含 JSON。"""
        self.make_snapshot(checksum=True)
        (self.snapshot / "manifest.json").write_bytes(
            "{ 这不是合法 JSON".encode("utf-8")
        )

        self.run_and_assert_rejected(
            [REASON_JSON],
            "清单 JSON 损坏",
        )

    # ---- 失败：清单引用的数据缺失 ----

    def test_reject_missing_data_file(self):
        """删除清单引用的嵌套二进制数据文件：退出码 2，标准错误包含
        数据缺失及该相对路径。"""
        self.make_snapshot(checksum=True)
        (self.snapshot / "data" / BIN_REL).unlink()

        self.run_and_assert_rejected(
            [REASON_MISSING, BIN_REL],
            "清单引用的数据缺失",
        )

    # ---- 失败：sha256 为 null ----

    def test_reject_sha256_null(self):
        """note.txt 条目的 sha256 显式为 null：格式错误，不等同于字段
        缺省；标准错误包含摘要格式错误及该相对路径。"""
        self.make_snapshot(checksum=True)
        manifest = self.read_manifest()
        replaced = False
        for item in manifest["files"]:
            if item["path"] == NOTE_REL:
                item["sha256"] = None
                replaced = True
        self.assertTrue(replaced, "测试前提：清单应包含 note.txt 条目")
        self.write_manifest(manifest)

        self.run_and_assert_rejected(
            [REASON_BAD_FORMAT, NOTE_REL],
            "sha256 为 null",
        )

    # ---- 失败：内容与合法摘要不一致 ----

    def test_reject_checksum_mismatch(self):
        """只改变 note.txt 的快照数据字节、清单保留合法摘要：退出码 2，
        标准错误包含摘要校验不一致及该相对路径。"""
        self.make_snapshot(checksum=True)
        data_file = self.snapshot / "data" / NOTE_REL
        original = data_file.read_bytes()
        self.assertTrue(original, "测试前提：note.txt 应为非空文件")
        corrupted = bytes([original[0] ^ 0xFF]) + original[1:]
        self.assertNotEqual(corrupted, original)
        data_file.write_bytes(corrupted)

        self.run_and_assert_rejected(
            [REASON_MISMATCH, NOTE_REL],
            "内容与合法摘要不一致",
        )

    # ---- 失败：错误项排在有效项之后也不输出局部成功结果 ----

    def test_reject_error_after_valid_entries_no_partial_output(self):
        """清单按路径排序后空文件条目排在两个有效条目之后；只破坏该末位
        条目的数据使其与合法摘要不一致，verify 仍整体失败且标准输出为空，
        不输出任何局部成功结果。"""
        self.make_snapshot(checksum=True)
        manifest = self.read_manifest()
        paths = [item["path"] for item in manifest["files"]]
        self.assertEqual(
            paths[-1], EMPTY_REL,
            "测试前提：空文件条目应排在两个有效条目之后",
        )
        # 空文件原内容为零字节，写入新字节即与清单中的合法摘要不一致。
        (self.snapshot / "data" / EMPTY_REL).write_bytes(b"corrupted")

        self.run_and_assert_rejected(
            [REASON_MISMATCH, EMPTY_REL],
            "错误项排在有效项之后",
        )


if __name__ == "__main__":
    unittest.main()
