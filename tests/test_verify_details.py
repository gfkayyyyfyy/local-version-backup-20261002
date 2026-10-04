#!/usr/bin/env python3
"""verify --details 逐文件明细的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum]
    python backup.py verify SNAPSHOT [--details]

只观察退出码、标准输出、标准错误与目录内容，不调用 backup.py 的任何
内部校验函数。仅依赖 Python 3 标准库；全部源目录与快照在独立临时目录
中运行时准备，用例结束自动清理，不读写用户现有目录，不依赖网络、
第三方库或管理员权限。

夹具源数据由公开 backup 命令生成，含三个普通文件：

- ``note.txt``：文本内容 ``old`` 加换行；
- ``嵌套 目录/二进制 文件.bin``：嵌套目录下的二进制字节 00 FF 10 00；
- ``空文件.dat``：零字节文件。

覆盖约定：

1. 成功用例（退出码 0、标准错误为空、标准输出只有一个可解析 JSON 对象，
   其中 snapshot 为快照解析后的绝对路径）：
   - 混合摘要清单（--checksum 快照中仅空文件条目移除 sha256，且清单顺序
     被打乱）：files/verified/unchecked=3/2/1；entries 恰好三个条目，
     按路径的 Unicode 码点升序排列，与清单顺序无关；path 逐字保留中文、
     空格与斜杠；note.txt 与嵌套二进制为 verified，空文件为 unchecked；
   - data 中额外放入未被清单引用的普通文件后，明细与计数保持不变；
   - 同一快照不传 --details：保留相同汇总字段但不含 entries；
   - 无摘要旧快照（不带 --checksum 备份）：3/0/3，三个条目均为 unchecked；
   - 空源目录生成的空快照：entries 为空数组且三个计数均为 0。
2. 失败用例（明细不得掩盖校验失败；每例只引入一种错误，且错误条目显式
   排在一个有效条目之后）：
   - 删除有效条目之后那个条目的数据文件：退出码 2、标准输出完全为空、
     不输出局部明细，标准错误包含“数据缺失”及对应相对路径；
   - 只修改有效条目之后那个条目的数据字节并保留合法摘要：退出码 2、
     标准输出完全为空，标准错误包含“摘要校验不一致”及对应相对路径。
3. 所有用例都比较 verify 前后的源目录、快照与整个临时工作区：相对路径
   集合、条目类型与普通文件原始字节一致，没有修改、删除或新增内容
   （读取可能引起的访问时间变化不参与比较）。
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

# 夹具中的三个相对路径（斜杠分隔，目录名含空格与中文）。
NOTE_REL = "note.txt"
BIN_REL = "嵌套 目录/二进制 文件.bin"
EMPTY_REL = "空文件.dat"

# 备份时的固定内容：文本 old 加换行、二进制 00 FF 10 00、零字节文件。
NOTE_BYTES = b"old\n"
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x00])
EMPTY_BYTES = b""

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

# 按路径 Unicode 码点升序（Python 字符串默认比较即码点序）的预期顺序。
# 首字符码点：n U+006E < 嵌 U+5D4C < 空 U+7A7A，即
# "note.txt" < "嵌套 目录/二进制 文件.bin" < "空文件.dat"。
SORTED_RELS = sorted(BACKUP_FILES)
assert SORTED_RELS == [NOTE_REL, BIN_REL, EMPTY_REL]

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_MISSING = "数据缺失"
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

    只记录条目类型与普通文件字节，不记录访问时间等读取易变属性。
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


class VerifyDetailsTests(unittest.TestCase):
    """verify --details 逐文件明细在成功与失败下的确定性公开结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照及任何意外产物都受限于此，
        # 用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-details-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"

        write_files(self.source, BACKUP_FILES)

    # ---- 通用工具与断言 ----

    def make_snapshot(self, name, source=None, checksum=False):
        """用公开 backup 命令在工作区内新建快照，失败则中止本用例。"""
        source = source if source is not None else self.source
        snapshot = self.work / name
        argv = ["backup", str(source), str(snapshot)]
        if checksum:
            argv.append("--checksum")
        proc = run_cmd(argv)
        if proc.returncode != 0 or not snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )
        return snapshot

    def read_manifest(self, snapshot):
        """读取快照清单（JSON 是公开快照格式的一部分）。"""
        return json.loads(
            (snapshot / "manifest.json").read_text(encoding="utf-8")
        )

    def write_manifest(self, snapshot, doc):
        """整体重写快照清单，用于在样例中引入唯一一种改动。"""
        (snapshot / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def make_mixed_shuffled_snapshot(self, name):
        """--checksum 快照：仅移除空文件条目的 sha256，并打乱清单顺序。

        返回快照路径。打乱采用确定性的逆序重排，并断言重排后确实与
        码点升序不同，保证后续明细排序断言与清单顺序无关。
        """
        snapshot = self.make_snapshot(name, checksum=True)
        manifest = self.read_manifest(snapshot)
        entries = manifest["files"]

        # 测试前提：起点是三个都带合法摘要的条目。
        self.assertEqual(
            sorted(item["path"] for item in entries), SORTED_RELS,
            "测试前提：清单应恰好包含三个相对路径",
        )
        for item in entries:
            self.assertIn("sha256", item, "测试前提：条目应带 sha256 字段")
            self.assertEqual(
                item["sha256"], sha256_hex(BACKUP_FILES[item["path"]]),
                "测试前提：摘要应与公开字节匹配",
            )

        removed = False
        for item in entries:
            if item["path"] == EMPTY_REL:
                del item["sha256"]
                removed = True
        self.assertTrue(removed, "测试前提：清单应包含空文件条目")

        shuffled = list(reversed(entries))
        self.assertNotEqual(
            [item["path"] for item in shuffled], SORTED_RELS,
            "测试前提：打乱后的清单顺序应与码点升序不同",
        )
        manifest["files"] = shuffled
        self.write_manifest(snapshot, manifest)
        return snapshot

    def order_entries(self, snapshot, first_rel, bad_rel):
        """重排清单：先放有效条目 first_rel，紧随错误条目 bad_rel，其余在后。

        用于显式构造“错误项排在一个有效项之后”的前提。
        """
        manifest = self.read_manifest(snapshot)
        entries = manifest["files"]
        by_path = {item["path"]: item for item in entries}
        self.assertIn(first_rel, by_path, f"测试前提：清单应包含 {first_rel}")
        self.assertIn(bad_rel, by_path, f"测试前提：清单应包含 {bad_rel}")
        rest = [
            item for item in entries
            if item["path"] not in (first_rel, bad_rel)
        ]
        manifest["files"] = [by_path[first_rel], by_path[bad_rel], *rest]
        self.write_manifest(snapshot, manifest)

    def run_verify(self, snapshot, details=True):
        """以公开入口执行 verify（默认带 --details）。"""
        argv = ["verify", str(snapshot)]
        if details:
            argv.append("--details")
        return run_cmd(argv)

    def assert_work_intact(self, source_before, snapshot, snapshot_before,
                           work_before, context):
        """verify 不得改动源目录、快照，也不得在工作区新增任何文件。"""
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"verify 后源目录目录树发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(snapshot), snapshot_before,
            f"verify 后快照目录树发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"verify 后工作区出现新增/改动/删除（报告、恢复目录等）\n{context}",
        )

    def capture_baselines(self, snapshot):
        """记录 verify 前源目录、快照与整个工作区的目录树基准。"""
        return (
            capture_tree(self.source),
            capture_tree(snapshot),
            capture_tree(self.work),
        )

    def run_and_parse_success(self, snapshot, label):
        """成功用例公共流程：退出码 0、stderr 空、stdout 仅一个结果 JSON，
        且源目录、快照与整个工作区相对调用前保持不变。"""
        source_before, snapshot_before, work_before = \
            self.capture_baselines(snapshot)

        proc = self.run_verify(snapshot, details=True)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")

        # 标准输出有且仅有一个可解析的 JSON 对象（允许结尾换行）。
        nonempty_lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(
            len(nonempty_lines), 1,
            f"标准输出应只有一个 JSON 对象\n{context}",
        )
        result = json.loads(nonempty_lines[0])
        self.assertIsInstance(result, dict, f"输出应为 JSON 对象\n{context}")

        self.assert_work_intact(
            source_before, snapshot, snapshot_before, work_before, context,
        )
        return result, context

    def assert_summary(self, result, snapshot, expected_files,
                       expected_verified, expected_unchecked, context):
        """核对汇总字段：snapshot 为解析后的绝对路径，三个计数符合预期。"""
        self.assertEqual(
            result["snapshot"], str(snapshot.resolve()),
            f"snapshot 应为快照解析后的绝对路径\n{context}",
        )
        self.assertEqual(
            result["files"], expected_files, f"files 计数不符\n{context}",
        )
        self.assertEqual(
            result["verified"], expected_verified,
            f"verified 计数不符\n{context}",
        )
        self.assertEqual(
            result["unchecked"], expected_unchecked,
            f"unchecked 计数不符\n{context}",
        )

    def assert_details_failure(self, snapshot, reasons, label):
        """失败用例公共流程：退出码 2、stdout 完全为空（不输出局部明细）、
        stderr 含全部原因片段，且源目录、快照与工作区保持不变。"""
        source_before, snapshot_before, work_before = \
            self.capture_baselines(snapshot)

        proc = self.run_verify(snapshot, details=True)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            stdout, "",
            f"失败时标准输出应完全为空，不得输出局部明细\n{context}",
        )
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少原因“{reason}”\n{context}",
            )

        self.assert_work_intact(
            source_before, snapshot, snapshot_before, work_before, context,
        )
        return stdout, stderr, context

    # ---- 成功：混合摘要且清单顺序被打乱 ----

    def test_details_mixed_shuffled_manifest(self):
        """混合摘要 + 打乱清单：3/2/1；entries 按码点升序、与清单顺序无关，
        path 逐字保留中文/空格/斜杠，空文件为 unchecked。"""
        snapshot = self.make_mixed_shuffled_snapshot("snap-mixed")

        result, context = self.run_and_parse_success(
            snapshot, "混合摘要且清单打乱",
        )

        self.assertEqual(
            set(result),
            {"snapshot", "files", "verified", "unchecked", "entries"},
            f"--details 输出 JSON 字段集合不符\n{context}",
        )
        self.assert_summary(result, snapshot, 3, 2, 1, context)

        expected_entries = [
            {"path": NOTE_REL, "status": "verified"},
            {"path": BIN_REL, "status": "verified"},
            {"path": EMPTY_REL, "status": "unchecked"},
        ]
        # 测试前提：预期顺序确为路径的 Unicode 码点升序。
        self.assertEqual(
            [item["path"] for item in expected_entries], SORTED_RELS,
            "测试前提：预期明细顺序应为码点升序",
        )
        self.assertEqual(
            result["entries"], expected_entries,
            f"entries 应恰好三个条目、按码点升序且状态符合预期\n{context}",
        )
        # path 逐字保留中文、空格与斜杠（JSON 解析后与原字符串完全相等）。
        self.assertEqual(
            [item["path"] for item in result["entries"]],
            sorted([NOTE_REL, BIN_REL, EMPTY_REL]),
            f"path 应逐字保留相对路径\n{context}",
        )
        for item in result["entries"]:
            self.assertEqual(
                set(item), {"path", "status"},
                f"每个明细条目应只有 path 与 status 字段\n{context}",
            )

    # ---- 成功：data 中额外的未引用普通文件不影响明细与计数 ----

    def test_details_unreferenced_extra_file_ignored(self):
        """data 中放入未被清单引用的普通文件后，明细与计数保持不变。"""
        snapshot = self.make_mixed_shuffled_snapshot("snap-extra")

        extra = snapshot / "data" / "未引用 的报告.txt"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes("不在清单里，不应出现在明细或计数中\n".encode("utf-8"))

        result, context = self.run_and_parse_success(
            snapshot, "data 含未引用普通文件",
        )
        self.assert_summary(result, snapshot, 3, 2, 1, context)
        self.assertEqual(
            result["entries"],
            [
                {"path": NOTE_REL, "status": "verified"},
                {"path": BIN_REL, "status": "verified"},
                {"path": EMPTY_REL, "status": "unchecked"},
            ],
            f"未引用文件不应改变明细\n{context}",
        )

    # ---- 成功：同一快照不传 --details 时保留汇总但不含 entries ----

    def test_same_snapshot_without_details_has_no_entries(self):
        """同一混合快照不传 --details：相同汇总字段，且不含 entries。"""
        snapshot = self.make_mixed_shuffled_snapshot("snap-plain")

        source_before, snapshot_before, work_before = \
            self.capture_baselines(snapshot)

        proc = self.run_verify(snapshot, details=False)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 同一快照不传 --details\n快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        nonempty_lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(
            len(nonempty_lines), 1,
            f"标准输出应只有一个 JSON 对象\n{context}",
        )
        result = json.loads(nonempty_lines[0])
        self.assertEqual(
            set(result),
            {"snapshot", "files", "verified", "unchecked"},
            f"不传 --details 时不应包含 entries 字段\n{context}",
        )
        self.assertNotIn(
            "entries", result, f"不传 --details 时不应输出明细\n{context}",
        )
        self.assert_summary(result, snapshot, 3, 2, 1, context)

        self.assert_work_intact(
            source_before, snapshot, snapshot_before, work_before, context,
        )

    # ---- 成功：无摘要旧快照的三个条目均为 unchecked ----

    def test_details_old_snapshot_all_unchecked(self):
        """不带 --checksum 的旧快照：3/0/3，三个条目状态均为 unchecked。"""
        snapshot = self.make_snapshot("snap-old", checksum=False)
        manifest = self.read_manifest(snapshot)
        # 测试前提：旧清单中任何条目都不带 sha256 字段。
        self.assertTrue(
            all("sha256" not in item for item in manifest["files"]),
            "测试前提：旧快照不应有任何条目带 sha256 字段",
        )

        result, context = self.run_and_parse_success(
            snapshot, "无摘要旧快照",
        )
        self.assert_summary(result, snapshot, 3, 0, 3, context)
        self.assertEqual(
            result["entries"],
            [
                {"path": NOTE_REL, "status": "unchecked"},
                {"path": BIN_REL, "status": "unchecked"},
                {"path": EMPTY_REL, "status": "unchecked"},
            ],
            f"旧快照三个条目应均为 unchecked 且按码点升序\n{context}",
        )

    # ---- 成功：空快照的明细为空数组 ----

    def test_details_empty_snapshot(self):
        """空源目录备份出的空清单：entries 为空数组且三个计数均为 0。"""
        empty_source = self.work / "empty-source"
        empty_source.mkdir()
        snapshot = self.make_snapshot(
            "snap-empty", source=empty_source, checksum=True,
        )
        manifest = self.read_manifest(snapshot)
        self.assertEqual(
            manifest["files"], [], "测试前提：空快照清单应为空数组",
        )

        result, context = self.run_and_parse_success(snapshot, "空快照")
        self.assert_summary(result, snapshot, 0, 0, 0, context)
        self.assertEqual(
            result["entries"], [],
            f"空快照的 entries 应为空数组\n{context}",
        )

    # ---- 失败：明细不得掩盖数据缺失 ----

    def test_details_rejects_missing_referenced_data(self):
        """错误条目（数据文件被删）排在一个有效条目之后：退出码 2、
        标准输出完全为空（无局部明细），stderr 含“数据缺失”与该路径。"""
        snapshot = self.make_snapshot("snap-missing", checksum=True)
        missing_file = snapshot / "data" / EMPTY_REL
        self.assertTrue(
            missing_file.is_file(), "测试前提：被删数据在破坏前应存在",
        )
        # 显式让一个有效条目排在错误条目之前。
        self.order_entries(snapshot, NOTE_REL, EMPTY_REL)
        missing_file.unlink()

        self.assert_details_failure(
            snapshot, [REASON_MISSING, EMPTY_REL], "引用数据缺失",
        )

    # ---- 失败：明细不得掩盖摘要校验不一致 ----

    def test_details_rejects_checksum_mismatch(self):
        """只改动错误条目的数据字节、清单保留合法摘要，且该条目排在有效
        条目之后：退出码 2、标准输出完全为空，stderr 含“摘要校验不一致”
        与该相对路径。"""
        snapshot = self.make_snapshot("snap-mismatch", checksum=True)
        self.order_entries(snapshot, NOTE_REL, BIN_REL)

        data_file = snapshot / "data" / BIN_REL
        original = data_file.read_bytes()
        self.assertEqual(
            original, BINARY_BYTES, "测试前提：被改动文件应为公开二进制样例",
        )
        corrupted = bytes([original[0] ^ 0xFF]) + original[1:]
        self.assertNotEqual(corrupted, original)
        data_file.write_bytes(corrupted)

        # 清单中的摘要仍是原始字节的合法摘要：本样例唯一错误是字节不一致。
        manifest = self.read_manifest(snapshot)
        for item in manifest["files"]:
            if item["path"] == BIN_REL:
                self.assertEqual(
                    item["sha256"], sha256_hex(original),
                    "测试前提：清单应保留改动前的合法摘要",
                )

        self.assert_details_failure(
            snapshot, [REASON_MISMATCH, BIN_REL], "摘要校验不一致",
        )


if __name__ == "__main__":
    unittest.main()
