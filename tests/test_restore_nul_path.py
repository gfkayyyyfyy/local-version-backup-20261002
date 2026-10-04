#!/usr/bin/env python3
"""restore 对清单 path 含 U+0000 空字符的整体拒绝回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH]...

只观察退出码、标准输出、标准错误与文件结果，不调用 backup.py 的任何
内部校验函数。仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在
独立临时目录中运行时准备，用例结束自动清理，不读写用户现有目录，
不依赖网络、第三方库、符号链接权限或固定机器路径。

夹具源数据由公开 backup 命令生成，含两个普通文件：

- ``note.txt``：明确的 UTF-8 文本，作为始终有效的条目；
- ``中文 目录/二进制 文件.bin``：含零字节（0x00）的二进制，用于证明
  文件内容中的零字节不属于损坏路径，正常对照下逐字节恢复。

覆盖约定（只补齐“清单路径含空字符”这一种损坏，不扩展其他非法字符）：

1. 版本 1 快照演示：保留有效 ``note.txt`` 条目并令其排在前面，把另一
   条 path 改成 ``中文 目录/坏\\u0000文件.bin``（JSON 解码后含实际
   U+0000）。restore 必须退出码 2、标准输出为空、标准错误包含
   “清单路径包含空字符”，并以 JSON 字符串形式展示原始路径，空字符
   显示为字面转义序列 ``\\u0000``；不输出实际空字符或异常堆栈，
   不创建 DEST。
2. 空字符位于开头、结尾以及一条路径中多次出现时结果相同。
3. 即使通过 --file 只选择有效的 note.txt，未选中的损坏条目仍使整体
   恢复被拒绝，且不创建 DEST。
4. 正常对照：含中文、空格路径与零字节二进制文件的有效快照恢复成功，
   恢复后字节不变。
5. 拒绝时快照清单、数据与快照/目录外已有文件保持原样，不产生临时
   文件（比较调用前后整个工作区的目录树）。

path 为 null 或空字符串等既有诊断不在本文件扩展，仍由
test_restore_validation.py 覆盖。
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
NOTE_BYTES = "笔记内容\n第二行\n".encode("utf-8")
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

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_NUL = "清单路径包含空字符"
# 标准错误中空字符必须以字面转义序列出现（反斜杠 + u0000 六个字符）。
NUL_ESCAPE_LITERAL = "\\u0000"
# 实际空字符不得出现在标准错误的原始字节中。
RAW_NUL = b"\x00"
TRACEBACK_MARKER = "Traceback"

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"


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


def dump_manifest_with_escaped_nul(doc):
    """序列化清单为合法 JSON 文本。

    json.dumps 按 JSON 规范把空字符等控制字符转义为 ``\\u0000``，中文
    （ensure_ascii=False）按字面写出，因此清单文本中不存在未转义空字符。
    """
    return json.dumps(doc, ensure_ascii=False, indent=2) + "\n"


class RestoreNulPathTests(unittest.TestCase):
    """非空字符串 path 中任意位置的空字符都必须在写入前整体拒绝。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照、恢复目标及任何意外产物
        # 都受限于此，用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-nul-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"
        self.dest = self.work / "restored"

        write_files(self.source, BACKUP_FILES)
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 快照/恢复目录之外的已有文件，拒绝前后必须字节不变。
        self.outside_marker = self.work / "outside.txt"
        self.outside_marker.write_bytes(b"outside snapshot, do not touch\n")

    # ---- 通用工具与断言 ----

    def read_manifest(self):
        """读取快照清单（JSON 是公开快照格式的一部分）。"""
        return json.loads(
            (self.snapshot / "manifest.json").read_text(encoding="utf-8")
        )

    def tamper_second_path(self, bad_path):
        """把第二条（排在有效 note.txt 之后）条目的 path 改成损坏路径。

        保持合法 JSON、整数版本 1、条目顺序与数据文件原样；仅 path
        的取值这一种损坏被引入。
        """
        manifest = self.read_manifest()
        # 测试前提：版本 1，note.txt 在前，被修改条目在其后。
        self.assertEqual(manifest.get("version"), 1)
        self.assertIsInstance(manifest.get("version"), int)
        self.assertNotIsInstance(manifest.get("version"), bool)
        self.assertEqual(manifest["files"][0]["path"], NOTE_REL)
        self.assertEqual(len(manifest["files"]), 2)

        manifest["files"][1]["path"] = bad_path
        (self.snapshot / "manifest.json").write_text(
            dump_manifest_with_escaped_nul(manifest), encoding="utf-8"
        )

        # 篡改后重新解码确认：仍是合法 JSON、版本 1，且 path 解码后
        # 确实含实际空字符（损坏来自解码后的字符而非 JSON 语法）。
        after = self.read_manifest()
        self.assertEqual(after.get("version"), 1)
        self.assertEqual(after["files"][1]["path"], bad_path)
        self.assertIn("\u0000", after["files"][1]["path"])

    def assert_rejected(self, proc, bad_path, label, dest, selections=None):
        """失败用例公共断言：退出码 2、stdout 空、stderr 形态确定，
        不创建恢复目标。dest 为本次用例的恢复目标路径。"""
        stdout_raw = proc.stdout
        stderr_raw = proc.stderr
        stdout = stdout_raw.decode("utf-8", errors="replace")
        stderr = stderr_raw.decode("utf-8", errors="replace")
        # 以 JSON 字符串形式展示的原始路径：json.dumps 已把空字符
        # 转义为字面 \u0000，中文保持可读。
        shown_path = json.dumps(bad_path, ensure_ascii=False)
        context = (
            f"用例: {label}\n选择: {selections}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            stdout_raw, b"",
            f"失败后标准输出应为空，不得出现成功摘要或部分结果\n{context}",
        )
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现成功摘要\n{context}")

        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误前缀\n{context}")
        self.assertIn(REASON_NUL, stderr, f"标准错误缺少拒绝原因\n{context}")
        self.assertIn(
            shown_path, stderr,
            f"标准错误应以 JSON 字符串形式展示原始路径（{shown_path}）"
            f"\n{context}",
        )
        self.assertIn(
            NUL_ESCAPE_LITERAL, stderr,
            f"空字符应显示为字面转义序列 \\u0000\n{context}",
        )
        # 不得输出实际空字符或异常堆栈。
        self.assertNotIn(
            RAW_NUL, stderr_raw,
            f"标准错误的原始字节中不得含实际空字符\n{context}",
        )
        self.assertNotIn(
            TRACEBACK_MARKER, stderr,
            f"不得输出异常堆栈\n{context}",
        )

        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )

    def assert_work_intact(self, work, snapshot, work_before, listing_before,
                            label):
        """拒绝后整个工作区（快照清单/数据、目录外文件）保持原样，
        不产生临时文件。"""
        self.assertEqual(
            capture_tree(work), work_before,
            f"拒绝后工作区出现新增/改动/删除（快照、目录外文件或临时文件）"
            f"\n用例: {label}",
        )
        self.assertEqual(
            sorted(os.listdir(snapshot)), listing_before,
            f"拒绝后快照目录顶层条目变化（可能遗留临时文件）\n用例: {label}",
        )
        self.assertFalse(
            (snapshot / "manifest.json.tmp").exists(),
            f"拒绝后遗留 manifest.json.tmp\n用例: {label}",
        )

    def run_reject_case(self, label, bad_path, selections=None):
        """准备损坏快照、拍摄目录基准、执行 restore 并断言拒绝与不变性。"""
        self.tamper_second_path(bad_path)

        # 基准在夹具全部就绪后、调用 restore 前拍摄。
        work_before = capture_tree(self.work)
        listing_before = sorted(os.listdir(self.snapshot))

        argv = ["restore", str(self.snapshot), str(self.dest)]
        if selections:
            for sel in selections:
                argv.extend(["--file", sel])
        proc = run_cmd(argv)
        self.assert_rejected(
            proc, bad_path, label, self.dest, selections=selections
        )
        self.assert_work_intact(
            self.work, self.snapshot, work_before, listing_before, label
        )

    # ---- 正常对照：中文/空格路径与内容含零字节的二进制 ----

    def test_valid_snapshot_restores_with_zero_byte_content(self):
        """有效版本 1 快照恢复成功：内容中的零字节是数据而非损坏路径，
        恢复后中文、空格路径与全部字节保持不变。"""
        proc = run_cmd(["restore", str(self.snapshot), str(self.dest)])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        context = f"stdout={stdout!r}\nstderr={stderr!r}"

        self.assertEqual(proc.returncode, 0, f"正常对照应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(str(self.dest.resolve()), stdout)
        self.assertIn("已恢复文件数: 2", stdout)

        for rel, data in BACKUP_FILES.items():
            self.assertEqual(
                (self.dest / rel).read_bytes(), data,
                f"恢复字节与快照不一致: {rel}",
            )
        restored_bin = (self.dest / BIN_REL).read_bytes()
        self.assertIn(0, restored_bin, "二进制内容中的零字节必须保留")

    # ---- 失败：版本 1 快照演示（空字符位于目录/文件名中间）----

    def test_reject_nul_in_middle_demo(self):
        """保留有效 note.txt 在前，另一条 path 为
        “中文 目录/坏\\u0000文件.bin”：整体拒绝、不创建 DEST。"""
        self.run_reject_case("空字符在中间（演示）", BAD_PATH_MIDDLE)

    # ---- 失败：空字符位于开头 ----

    def test_reject_nul_at_start(self):
        """path 以空字符开头时结果与中间情形完全相同。"""
        self.run_reject_case("空字符在开头", BAD_PATH_START)

    # ---- 失败：空字符位于结尾 ----

    def test_reject_nul_at_end(self):
        """path 以空字符结尾时结果与中间情形完全相同。"""
        self.run_reject_case("空字符在结尾", BAD_PATH_END)

    # ---- 失败：一条路径含多个空字符 ----

    def test_reject_multiple_nuls(self):
        """一条 path 含多个空字符时同样整体拒绝。"""
        self.run_reject_case("多个空字符", BAD_PATH_MULTIPLE)

    # ---- 失败：--file 只选择有效文件，未选条目含空字符 ----

    def test_reject_unselected_nul_entry_when_selecting_valid_file(self):
        """即使 --file 只选择有效 note.txt，未选中的损坏条目仍使清单
        整体校验失败：退出码 2、不创建 DEST、无部分恢复。"""
        for label, bad_path in (
            ("中间", BAD_PATH_MIDDLE),
            ("开头", BAD_PATH_START),
            ("结尾", BAD_PATH_END),
            ("多个", BAD_PATH_MULTIPLE),
        ):
            with self.subTest(position=label):
                # 每个子用例需要独立的快照与恢复目标。
                self._run_fresh_selection_case(bad_path, f"--file 选择/{label}")

    def _run_fresh_selection_case(self, bad_path, label):
        """在独立临时工作区中准备损坏快照并只选择 note.txt 恢复。"""
        with tempfile.TemporaryDirectory(prefix="restore-nul-sel-") as work:
            work = Path(work)
            source = work / "source"
            snapshot = work / "snapshot"
            dest = work / "restored"
            write_files(source, BACKUP_FILES)
            proc = run_cmd(["backup", str(source), str(snapshot)])
            self.assertEqual(
                proc.returncode, 0,
                f"夹具：快照创建失败\n{proc.stderr.decode('utf-8', 'replace')}",
            )
            # 快照/恢复目录之外的已有文件，拒绝前后必须字节不变。
            (work / "outside.txt").write_bytes(b"outside marker\n")

            manifest = json.loads(
                (snapshot / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["files"][0]["path"], NOTE_REL)
            manifest["files"][1]["path"] = bad_path
            (snapshot / "manifest.json").write_text(
                dump_manifest_with_escaped_nul(manifest), encoding="utf-8"
            )

            work_before = capture_tree(work)
            listing_before = sorted(os.listdir(snapshot))
            proc = run_cmd([
                "restore", str(snapshot), str(dest),
                "--file", NOTE_REL,
            ])
            self.assert_rejected(
                proc, bad_path, label, dest, selections=[NOTE_REL]
            )
            self.assert_work_intact(
                work, snapshot, work_before, listing_before, label
            )


if __name__ == "__main__":
    unittest.main()
