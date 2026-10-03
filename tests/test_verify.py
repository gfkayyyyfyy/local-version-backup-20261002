#!/usr/bin/env python3
"""verify 只读校验的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum]
    python backup.py verify SNAPSHOT

只观察退出码、标准输出、标准错误与文件原始字节，不调用 backup.py 的
任何内部校验函数。仅依赖 Python 3 标准库；全部源目录与快照在独立临时
目录中运行时准备，用例结束自动清理，不读写用户现有目录，不依赖网络、
第三方库或管理员权限。

夹具源数据由公开 backup 命令生成，含三个普通文件：

- ``note.txt``：明确的 UTF-8 文本；
- ``嵌套 目录/二进制 文件.bin``：嵌套目录下含零字节（0x00）的二进制；
- ``空文件.dat``：零字节文件（空字节串）。

覆盖约定：

1. 成功用例（退出码 0、标准错误为空、标准输出只有一个可解析 JSON 对象，
   其中 snapshot 为快照解析后的绝对路径）：
   - 无摘要旧快照（不带 --checksum 备份）：files/verified/unchecked=3/0/3；
   - 全部带合法且匹配摘要的快照：3/3/0；
   - 混合摘要清单，仅空文件缺省 sha256 字段：3/2/1；
   - 空源目录生成的空快照：0/0/0；
   - 额外放入 data 的未引用普通文件不影响计数。
2. 失败用例（每例只引入一种错误，退出码 2、标准输出为空）：
   - 清单 JSON 损坏：标准错误包含“JSON”；
   - 清单引用的数据缺失：包含“数据缺失”及对应相对路径；
   - sha256 显式为 null：包含“摘要格式错误”及对应相对路径；
   - 数据字节与合法摘要不一致：包含“摘要校验不一致”及对应相对路径。
   后三种样例中错误条目都显式排在一个有效条目之后，验证失败时不输出
   任何局部成功结果。
3. 所有用例都比较 verify 前后的源目录与快照：相对路径集合、条目类型与
   普通文件字节一致，且整个临时工作区没有新增报告、恢复目录或其他文件
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

# 夹具中的三个相对路径（斜杠分隔，目录名含空格，与 README 示例一致）。
NOTE_REL = "note.txt"
BIN_DIR_REL = "嵌套 目录"
BIN_REL = "嵌套 目录/二进制 文件.bin"
EMPTY_REL = "空文件.dat"

# 备份时的固定内容：明确文本、含零字节的二进制、零字节文件。
NOTE_BYTES = "note.txt 的校验内容\nUTF-8 第二行\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x10, 0x7F, 0x00, 0x80, 0x00])
EMPTY_BYTES = b""

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

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


class VerifyTests(unittest.TestCase):
    """verify 只读校验在成功与各类失败下的确定性公开结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照及任何意外产物都受限于此，
        # 用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-test-")
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
        """整体重写快照清单，用于在样例中引入唯一一种错误。"""
        (snapshot / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

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

    def assert_verify_success(self, snapshot, expected_files,
                              expected_verified, expected_unchecked, label):
        """成功用例公共流程：退出码 0、stderr 空、stdout 仅一个结果 JSON，
        且源目录、快照与整个工作区相对调用前保持不变。"""
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(snapshot)
        work_before = capture_tree(self.work)

        proc = self.run_verify(snapshot)
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
        self.assertEqual(
            set(result),
            {"snapshot", "files", "verified", "unchecked"},
            f"输出 JSON 字段集合不符\n{context}",
        )
        self.assertEqual(
            result["snapshot"], str(snapshot.resolve()),
            f"snapshot 应为快照解析后的绝对路径\n{context}",
        )
        self.assertEqual(result["files"], expected_files, f"files 计数不符\n{context}")
        self.assertEqual(
            result["verified"], expected_verified,
            f"verified 计数不符\n{context}",
        )
        self.assertEqual(
            result["unchecked"], expected_unchecked,
            f"unchecked 计数不符\n{context}",
        )

        self.assert_work_intact(
            source_before, snapshot_before, work_before, snapshot, context,
        )
        return result, context

    def assert_verify_failure(self, snapshot, reasons, label):
        """失败用例公共流程：退出码 2、stdout 为空、stderr 含全部原因片段，
        且源目录、快照与整个工作区相对调用前保持不变。"""
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(snapshot)
        work_before = capture_tree(self.work)

        proc = self.run_verify(snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(stdout, "", f"失败后标准输出应为空\n{context}")
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少原因“{reason}”\n{context}",
            )

        self.assert_work_intact(
            source_before, snapshot_before, work_before, snapshot, context,
        )
        return stdout, stderr, context

    # ---- 成功：无摘要旧快照 ----

    def test_verify_old_snapshot_without_checksums(self):
        """不带 --checksum 的旧快照：三个文件均无摘要，3/0/3。"""
        snapshot = self.make_snapshot("snap-old", checksum=False)
        manifest = self.read_manifest(snapshot)
        # 测试前提：旧清单中任何条目都不带 sha256 字段。
        self.assertTrue(
            all("sha256" not in item for item in manifest["files"]),
            "测试前提：旧快照不应有任何条目带 sha256 字段",
        )
        self.assert_verify_success(snapshot, 3, 0, 3, "无摘要旧快照")

    # ---- 成功：全部带摘要 ----

    def test_verify_snapshot_with_all_checksums(self):
        """--checksum 快照且数据未改动：3/3/0；data 中额外的未引用普通
        文件不增加任何计数。"""
        snapshot = self.make_snapshot("snap-all", checksum=True)
        manifest = self.read_manifest(snapshot)
        # 测试前提：三个条目均带与字节匹配的合法摘要。
        for item in manifest["files"]:
            self.assertIn("sha256", item, "测试前提：条目应带 sha256 字段")
            rel = item["path"]
            self.assertEqual(
                item["sha256"],
                sha256_hex(BACKUP_FILES[rel]),
                f"测试前提：{rel} 的摘要应与字节匹配",
            )

        # 额外放入 data 的未引用普通文件：计数只以清单为准。
        extra = snapshot / "data" / "未引用 的报告.txt"
        extra.parent.mkdir(parents=True, exist_ok=True)
        extra.write_bytes("不在清单里，不应被计数\n".encode("utf-8"))

        self.assert_verify_success(snapshot, 3, 3, 0, "全部带摘要快照")

    # ---- 成功：混合摘要，仅空文件缺省 sha256 ----

    def test_verify_mixed_only_empty_file_unchecked(self):
        """清单中仅空文件条目完全省略 sha256：3/2/1，无摘要不被误判。"""
        snapshot = self.make_snapshot("snap-mixed", checksum=True)
        manifest = self.read_manifest(snapshot)
        removed = False
        for item in manifest["files"]:
            if item["path"] == EMPTY_REL:
                del item["sha256"]
                removed = True
            else:
                self.assertIn(
                    "sha256", item,
                    "测试前提：其余两个条目应保留合法摘要",
                )
        self.assertTrue(removed, "测试前提：清单应包含空文件条目")
        self.write_manifest(snapshot, manifest)

        self.assert_verify_success(snapshot, 3, 2, 1, "混合摘要快照")

    # ---- 成功：空快照 ----

    def test_verify_empty_snapshot(self):
        """空源目录备份出的空清单：三个计数均为 0。"""
        empty_source = self.work / "empty-source"
        empty_source.mkdir()
        snapshot = self.make_snapshot(
            "snap-empty", source=empty_source, checksum=True,
        )
        manifest = self.read_manifest(snapshot)
        self.assertEqual(
            manifest["files"], [], "测试前提：空快照清单应为空数组",
        )
        self.assert_verify_success(snapshot, 0, 0, 0, "空快照")

    # ---- 失败：清单 JSON 损坏 ----

    def test_verify_rejects_corrupt_manifest_json(self):
        """清单文件内容不是合法 JSON：退出码 2，stderr 含 JSON，无输出。"""
        snapshot = self.make_snapshot("snap-bad-json", checksum=True)
        # 在破坏前确认清单原本可读，确保本样例只引入 JSON 损坏这一种错误。
        self.read_manifest(snapshot)
        (snapshot / "manifest.json").write_bytes(
            b"{ this is not valid json\n",
        )
        self.assert_verify_failure(
            snapshot, [REASON_JSON], "清单 JSON 损坏",
        )

    # ---- 失败：清单引用的数据缺失 ----

    def test_verify_rejects_missing_referenced_data(self):
        """删除清单引用的空文件数据，且该错误条目排在有效条目之后：
        退出码 2，stderr 含“数据缺失”与该相对路径，无局部成功输出。"""
        snapshot = self.make_snapshot("snap-missing", checksum=True)
        missing_file = snapshot / "data" / EMPTY_REL
        self.assertTrue(
            missing_file.is_file(), "测试前提：被删数据在破坏前应存在",
        )
        # 显式让一个有效条目排在错误条目之前。
        self.order_entries(snapshot, NOTE_REL, EMPTY_REL)
        missing_file.unlink()

        self.assert_verify_failure(
            snapshot, [REASON_MISSING, EMPTY_REL], "引用数据缺失",
        )

    # ---- 失败：sha256 为 null ----

    def test_verify_rejects_null_sha256(self):
        """错误条目 sha256 显式为 null 且排在有效条目之后：格式错误，
        不等同于字段缺省；退出码 2，stderr 含原因与该相对路径。"""
        snapshot = self.make_snapshot("snap-null", checksum=True)
        self.order_entries(snapshot, NOTE_REL, BIN_REL)
        manifest = self.read_manifest(snapshot)
        replaced = False
        for item in manifest["files"]:
            if item["path"] == BIN_REL:
                item["sha256"] = None
                replaced = True
        self.assertTrue(replaced, "测试前提：清单应包含嵌套二进制条目")
        self.write_manifest(snapshot, manifest)

        self.assert_verify_failure(
            snapshot, [REASON_BAD_FORMAT, BIN_REL], "sha256 为 null",
        )

    # ---- 失败：内容与合法摘要不一致 ----

    def test_verify_rejects_checksum_mismatch(self):
        """只改动错误条目的数据字节、清单保留合法摘要，且该条目排在有效
        条目之后：退出码 2，stderr 含“摘要校验不一致”与该相对路径。"""
        snapshot = self.make_snapshot("snap-mismatch", checksum=True)
        self.order_entries(snapshot, NOTE_REL, BIN_REL)

        data_file = snapshot / "data" / BIN_REL
        original = data_file.read_bytes()
        self.assertTrue(original, "测试前提：被改动文件应非空")
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

        self.assert_verify_failure(
            snapshot, [REASON_MISMATCH, BIN_REL], "摘要校验不一致",
        )


if __name__ == "__main__":
    unittest.main()
