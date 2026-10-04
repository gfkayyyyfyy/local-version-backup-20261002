#!/usr/bin/env python3
"""verify 对清单条目 sha256 摘要字段格式的回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT --checksum
    python backup.py verify SNAPSHOT

只观察退出码、标准输出、标准错误与文件原始字节，不调用 backup.py 的
任何内部校验函数。仅依赖 Python 3 标准库；全部源目录与快照在独立临时
目录中运行时准备，用例结束自动清理，不读写用户现有目录，不依赖网络、
第三方库或管理员权限。

夹具源数据由公开 backup --checksum 命令生成，含三个普通文件：

- ``note.txt``：UTF-8 文本 ``old`` 后接换行（合法条目，排在错误条目前）；
- ``中文 目录/内容.bin``：嵌套目录下依次为 0x00、0xFF、0x10 的三个字节，
  各失败样例只篡改该条目在清单中的 sha256 字段；
- ``空文件.dat``：零字节文件，摘要为空字节串的 SHA-256。

覆盖约定：

1. 成功用例：带合法且匹配摘要的有效快照，退出码 0、标准错误为空、标准
   输出只有一个可解析 JSON 对象，snapshot 为快照解析后的绝对路径，
   files/verified/unchecked=3/3/0。
2. 失败用例：每例从公开 backup 命令生成的独立有效快照出发，仅把嵌套
   文件条目的 sha256 改成一个形态错误的值，清单仍为合法 JSON、版本仍为
   整数 1，数据字节与另外两个条目保持有效，并让合法条目 note.txt 排在
   错误条目前面。待检查值：
   - 整数 0、布尔值 false、空数组、空对象（类型不是字符串）；
   - 空字符串、63 个零（长度不足 64）、65 个零（长度超过 64）；
   - ``A`` 后接 63 个零、``g`` 后接 63 个零（长度为 64 但含小写十六
     进制以外字符）。
   所有样例退出码为 2、标准输出完全为空（不得输出已检查条目的局部成功
   报告），标准错误同时包含“摘要格式错误”与原始相对路径
   ``中文 目录/内容.bin``，且不得被归为“摘要校验不一致”。
3. 各样例独立准备，不沿用上一次破坏后的快照；比较基准取自本次清单修改
   完成之后、调用 verify 之前。verify 前后比较整个临时工作区：相对路径
   集合、文件与目录类型及普通文件原始字节完全一致，没有新增报告或恢复
   目录（读取可能引起的访问时间变化不参与比较）。
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

# 夹具中的三个相对路径（斜杠分隔，目录名含中文与空格）。
NOTE_REL = "note.txt"
BIN_REL = "中文 目录/内容.bin"
EMPTY_REL = "空文件.dat"

# 备份时的固定内容：UTF-8 文本 old+换行、三个字节的二进制、零字节文件。
NOTE_BYTES = "old\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x10])
EMPTY_BYTES = b""

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

# 标准错误中应出现（或不得出现）的原因片段（与公开报错文案对应，
# 不调用内部函数）。
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


class VerifyChecksumFormatTests(unittest.TestCase):
    """verify 对 sha256 摘要字段形态错误的确定性公开结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照及任何意外产物都受限于此，
        # 用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-sha-format-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"

        write_files(self.source, BACKUP_FILES)

    # ---- 通用工具与断言 ----

    def make_checksum_snapshot(self, name):
        """用公开 backup --checksum 命令在工作区内新建快照，失败则中止。"""
        snapshot = self.work / name
        proc = run_cmd(["backup", str(self.source), str(snapshot),
                        "--checksum"])
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
        """整体重写快照清单，用于在样例中引入唯一一种错误。"""
        (snapshot / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_verify(self, snapshot):
        """以公开入口执行 verify。"""
        return run_cmd(["verify", str(snapshot)])

    def assert_work_intact(self, source_before, snapshot_before, work_before,
                           snapshot, context):
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
            f"verify 后工作区出现新增/改动文件（报告、恢复目录等）\n{context}",
        )

    # ---- 成功：全部带合法且匹配的摘要 ----

    def test_verify_valid_checksum_snapshot(self):
        """--checksum 快照且数据未改动：退出码 0、stderr 空、stdout 只有
        一个 JSON 对象，snapshot 为解析后的绝对路径，3/3/0。"""
        snapshot = self.make_checksum_snapshot("snap-valid")
        manifest = self.read_manifest(snapshot)
        # 测试前提：版本 1，三个条目均带与字节匹配的合法摘要。
        self.assertEqual(manifest.get("version"), 1)
        self.assertIsInstance(manifest.get("version"), int)
        self.assertNotIsInstance(manifest.get("version"), bool)
        for item in manifest["files"]:
            rel = item["path"]
            self.assertIn("sha256", item, f"测试前提：{rel} 应带 sha256 字段")
            self.assertEqual(
                item["sha256"], sha256_hex(BACKUP_FILES[rel]),
                f"测试前提：{rel} 的摘要应与字节匹配",
            )

        # 基准在调用前采集：成功 verify 同样必须只读。
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(snapshot)
        work_before = capture_tree(self.work)

        proc = self.run_verify(snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 合法摘要快照\n快照 SNAPSHOT: {snapshot}\n"
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
        self.assertEqual(
            set(result),
            {"snapshot", "files", "verified", "unchecked"},
            f"输出 JSON 字段集合不符\n{context}",
        )
        self.assertEqual(
            result["snapshot"], str(snapshot.resolve()),
            f"snapshot 应为快照解析后的绝对路径\n{context}",
        )
        self.assertEqual(result["files"], 3, f"files 计数不符\n{context}")
        self.assertEqual(result["verified"], 3, f"verified 计数不符\n{context}")
        self.assertEqual(
            result["unchecked"], 0, f"unchecked 计数不符\n{context}",
        )

        self.assert_work_intact(
            source_before, snapshot_before, work_before, snapshot, context,
        )

    # ---- 失败：sha256 字段格式错误 ----

    def prepare_bad_format_snapshot(self, name, bad_value, label, value_desc):
        """从独立的公开 backup --checksum 快照出发，只把嵌套文件条目的
        sha256 替换为 bad_value，其余清单字段与全部数据字节保持有效。

        显式重排为 [note.txt, 中文 目录/内容.bin, 空文件.dat]，保证一个
        合法条目排在错误条目前面。返回篡改完成后的快照。
        """
        snapshot = self.make_checksum_snapshot(name)
        manifest = self.read_manifest(snapshot)

        # 测试前提：起点是版本 1、三个带匹配摘要条目的有效快照。
        self.assertEqual(
            manifest.get("version"), 1,
            f"测试前提：{label} 起点清单版本应为整数 1\n输入: {value_desc}",
        )
        self.assertIsInstance(manifest.get("version"), int)
        self.assertNotIsInstance(manifest.get("version"), bool)
        by_path = {item["path"]: item for item in manifest["files"]}
        self.assertEqual(
            set(by_path), set(BACKUP_FILES),
            f"测试前提：{label} 起点清单应恰好包含三个相对路径"
            f"\n输入: {value_desc}",
        )
        for rel, item in by_path.items():
            self.assertEqual(
                item.get("sha256"), sha256_hex(BACKUP_FILES[rel]),
                f"测试前提：{label} 起点 {rel} 摘要应与字节匹配"
                f"\n输入: {value_desc}",
            )

        # 合法条目 note.txt 排第一，错误条目紧随其后，空文件条目收尾。
        manifest["files"] = [
            by_path[NOTE_REL], by_path[BIN_REL], by_path[EMPTY_REL],
        ]
        manifest["files"][1]["sha256"] = bad_value
        self.write_manifest(snapshot, manifest)

        # 篡改后重新读取确认：仍是合法 JSON、版本 1、三个条目，只有嵌套
        # 条目的 sha256 发生变化；数据字节一概未动。
        after = self.read_manifest(snapshot)
        self.assertEqual(
            after.get("version"), 1,
            f"测试前提：{label} 篡改后版本应保持整数 1\n输入: {value_desc}",
        )
        entries = after["files"]
        self.assertEqual(
            [item["path"] for item in entries],
            [NOTE_REL, BIN_REL, EMPTY_REL],
            f"测试前提：{label} 篡改后条目顺序与路径应符合预期"
            f"\n输入: {value_desc}",
        )
        self.assertEqual(
            entries[0]["sha256"], sha256_hex(NOTE_BYTES),
            f"测试前提：{label} 排在前面的合法条目摘要应仍有效"
            f"\n输入: {value_desc}",
        )
        self.assertEqual(
            entries[2]["sha256"], sha256_hex(EMPTY_BYTES),
            f"测试前提：{label} 空文件条目摘要应仍有效"
            f"\n输入: {value_desc}",
        )
        self.assertEqual(
            entries[1]["sha256"], bad_value,
            f"测试前提：{label} 篡改后回读到的 sha256 应为输入值本身"
            f"\n输入: {value_desc}",
        )
        # 嵌套条目的数据字节与夹具一致：本样例唯一错误是摘要字段形态。
        self.assertEqual(
            (snapshot / "data" / BIN_REL).read_bytes(), BINARY_BYTES,
            f"测试前提：{label} 嵌套文件数据字节应保持原样"
            f"\n输入: {value_desc}",
        )
        return snapshot

    def assert_bad_format_rejected(self, snapshot, label, value_desc):
        """格式错误用例公共流程。

        基准取自清单修改完成之后：退出码 2、stdout 完全为空、stderr 同时
        含“摘要格式错误”与嵌套文件原始相对路径，且不含“摘要校验不一致”；
        源目录、快照与整个工作区与基准完全一致。
        """
        # 基准取自篡改完成之后、verify 之前：准备动作不算产品写入。
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(snapshot)
        work_before = capture_tree(self.work)

        proc = self.run_verify(snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\nsha256 输入: {value_desc}\n"
            f"快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            stdout, "",
            f"拒绝后标准输出应完全为空，不得输出已检查条目的局部成功报告"
            f"\n{context}",
        )
        self.assertIn(
            REASON_BAD_FORMAT, stderr,
            f"标准错误缺少原因“{REASON_BAD_FORMAT}”\n{context}",
        )
        self.assertIn(
            BIN_REL, stderr,
            f"标准错误应指出原始相对路径 {BIN_REL}\n{context}",
        )
        self.assertNotIn(
            REASON_MISMATCH, stderr,
            f"格式错误不得被归为“{REASON_MISMATCH}”\n{context}",
        )

        self.assert_work_intact(
            source_before, snapshot_before, work_before, snapshot, context,
        )

    def run_bad_format_case(self, name, bad_value, label, value_desc):
        """单个格式错误值的完整独立流程：独立准备、执行、断言。"""
        snapshot = self.prepare_bad_format_snapshot(
            f"snap-{name}", bad_value, label, value_desc,
        )
        self.assert_bad_format_rejected(snapshot, label, value_desc)

    def test_verify_rejects_sha256_integer_zero(self):
        """sha256 为整数 0：类型不是字符串，按摘要格式错误拒绝。"""
        self.run_bad_format_case(
            "sha-int-zero", 0,
            "sha256 为整数 0", "0（整数）",
        )

    def test_verify_rejects_sha256_false_boolean(self):
        """sha256 为布尔值 false：不是字符串，按摘要格式错误拒绝。"""
        self.run_bad_format_case(
            "sha-false", False,
            "sha256 为 false", "false（布尔值）",
        )

    def test_verify_rejects_sha256_empty_array(self):
        """sha256 为空数组：类型不是字符串，按摘要格式错误拒绝。"""
        self.run_bad_format_case(
            "sha-empty-array", [],
            "sha256 为空数组", "[]（空数组）",
        )

    def test_verify_rejects_sha256_empty_object(self):
        """sha256 为空对象：类型不是字符串，按摘要格式错误拒绝。"""
        self.run_bad_format_case(
            "sha-empty-object", {},
            "sha256 为空对象", "{}（空对象）",
        )

    def test_verify_rejects_sha256_empty_string(self):
        """sha256 为空字符串：长度不是 64，按摘要格式错误拒绝。"""
        self.run_bad_format_case(
            "sha-empty-string", "",
            "sha256 为空字符串", '""（空字符串）',
        )

    def test_verify_rejects_sha256_63_zeros(self):
        """sha256 为 63 个零：长度不足 64，按摘要格式错误拒绝。"""
        value = "0" * 63
        self.run_bad_format_case(
            "sha-63-zeros", value,
            "sha256 为 63 个零", "63 个字符的 \"0\" 字符串",
        )

    def test_verify_rejects_sha256_65_zeros(self):
        """sha256 为 65 个零：长度超过 64，按摘要格式错误拒绝。"""
        value = "0" * 65
        self.run_bad_format_case(
            "sha-65-zeros", value,
            "sha256 为 65 个零", "65 个字符的 \"0\" 字符串",
        )

    def test_verify_rejects_sha256_uppercase_a_prefix(self):
        """sha256 为 A 后接 63 个零：长度为 64 但含大写字母，按摘要格式
        错误拒绝（摘要只接受小写十六进制字符）。"""
        value = "A" + "0" * 63
        self.run_bad_format_case(
            "sha-uppercase-a", value,
            "sha256 为 A 加 63 个零", "64 个字符，首字符为大写 A",
        )

    def test_verify_rejects_sha256_lowercase_g_prefix(self):
        """sha256 为 g 后接 63 个零：长度为 64 但 g 超出十六进制范围，
        按摘要格式错误拒绝。"""
        value = "g" + "0" * 63
        self.run_bad_format_case(
            "sha-out-of-range-g", value,
            "sha256 为 g 加 63 个零", "64 个字符，首字符为 g",
        )


if __name__ == "__main__":
    unittest.main()
