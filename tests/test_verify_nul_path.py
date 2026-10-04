#!/usr/bin/env python3
"""verify 对清单 path 含 U+0000 空字符的回归测试。

验收方式严格走 README 命令：

    python backup.py verify SNAPSHOT

只观察退出码、标准输出、标准错误与目录状态，不调用 backup.py 的任何
内部校验函数。仅依赖 Python 3 标准库；全部源目录与快照在独立临时
目录中运行时准备，用例结束自动清理，不读写用户现有目录，不依赖
网络、第三方库、符号链接权限或固定机器路径。

夹具源数据由公开 backup 命令生成，含两个普通文件：

- ``note.txt``：明确的 UTF-8 文本，作为始终有效的条目；
- ``中文 目录/二进制 文件.bin``：含零字节（0x00）的二进制，用于证明
  文件内容中的零字节不属于损坏路径，作为正常对照逐字节存在于快照中。

覆盖约定（只补齐“清单路径含空字符”这一种损坏，不扩展其他非法字符
或平台文件名规则）：

1. 版本 1 快照演示：保留有效 ``note.txt`` 条目并令其排在前面，把另一
   条 path 改成 ``中文 目录/坏\\u0000文件.bin``（JSON 解码后含实际
   U+0000）。verify 必须退出码 2、标准输出为空（不输出部分统计）、
   标准错误包含“清单路径包含空字符”，并以 JSON 字符串形式展示原始
   路径，空字符显示为字面转义序列 ``\\u0000``；不输出实际空字符或
   异常堆栈。
2. 空字符位于开头、结尾以及一条路径中多次出现时结果相同。
3. 正常对照：带中文、空格路径与零字节二进制的有效快照校验通过，
   原有 JSON 统计为 2 个文件、0 个已校验摘要、2 个未校验（2/0/2）。
4. 拒绝时快照清单、数据与快照/目录外已有文件保持原样，不产生临时
   文件（比较调用前后整个工作区的目录树）。

path 为 null、空字符串或其他类型沿用既有诊断，由
test_verify_manifest_paths.py 覆盖，本文件不重复。
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

# 夹具中的两个相对路径（斜杠分隔，目录与文件名含中文和空格）。
NOTE_REL = "note.txt"
BIN_REL = "中文 目录/二进制 文件.bin"

# 备份时的固定内容：明确文本，以及含多个零字节（0x00）的二进制。
NOTE_BYTES = "old\n".encode("utf-8")
BINARY_BYTES = bytes([0x00, 0xFF, 0x0D, 0x0A, 0x00, 0x80, 0x00])

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
}

# 损坏样例：JSON 解码后的 path 实际含 U+0000（四种位置）。
BAD_PATH_MIDDLE = "中文 目录/坏\u0000文件.bin"
BAD_PATH_START = "\u0000坏文件.bin"
BAD_PATH_END = "坏文件.bin\u0000"
BAD_PATH_MULTIPLE = "坏\u0000文\u0000件.bin"

# 标准错误中应出现的原因片段（与公开报错文案对应，断言基于子进程
# 实际输出的 UTF-8 字节，不调用内部函数）。
REASON_NUL = "清单路径包含空字符"
# 空字符必须以字面转义序列出现（反斜杠 + u0000 六个字符）。
NUL_ESCAPE_LITERAL = "\\u0000"
# 实际空字符不得出现在标准错误的原始字节中。
RAW_NUL = b"\x00"
TRACEBACK_MARKER = "Traceback"


def run_cmd(argv):
    """通过公开命令行执行 backup.py，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), *argv],
        cwd=str(ROOT),
        env=CHILD_ENV,
        timeout=60,
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


def dump_manifest_with_escaped_nul(doc):
    """序列化清单为合法 JSON 文本。

    json.dumps 按 JSON 规范把空字符等控制字符转义为 ``\\u0000``，中文
    （ensure_ascii=False）按字面写出，因此清单文本中不存在未转义空字符。
    """
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


class VerifyNulPathTests(unittest.TestCase):
    """非空字符串 path 中任意位置的空字符都必须被整体拒绝。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照及任何意外产物都受限
        # 于此，用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-nul-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

        write_files(self.source, BACKUP_FILES)
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 快照之外的已有文件，只读 verify 前后必须字节不变。
        self.outside_marker = self.work / "outside.txt"
        self.outside_marker.write_bytes(b"outside snapshot, do not touch\n")

    # ---- 工具 ----

    def read_manifest(self):
        return json.loads(
            (self.snapshot / "manifest.json").read_text(encoding="utf-8")
        )

    def tamper_second_path(self, bad_path):
        """把第二条（排在有效 note.txt 之后）条目的 path 改成损坏路径。"""
        manifest = self.read_manifest()
        self.assertEqual(manifest.get("version"), 1)
        self.assertIsInstance(manifest.get("version"), int)
        self.assertNotIsInstance(manifest.get("version"), bool)
        self.assertEqual(manifest["files"][0]["path"], NOTE_REL)
        self.assertEqual(len(manifest["files"]), 2)

        manifest["files"][1]["path"] = bad_path
        (self.snapshot / "manifest.json").write_text(
            dump_manifest_with_escaped_nul(manifest), encoding="utf-8"
        )

        after = self.read_manifest()
        self.assertEqual(after.get("version"), 1)
        self.assertEqual(after["files"][1]["path"], bad_path)
        self.assertIn("\u0000", after["files"][1]["path"])

    def assert_failure(self, proc, bad_path, label):
        stdout_raw = proc.stdout
        stderr_raw = proc.stderr
        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")
        shown_path = json.dumps(bad_path, ensure_ascii=False)
        context = (
            f"用例: {label}\nexit={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            stdout_raw, b"",
            f"失败后标准输出应为空（不得输出部分统计）\n{context}",
        )
        self.assertIn("清单路径包含空字符", stderr, f"缺少拒绝原因\n{context}")
        self.assertIn(shown_path, stderr, f"缺少原始路径的 JSON 展示\n{context}")
        self.assertIn(NUL_ESCAPE_LITERAL, stderr, context)
        self.assertNotIn(RAW_NUL, stderr_raw, f"stderr 不得含实际空字符\n{context}")
        self.assertNotIn(TRACEBACK_MARKER, stderr, f"不得输出堆栈\n{context}")

    # ---- 正常对照 ----

    def test_verify_valid_snapshot_stats_and_zero_byte_content(self):
        """有效版本 1 快照（无摘要）：退出码 0、stderr 空，stdout 只有
        一个结果 JSON，files/verified/unchecked = 2/0/2；数据文件中的
        零字节是内容而非损坏路径，快照字节不受影响。"""
        # 数据中的零字节在夹具备份后仍然逐字存在（正常路径形态）。
        self.assertEqual(
            (self.snapshot / "data" / BIN_REL).read_bytes(), BINARY_BYTES
        )

        work_before = capture_tree(self.work)
        proc = run_cmd(["verify", str(self.snapshot)])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = f"stdout={stdout!r}\nstderr={stderr!r}"

        self.assertEqual(proc.returncode, 0, f"正常对照应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时 stderr 应为空\n{context}")

        nonempty = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(len(nonempty), 1, f"stdout 应只有一个 JSON\n{context}")
        result = json.loads(nonempty[0])
        self.assertEqual(set(result),
                         {"snapshot", "files", "verified", "unchecked"},
                         context)
        self.assertEqual(result["snapshot"], str(self.snapshot.resolve()), context)
        self.assertEqual(result["files"], 2, context)
        self.assertEqual(result["verified"], 0, context)
        self.assertEqual(result["unchecked"], 2, context)

        self.assertEqual(
            capture_tree(self.work), work_before,
            f"verify 后工作区发生变化\n{context}",
        )

    # ---- 失败用例（每种位置一个）----

    def _run_bad_case(self, label, bad_path):
        self.tamper_second_path(bad_path)
        work_before = capture_tree(self.work)
        snap_listing_before = sorted(os.listdir(self.snapshot))

        proc = run_cmd(["verify", str(self.snapshot)])
        self.assert_failure(proc, bad_path, label)

        self.assertEqual(
            capture_tree(self.work), work_before,
            f"拒绝后工作区出现新增/改动/删除（快照或目录外文件）\n用例: {label}",
        )
        self.assertEqual(
            sorted(os.listdir(self.snapshot)), snap_listing_before,
            f"拒绝后快照顶层条目变化（可能遗留临时文件）\n用例: {label}",
        )
        self.assertFalse(
            (self.snapshot / "manifest.json.tmp").exists(),
            f"拒绝后遗留 manifest.json.tmp\n用例: {label}",
        )

    def test_reject_nul_in_middle_demo(self):
        """版本 1 演示：note.txt 在前，另一条 path 含空字符（中间）。"""
        self._run_bad_case("空字符在中间（演示）", BAD_PATH_MIDDLE)

    def test_reject_nul_at_start(self):
        """空字符位于 path 开头时结果相同。"""
        self._run_bad_case("空字符在开头", BAD_PATH_START)

    def test_reject_nul_at_end(self):
        """空字符位于 path 结尾时结果相同。"""
        self._run_bad_case("空字符在结尾", BAD_PATH_END)

    def test_reject_multiple_nuls(self):
        """一条 path 含多个空字符时结果相同。"""
        self._run_bad_case("多个空字符", BAD_PATH_MULTIPLE)


if __name__ == "__main__":
    unittest.main()
