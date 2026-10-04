#!/usr/bin/env python3
"""``verify --details`` 逐文件明细的可独立执行回归测试。

只走 README 公开命令，且仅观察退出码、标准输出、标准错误与目录内容，
不调用 backup.py 的任何内部校验函数：

    python backup.py backup SOURCE SNAPSHOT --checksum
    python backup.py verify SNAPSHOT [--details]

仅依赖 Python 3 标准库；演示源目录、快照及任何意外产物都在独立临时
目录中运行时准备（快照由现有 backup 命令生成），用例结束随临时目录
一并清理，不读写用户现有目录，不依赖网络、第三方库或管理员权限。

固定演示目录（源目录）含三个普通文件：

- ``note.txt``：文本，内容为 ``old\\n``（old 加换行，共 4 字节）；
- ``嵌套 目录/二进制 文件.bin``：嵌套目录（目录名含空格）下的二进制，
  字节固定为 ``00 FF 10 00``；
- ``空文件.dat``：零字节空文件。

成功预期（先以 --checksum 创建快照，再仅移除空文件条目的 sha256，
并打乱清单顺序）：退出码 0、标准错误为空、标准输出只有一个 JSON 对象；
snapshot 为快照解析后的绝对路径；files/verified/unchecked = 3/2/1；
entries 恰好三个条目，按路径的 Unicode 码点升序排列，与清单顺序无关；
path 逐字保留中文、空格与斜杠；前两个文件 status=verified，
空文件 status=unchecked。data 中额外放入未被清单引用的普通文件时，
明细与计数保持不变；同一快照不传 --details 时保留相同汇总但不含
entries。无摘要旧快照三个条目均为 unchecked；空快照明细为空数组、
三个计数均为 0。

失败预期（均从有效快照出发，错误条目显式排在一个有效条目之后）：
删除错误条目的数据文件，或只修改其字节而保留合法摘要，带 --details
校验时两例都退出码 2、标准输出完全为空（不输出任何局部明细），
标准错误分别包含“数据缺失”或“摘要校验不一致”及对应相对路径。

每次校验前后都核对源目录、快照与整个临时工作区的相对路径集合、
条目类型与普通文件字节（符号链接记录其链接目标），确认没有修改、
删除或新增内容；读取可能引起的访问时间变化不参与比较。
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

# 演示目录中的三个相对路径（斜杠分隔，目录名含空格与中文）。
NOTE_REL = "note.txt"
BIN_REL = "嵌套 目录/二进制 文件.bin"
EMPTY_REL = "空文件.dat"
ALL_RELS = [NOTE_REL, BIN_REL, EMPTY_REL]

# 固定夹具字节：old 加换行；00 FF 10 00；零字节空文件。
NOTE_BYTES = b"old\n"
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x00])
EMPTY_BYTES = b""

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

# 按路径的 Unicode 码点升序（Python 字符串默认比较即码点序）。
CODEPOINT_ORDER = [NOTE_REL, BIN_REL, EMPTY_REL]

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
    """递归记录目录树：相对 POSIX 路径 -> (类型, 字节或链接目标)。

    类型为 "dir" / "file" / "symlink"；只记录条目类型、普通文件字节与
    符号链接目标，不记录访问时间等读取易变属性，与遍历顺序无关。
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
    """verify --details 在成功与失败下的确定性公开结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照及任何意外产物都受限于此，
        # 用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-details-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"

        write_files(self.source, BACKUP_FILES)

    # ---- 通用工具与断言 ----

    def make_snapshot(self, name, source=None, checksum=True):
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
        """整体重写快照清单，用于在样例中引入受控改动。"""
        (snapshot / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def make_mixed_snapshot(self, name, shuffled_order):
        """以 --checksum 创建有效快照，再仅移除空文件条目的 sha256，
        并按 shuffled_order 重排清单条目。"""
        snapshot = self.make_snapshot(name, checksum=True)
        manifest = self.read_manifest(snapshot)

        by_path = {}
        removed = False
        for item in manifest["files"]:
            rel = item["path"]
            self.assertIn(rel, ALL_RELS, "测试前提：清单应恰好包含三个固定路径")
            if rel == EMPTY_REL:
                self.assertIn("sha256", item, "测试前提：空文件条目本应带摘要")
                del item["sha256"]
                removed = True
            else:
                self.assertEqual(
                    item.get("sha256"), sha256_hex(BACKUP_FILES[rel]),
                    f"测试前提：{rel} 的摘要应与其字节匹配",
                )
            by_path[rel] = item
        self.assertTrue(removed, "测试前提：清单应包含空文件条目")

        self.assertEqual(
            sorted(shuffled_order), sorted(ALL_RELS),
            "测试前提：打乱顺序只能重排，不得增删条目",
        )
        manifest["files"] = [by_path[rel] for rel in shuffled_order]
        self.write_manifest(snapshot, manifest)

        # 明确测试前提：清单物理顺序确实不同于码点升序，否则无法证明
        # entries 排序与清单顺序无关。
        self.assertNotEqual(
            shuffled_order, CODEPOINT_ORDER,
            "测试前提：清单顺序应已被打乱为非码点升序",
        )
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

    def run_verify(self, snapshot, details):
        """以公开入口执行 verify，按需带 --details。"""
        argv = ["verify", str(snapshot)]
        if details:
            argv.append("--details")
        return run_cmd(argv)

    def parse_single_json_stdout(self, stdout, context):
        """成功用例标准输出有且仅有一个可解析的 JSON 对象（允许结尾换行）。"""
        text = stdout.decode("utf-8", errors="replace")
        nonempty_lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(
            len(nonempty_lines), 1,
            f"标准输出应只有一个 JSON 对象\n{context}",
        )
        result = json.loads(nonempty_lines[0])
        self.assertIsInstance(result, dict, f"输出应为 JSON 对象\n{context}")
        return result

    def assert_trees_unchanged(self, snapshot, before_triple, context):
        """verify 不得改动源目录、快照，也不得在工作区新增任何文件。"""
        source_before, snapshot_before, work_before = before_triple
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"verify 后源目录路径集合/类型/字节发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(snapshot), snapshot_before,
            f"verify 后快照路径集合/类型/字节发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"verify 后工作区出现新增/改动/删除（报告、恢复目录等）\n{context}",
        )

    def assert_details_success(self, snapshot, expected_counts, expected_entries,
                               label, details=True):
        """成功公共流程：退出码 0、stderr 完全为空、stdout 仅一个结果 JSON，
        汇总与明细符合预期，且源目录、快照与整个工作区调用前后完全一致。"""
        before = (
            capture_tree(self.source),
            capture_tree(snapshot),
            capture_tree(self.work),
        )

        proc = self.run_verify(snapshot, details)
        stdout_text = proc.stdout.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(proc.stderr, b"", f"成功时标准错误应为空\n{context}")

        result = self.parse_single_json_stdout(proc.stdout, context)
        files, verified, unchecked = expected_counts
        expected_keys = {"snapshot", "files", "verified", "unchecked"}
        if details:
            expected_keys.add("entries")
        self.assertEqual(set(result), expected_keys, f"输出 JSON 字段集合不符\n{context}")
        self.assertEqual(
            result["snapshot"], str(snapshot.resolve()),
            f"snapshot 应为快照解析后的绝对路径\n{context}",
        )
        self.assertEqual(result["files"], files, f"files 计数不符\n{context}")
        self.assertEqual(result["verified"], verified, f"verified 计数不符\n{context}")
        self.assertEqual(
            result["unchecked"], unchecked, f"unchecked 计数不符\n{context}",
        )
        if details:
            self.assertEqual(
                result["entries"], expected_entries,
                f"entries 明细不符（应恰好三条且按码点升序）\n{context}",
            )

        self.assert_trees_unchanged(snapshot, before, context)
        return result, stdout_text

    def assert_details_failure(self, snapshot, reasons, label):
        """失败公共流程（带 --details）：退出码 2、stdout 完全为空（不输出
        任何局部明细）、stderr 含全部原因片段；调用前后三处目录树完全一致。

        目录树基准在篡改完成之后采集，专门证明只读 verify 本身没有修改、
        删除或新增任何内容。
        """
        before = (
            capture_tree(self.source),
            capture_tree(snapshot),
            capture_tree(self.work),
        )

        proc = self.run_verify(snapshot, details=True)
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            proc.stdout, b"",
            f"失败后标准输出必须完全为空，不得输出局部明细\n{context}",
        )
        stderr = proc.stderr.decode("utf-8", errors="replace")
        for reason in reasons:
            self.assertIn(reason, stderr, f"标准错误缺少原因“{reason}”\n{context}")

        self.assert_trees_unchanged(snapshot, before, context)

    def expected_entry(self, rel, status):
        """明细条目恰有 path 与 status 两个字段。"""
        return {"path": rel, "status": status}

    # ---- 成功：混合摘要 + 打乱清单顺序后的完整明细 ----

    def test_details_mixed_manifest_sorted_regardless_of_order(self):
        """仅空文件缺省 sha256、清单顺序被打乱：3/2/1；entries 恰好三条，
        按路径 Unicode 码点升序，path 逐字保留中文/空格/斜杠。"""
        # 码点序为 note.txt < 嵌套… < 空文件…；故意排成“空、note、嵌套”。
        snapshot = self.make_mixed_snapshot(
            "snap-mixed", [EMPTY_REL, NOTE_REL, BIN_REL],
        )

        # 再次确认固定夹具字节没有被准备环节改动。
        self.assertEqual((self.source / NOTE_REL).read_bytes(), b"old\n")
        self.assertEqual(
            (self.source / BIN_REL).read_bytes(), bytes([0x00, 0xFF, 0x10, 0x00]),
        )
        self.assertEqual((self.source / EMPTY_REL).read_bytes(), b"")

        expected_entries = [
            self.expected_entry(NOTE_REL, "verified"),
            self.expected_entry(BIN_REL, "verified"),
            self.expected_entry(EMPTY_REL, "unchecked"),
        ]
        self.assertEqual(
            CODEPOINT_ORDER, sorted(ALL_RELS),
            "测试前提：预期顺序必须等于码点升序",
        )
        self.assert_details_success(
            snapshot, (3, 2, 1), expected_entries,
            "混合摘要且清单乱序的明细",
        )

    # ---- 成功：data 中额外的未引用普通文件不影响明细与计数 ----

    def test_details_ignores_unreferenced_data_file(self):
        """data 内额外放入清单未引用的普通文件（含子目录）：明细仍恰好三条，
        3/2/1 保持不变。"""
        snapshot = self.make_mixed_snapshot(
            "snap-extra", [BIN_REL, EMPTY_REL, NOTE_REL],
        )

        extra = snapshot / "data" / "额外 目录" / "未引用 文件.txt"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes("不被任何清单条目引用\n".encode("utf-8"))

        expected_entries = [
            self.expected_entry(NOTE_REL, "verified"),
            self.expected_entry(BIN_REL, "verified"),
            self.expected_entry(EMPTY_REL, "unchecked"),
        ]
        self.assert_details_success(
            snapshot, (3, 2, 1), expected_entries,
            "data 含未引用普通文件",
        )

    # ---- 成功：不传 --details 时汇总相同但不含 entries ----

    def test_verify_without_details_keeps_summary_without_entries(self):
        """同一混合快照不带 --details：snapshot/files/verified/unchecked
        与带明细时相同，但输出中不存在 entries 字段。"""
        snapshot = self.make_mixed_snapshot(
            "snap-no-details", [EMPTY_REL, BIN_REL, NOTE_REL],
        )
        result, _ = self.assert_details_success(
            snapshot, (3, 2, 1), None,
            "混合快照不带 --details", details=False,
        )
        self.assertNotIn(
            "entries", result,
            "不带 --details 时不应输出 entries 字段",
        )

    # ---- 成功：无摘要旧快照三个状态均为 unchecked ----

    def test_details_old_snapshot_all_unchecked(self):
        """不带 --checksum 的旧快照：files/verified/unchecked=3/0/3，
        三个条目 status 均为 unchecked，仍按码点升序。"""
        snapshot = self.make_snapshot("snap-old", checksum=False)
        manifest = self.read_manifest(snapshot)
        self.assertTrue(
            all("sha256" not in item for item in manifest["files"]),
            "测试前提：旧快照任何条目都不应带 sha256 字段",
        )
        expected_entries = [
            self.expected_entry(NOTE_REL, "unchecked"),
            self.expected_entry(BIN_REL, "unchecked"),
            self.expected_entry(EMPTY_REL, "unchecked"),
        ]
        self.assert_details_success(
            snapshot, (3, 0, 3), expected_entries,
            "无摘要旧快照明细",
        )

    # ---- 成功：空快照明细为空数组、计数均为 0 ----

    def test_details_empty_snapshot(self):
        """空源目录以 --checksum 备份：entries 为空数组，三个计数均为 0。"""
        empty_source = self.work / "empty-source"
        empty_source.mkdir()
        snapshot = self.make_snapshot(
            "snap-empty", source=empty_source, checksum=True,
        )
        manifest = self.read_manifest(snapshot)
        self.assertEqual(
            manifest["files"], [], "测试前提：空快照清单应为空数组",
        )
        self.assert_details_success(
            snapshot, (0, 0, 0), [], "空快照明细",
        )

    # ---- 失败：错误条目排在有效条目之后，明细不得掩盖失败 ----

    def test_details_failure_missing_data_outputs_nothing(self):
        """删除一个有效条目之后的错误条目的数据文件：退出码 2、标准输出
        完全为空（无局部明细），stderr 含“数据缺失”与该相对路径。"""
        snapshot = self.make_snapshot("snap-missing", checksum=True)
        # 显式让 note.txt（有效）排在错误条目空文件之前。
        self.order_entries(snapshot, NOTE_REL, EMPTY_REL)

        missing_file = snapshot / "data" / EMPTY_REL
        self.assertTrue(
            missing_file.is_file(), "测试前提：被删数据在破坏前应存在",
        )
        missing_file.unlink()
        self.assertFalse(
            missing_file.exists(), "测试前提：破坏后数据文件应确实缺失",
        )

        self.assert_details_failure(
            snapshot, [REASON_MISSING, EMPTY_REL], "引用数据缺失",
        )

    def test_details_failure_checksum_mismatch_outputs_nothing(self):
        """只修改错误条目的字节、清单保留改动前的合法摘要，且该条目排在
        一个有效条目之后：退出码 2、标准输出完全为空，stderr 含
        “摘要校验不一致”与该相对路径。"""
        snapshot = self.make_snapshot("snap-mismatch", checksum=True)
        # 显式让 note.txt（有效）排在被篡改的二进制条目之前。
        self.order_entries(snapshot, NOTE_REL, BIN_REL)

        data_file = snapshot / "data" / BIN_REL
        original = data_file.read_bytes()
        self.assertEqual(
            original, bytes([0x00, 0xFF, 0x10, 0x00]),
            "测试前提：被改文件应为固定夹具字节",
        )
        # 只翻转首字节（00 -> 01），其余字节与长度不变。
        corrupted = bytes([0x01, 0xFF, 0x10, 0x00])
        self.assertNotEqual(corrupted, original)
        data_file.write_bytes(corrupted)

        # 清单摘要仍是原始字节的合法 64 位小写十六进制摘要：本样例唯一
        # 错误是数据字节与摘要不一致。
        manifest = self.read_manifest(snapshot)
        for item in manifest["files"]:
            if item["path"] == BIN_REL:
                self.assertEqual(
                    item["sha256"], sha256_hex(original),
                    "测试前提：清单应保留改动前的合法摘要",
                )
                self.assertNotEqual(
                    item["sha256"], sha256_hex(corrupted),
                    "测试前提：合法摘要不应恰好匹配被篡改的字节",
                )

        self.assert_details_failure(
            snapshot, [REASON_MISMATCH, BIN_REL], "摘要校验不一致",
        )


if __name__ == "__main__":
    unittest.main()
