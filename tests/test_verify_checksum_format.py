#!/usr/bin/env python3
"""verify 入口对 sha256 摘要格式的回归测试（只补格式样例）。

严格通过 README 公开命令验收，不调用 backup.py 的任何内部校验函数：

    python backup.py backup SOURCE SNAPSHOT --checksum
    python backup.py verify SNAPSHOT

只观察子进程的退出码、标准输出、标准错误以及工作区文件状态；仅依赖
Python 3 标准库。每个用例的源目录与快照都在独立临时目录中运行时准备，
用例结束自动清理，不读写用户现有目录，不依赖网络、第三方库、管理员
权限或外部账号。

夹具源数据（由公开 backup --checksum 命令生成清单版本 1 的快照）：

- ``note.txt``：UTF-8 文本 ``old\\n``（4 个字节）；
- ``中文 目录/内容.bin``：嵌套目录下的二进制，字节为 00 ff 10；
- ``空文件.dat``：零字节文件。

覆盖内容：

1. 成功：verify 退出码 0、标准错误为空、标准输出只有一个 JSON 对象，
   其中 snapshot 是快照解析后的绝对路径，files/verified/unchecked=3/3/0。
2. 失败（每个样例只把嵌套条目 ``中文 目录/内容.bin`` 的 sha256 改成一个
   格式非法的值，数据字节与其他条目保持有效，并让合法的 note.txt 条目
   显式排在错误条目之前）：

   - 整数 0；
   - 布尔值 false；
   - 空数组 []；
   - 空对象 {}；
   - 空字符串 ""；
   - 63 个 "0"（长度不足 64）；
   - 65 个 "0"（长度超过 64）；
   - "A" 后接 63 个 "0"（长度为 64 但含非十六进制字符）；
   - "g" 后接 63 个 "0"（小写字母超出 a-f 范围）。

   各样例独立准备全新快照，不复用前一个被破坏的快照。每个样例都要求：
   退出码 2、标准输出完全为空、标准错误同时包含“摘要格式错误”与原始
   相对路径 ``中文 目录/内容.bin``；不得归类为“摘要校验不一致”，也不得
   输出已校验部分文件的成功报告。

3. 每次命令调用前后都核对整个临时工作区：相对路径集合、文件/目录类型
   与普通文件原始字节完全一致。比较基准取自“本次修改清单之后、调用
   verify 之前”的状态，因此测试自身的准备动作不被当作产品写入；读取
   可能改变的访问时间不参与比较。

本文件不新增命令、不修改产品行为；备份、排除文件、全部恢复与指定文件
恢复的既有公开行为以及清单版本 1 均保持不变。
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

# 备份时的固定内容：明确的 UTF-8 文本、嵌套二进制、零字节文件。
NOTE_BYTES = "old\n".encode("utf-8")
BIN_BYTES = bytes([0x00, 0xFF, 0x10])
EMPTY_BYTES = b""

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BIN_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

# 标准错误中应出现/不应出现的原因片段（与公开报错文案对应）。
REASON_BAD_FORMAT = "摘要格式错误"
REASON_MISMATCH = "摘要校验不一致"

# 摘要格式非法值样例：(用于报告的描述, 写入清单 sha256 字段的 Python 值)。
# 注意刻意不包含 64 个 "0"：它是形态合法的摘要，只会造成“校验不一致”，
# 不属于本文件覆盖的格式错误。
BAD_FORMAT_VALUES = [
    ("整数 0", 0),
    ("布尔值 false", False),
    ("空数组 []", []),
    ("空对象 {}", {}),
    ('空字符串 ""', ""),
    ("63 个零（长度不足）", "0" * 63),
    ("65 个零（长度超长）", "0" * 65),
    ("A 后接 63 个零（含非十六进制字符）", "A" + "0" * 63),
    ("g 后接 63 个零（小写字母超出 a-f）", "g" + "0" * 63),
]


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
    """递归记录目录树：相对路径 -> (类型, 字节或链接目标)。

    只记录相对路径集合、条目类型与普通文件字节，不记录访问时间等读取
    易变属性；键排序使比较与遍历顺序无关。
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
    """verify 对 sha256 摘要形态错误的公开回归结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照及任何意外产物都受限于此，
        # 用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-fmt-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"

        write_files(self.source, BACKUP_FILES)

    # ---- 通用工具 ----

    def make_fresh_snapshot(self, name):
        """用公开 backup --checksum 命令在工作区内新建全新快照。"""
        snapshot = self.work / name
        proc = run_cmd(
            ["backup", str(self.source), str(snapshot), "--checksum"]
        )
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
        """整体重写快照清单，用于只替换嵌套条目的 sha256 值。"""
        (snapshot / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def assert_tree_unchanged(self, before, after, label):
        """相对路径集合、条目类型、普通文件字节必须逐项一致。"""
        self.assertEqual(
            set(before), set(after),
            f"verify 前后相对路径集合发生变化\n用例: {label}",
        )
        for rel in sorted(set(before) & set(after)):
            before_type = before[rel][0]
            after_type = after[rel][0]
            self.assertEqual(
                before_type, after_type,
                f"{rel} 的条目类型发生变化\n用例: {label}",
            )
        self.assertEqual(
            before, after,
            f"verify 前后目录树（类型或普通文件字节）发生变化\n用例: {label}",
        )

    # ---- 成功：带合法摘要的快照 ----

    def test_verify_valid_checksum_snapshot(self):
        """--checksum 快照且摘要全部匹配：退出码 0、stderr 空，stdout 只有
        一个 JSON 对象，snapshot 为解析后的绝对路径，计数 3/3/0。"""
        snapshot = self.make_fresh_snapshot("snap-ok")
        manifest = self.read_manifest(snapshot)

        # 测试前提：清单仍是版本 1，且三个条目带与字节匹配的合法摘要。
        self.assertEqual(manifest["version"], 1, "测试前提：清单版本应为 1")
        self.assertEqual(
            sorted(item["path"] for item in manifest["files"]),
            sorted(BACKUP_FILES),
            "测试前提：清单应恰好包含三个相对路径",
        )
        for item in manifest["files"]:
            rel = item["path"]
            self.assertEqual(
                item["sha256"], sha256_hex(BACKUP_FILES[rel]),
                f"测试前提：{rel} 的摘要应与数据字节匹配",
            )

        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(snapshot)
        work_before = capture_tree(self.work)

        proc = run_cmd(["verify", str(snapshot)])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"快照 SNAPSHOT: {snapshot}\n"
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
            set(result), {"snapshot", "files", "verified", "unchecked"},
            f"输出 JSON 字段集合不符\n{context}",
        )
        self.assertEqual(
            result["snapshot"], str(snapshot.resolve()),
            f"snapshot 应为快照解析后的绝对路径\n{context}",
        )
        self.assertEqual(result["files"], 3, f"files 应为 3\n{context}")
        self.assertEqual(result["verified"], 3, f"verified 应为 3\n{context}")
        self.assertEqual(result["unchecked"], 0, f"unchecked 应为 0\n{context}")

        self.assert_tree_unchanged(
            source_before, capture_tree(self.source), "成功用例-源目录",
        )
        self.assert_tree_unchanged(
            snapshot_before, capture_tree(snapshot), "成功用例-快照",
        )
        self.assert_tree_unchanged(
            work_before, capture_tree(self.work), "成功用例-工作区",
        )

    # ---- 失败：嵌套条目的 sha256 形态非法 ----

    def test_verify_rejects_malformed_checksum_formats(self):
        """每种非法形态独立准备全新快照：只替换嵌套条目的 sha256，且让
        合法的 note.txt 排在错误条目之前；退出码 2、stdout 完全为空、
        stderr 含“摘要格式错误”与原始相对路径，且不得误报为校验不一致。"""
        for index, (desc, bad_value) in enumerate(BAD_FORMAT_VALUES):
            with self.subTest(desc=desc):
                # 每个样例独立备份全新快照，不复用上一个被破坏的快照。
                snapshot = self.make_fresh_snapshot(f"snap-bad-fmt-{index}")
                manifest = self.read_manifest(snapshot)

                # 只替换嵌套条目的 sha256；其他条目（含其合法摘要）原样
                # 保留，并显式让合法条目 note.txt 排在错误条目之前。
                entries = manifest["files"]
                by_path = {item["path"]: item for item in entries}
                self.assertEqual(
                    set(by_path), set(BACKUP_FILES),
                    f"测试前提：清单应恰好包含三个条目\n样例: {desc}",
                )
                by_path[BIN_REL]["sha256"] = bad_value
                manifest["files"] = [
                    by_path[NOTE_REL],
                    by_path[BIN_REL],
                    by_path[EMPTY_REL],
                ]
                self.write_manifest(snapshot, manifest)

                # 破坏后、verify 前的前提自检：
                # 1) 清单仍是合法 JSON，版本 1，错误条目确实排第二位，
                #    前面是一个合法且与字节匹配的条目；
                # 2) 写入值经 JSON 往返后类型与内容都保持预期；
                # 3) 三个数据文件字节均未改动（嵌套文件仍是 00 ff 10），
                #    本样例唯一错误是摘要字段形态。
                after = self.read_manifest(snapshot)
                self.assertEqual(
                    after["version"], 1,
                    f"测试前提：清单版本应保持 1\n样例: {desc}",
                )
                ordered = after["files"]
                self.assertEqual(
                    [item["path"] for item in ordered],
                    [NOTE_REL, BIN_REL, EMPTY_REL],
                    f"测试前提：合法条目应排在错误条目之前\n样例: {desc}",
                )
                self.assertEqual(
                    ordered[0]["sha256"], sha256_hex(NOTE_BYTES),
                    f"测试前提：首位条目的摘要应仍合法且匹配\n样例: {desc}",
                )
                self.assertEqual(
                    ordered[2]["sha256"], sha256_hex(EMPTY_BYTES),
                    f"测试前提：空文件条目的摘要应仍合法且匹配\n样例: {desc}",
                )
                stored = ordered[1]["sha256"]
                self.assertIs(
                    type(stored), type(bad_value),
                    f"测试前提：非法值经 JSON 往返后类型应保持"
                    f"（期望 {type(bad_value)!r}，实际 {type(stored)!r}）"
                    f"\n样例: {desc}",
                )
                self.assertEqual(
                    stored, bad_value,
                    f"测试前提：非法值经 JSON 往返后内容应保持\n样例: {desc}",
                )
                self.assertEqual(
                    (snapshot / "data" / BIN_REL).read_bytes(), BIN_BYTES,
                    f"测试前提：嵌套数据字节不应被改动\n样例: {desc}",
                )
                self.assertEqual(
                    (snapshot / "data" / NOTE_REL).read_bytes(), NOTE_BYTES,
                    f"测试前提：note.txt 字节不应被改动\n样例: {desc}",
                )
                self.assertEqual(
                    (snapshot / "data" / EMPTY_REL).read_bytes(), EMPTY_BYTES,
                    f"测试前提：空文件字节不应被改动\n样例: {desc}",
                )

                # 比较基准取自清单修改完成之后：此后的只读 verify 不得
                # 改动源目录、快照，也不得在工作区新增任何文件。
                source_before = capture_tree(self.source)
                snapshot_before = capture_tree(snapshot)
                work_before = capture_tree(self.work)

                proc = run_cmd(["verify", str(snapshot)])
                stdout = proc.stdout.decode("utf-8", errors="replace")
                stderr = proc.stderr.decode("utf-8", errors="replace")
                context = (
                    f"样例: {desc}\n非法值: {bad_value!r}\n"
                    f"快照 SNAPSHOT: {snapshot}\n"
                    f"exit={proc.returncode}\n"
                    f"stdout={stdout!r}\nstderr={stderr!r}"
                )

                self.assertEqual(
                    proc.returncode, 2, f"退出码应为 2\n{context}",
                )
                self.assertEqual(
                    stdout, "",
                    f"失败后标准输出必须完全为空"
                    f"（不得输出部分校验的成功报告）\n{context}",
                )
                self.assertIn(
                    REASON_BAD_FORMAT, stderr,
                    f"标准错误缺少“{REASON_BAD_FORMAT}”\n{context}",
                )
                self.assertIn(
                    BIN_REL, stderr,
                    f"标准错误应包含原始相对路径 {BIN_REL}\n{context}",
                )
                self.assertNotIn(
                    REASON_MISMATCH, stderr,
                    f"形态错误不得被归类为“{REASON_MISMATCH}”\n{context}",
                )

                self.assert_tree_unchanged(
                    source_before, capture_tree(self.source),
                    f"{desc}-源目录",
                )
                self.assert_tree_unchanged(
                    snapshot_before, capture_tree(snapshot),
                    f"{desc}-快照",
                )
                self.assert_tree_unchanged(
                    work_before, capture_tree(self.work),
                    f"{desc}-工作区",
                )


if __name__ == "__main__":
    unittest.main()
