#!/usr/bin/env python3
"""restore 与 verify 对清单 path 含 U+0000 空字符的整体拒绝回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH]...
    python backup.py verify SNAPSHOT

只观察退出码、标准输出、标准错误与文件原始字节，不调用 backup.py 的
任何内部校验函数。仅依赖 Python 3 标准库；全部源目录、快照与恢复目标
在独立临时目录中运行时准备，用例结束自动清理，不读写用户现有目录，
不依赖网络、第三方库、符号链接权限或固定机器路径。

覆盖约定：

1. 演示用例：公开 backup 命令生成版本 1 快照，保留有效条目 note.txt
   在前，把第二条目 path 改为 JSON 字符串 "中文 目录/坏\\u0000文件.bin"
   （JSON 解码后含实际空字符）。restore 与 verify 均退出码 2、标准输出
   为空、标准错误含“清单路径包含空字符”与该路径的 JSON 字符串形式
   （空字符呈现为字面 \\u0000，不输出实际空字符，无异常堆栈），
   restore 不创建 DEST。
2. 空字符位于开头、结尾与多次出现（含同一条目多个空字符）结果相同。
3. restore 即使通过 --file 只选择有效文件，只要清单中未选条目含空
   字符也整体拒绝，且不创建 DEST；verify 不输出部分统计。
4. 正常对照：中文、空格路径与含零字节（0x00）内容的二进制文件照常
   备份、恢复与校验，文件内容中的零字节不属于损坏路径，恢复后字节
   不变；verify 输出原有 JSON 统计。
5. 所有失败用例以清单篡改完成后的目录状态为基准，比较调用前后源
   目录、快照与整个临时工作区：相对路径集合、条目类型与普通文件字节
   一致，不产生临时文件，目录外已有文件保持原样。
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
BIN_REL = "嵌套 目录/二进制 文件.bin"

# 备份时的固定内容：文本与含零字节（0x00）的二进制内容。
# 文件内容中的零字节是合法数据，不属于损坏路径。
NOTE_BYTES = "笔记内容\n第二行\n".encode("utf-8")
BINARY_BYTES = bytes(range(256))

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
}

# 演示用例的损坏路径：JSON 字符串 "中文 目录/坏\u0000文件.bin"。
DEMO_BAD_PATH = "中文 目录/坏\x00文件.bin"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_NUL = "清单路径包含空字符"


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


def json_form(path):
    """路径的 JSON 字符串形式：空字符呈现为字面 \\u0000 转义序列。"""
    return json.dumps(path, ensure_ascii=False)


class ManifestNulPathTests(unittest.TestCase):
    """清单 path 含空字符时 restore 与 verify 的确定性公开结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照、恢复目标及任何意外产物
        # 都受限于此，用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="nul-path-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"

        write_files(self.source, BACKUP_FILES)

    # ---- 通用工具与断言 ----

    def make_snapshot(self, name="snapshot"):
        """用公开 backup 命令在工作区内新建无摘要快照，失败则中止本用例。"""
        snapshot = self.work / name
        proc = run_cmd(["backup", str(self.source), str(snapshot)])
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

    def prepare_nul_snapshot(self, bad_path, label):
        """从公开 backup 命令生成的有效无摘要版本 1 快照出发，保留有效
        条目 note.txt 在前，仅把第二条目的 path 改为含空字符的 bad_path，
        保持合法 JSON、整数版本 1 与原有数据完整。返回篡改后的快照。"""
        snapshot = self.make_snapshot(f"snap-{label}")
        manifest = self.read_manifest(snapshot)
        # 测试前提：起点是版本 1、两个无摘要条目的有效快照，note.txt 在前。
        self.assertEqual(
            manifest.get("version"), 1,
            f"测试前提：{label} 起点清单版本应为整数 1",
        )
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            sorted(BACKUP_FILES),
            f"测试前提：{label} 起点清单应恰好包含两个相对路径",
        )
        self.assertEqual(
            manifest["files"][0]["path"], NOTE_REL,
            f"测试前提：{label} 有效条目 note.txt 应排在错误条目前面",
        )
        self.assertTrue(
            all(set(item) == {"path"} for item in manifest["files"]),
            f"测试前提：{label} 起点条目均不应带 sha256 字段",
        )

        manifest["files"][1]["path"] = bad_path
        self.write_manifest(snapshot, manifest)

        # 篡改后重新读取确认：清单仍是合法 JSON、版本仍为整数 1、损坏
        # 路径中的空字符是 JSON 解码后的实际字符，保证拒绝原因只可能
        # 来自被改动的 path 内容本身。
        after = self.read_manifest(snapshot)
        self.assertEqual(after.get("version"), 1)
        self.assertEqual(len(after["files"]), 2)
        self.assertEqual(after["files"][0]["path"], NOTE_REL)
        self.assertEqual(
            after["files"][1]["path"], bad_path,
            f"测试前提：{label} 篡改后第二条目应为损坏路径",
        )
        self.assertIn(
            "\x00", after["files"][1]["path"],
            f"测试前提：{label} 损坏路径应含 JSON 解码后的实际空字符",
        )
        return snapshot

    def assert_rejected(self, proc, bad_path, label, dest=None):
        """失败公共断言：退出码 2、stdout 为空、stderr 含原因与路径的
        JSON 字符串形式（字面 \\u0000），无实际空字符与异常堆栈；
        传入 dest 时确认恢复目标未被创建。"""
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n损坏路径: {json_form(bad_path)}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            stdout, "",
            f"失败后标准输出应为空，不得输出部分统计或局部成功结果\n{context}",
        )
        self.assertIn(
            REASON_NUL, stderr,
            f"标准错误缺少原因“{REASON_NUL}”\n{context}",
        )
        self.assertIn(
            json_form(bad_path), stderr,
            f"标准错误应包含原始路径的 JSON 字符串形式"
            f"（空字符为字面 \\u0000）\n{context}",
        )
        self.assertNotIn(
            "\x00", stderr,
            f"标准错误不得输出实际空字符\n{context}",
        )
        self.assertNotIn(
            "Traceback", stderr,
            f"标准错误不得输出异常堆栈\n{context}",
        )
        if dest is not None:
            self.assertFalse(
                os.path.lexists(dest),
                f"失败后恢复目标仍被创建: {dest}\n{context}",
            )
        return context

    def assert_work_intact(self, baselines, context):
        """调用前后源目录、快照与整个工作区保持原样，不产生临时文件。"""
        for path, before in baselines:
            self.assertEqual(
                capture_tree(path), before,
                f"调用前后目录树发生变化: {path}\n{context}",
            )

    def run_nul_case(self, bad_path, label):
        """单个损坏路径用例的统一流程：restore 与 verify 均整体拒绝，
        且源目录、快照与整个工作区相对调用前（篡改完成之后）不变。"""
        snapshot = self.prepare_nul_snapshot(bad_path, label)
        dest = self.work / f"restored-{label}"
        self.assertFalse(os.path.lexists(dest))

        # 基准取自清单篡改完成之后：此后的拒绝不得改动任何内容。
        baselines = [
            (self.source, capture_tree(self.source)),
            (snapshot, capture_tree(snapshot)),
            (self.work, capture_tree(self.work)),
        ]

        proc = run_cmd(["restore", str(snapshot), str(dest)])
        context = self.assert_rejected(proc, bad_path, f"{label}/restore",
                                       dest=dest)
        self.assert_work_intact(baselines, context)

        proc = run_cmd(["verify", str(snapshot)])
        context = self.assert_rejected(proc, bad_path, f"{label}/verify")
        self.assert_work_intact(baselines, context)

    # ---- 演示用例：path 为 JSON 字符串 "中文 目录/坏\u0000文件.bin" ----

    def test_reject_nul_in_middle(self):
        """有效 note.txt 条目在前，第二条目 path 含空字符（中文与空格
        目录名之间）：restore 与 verify 均整体拒绝。"""
        self.run_nul_case(DEMO_BAD_PATH, "nul-middle")

    # ---- 空字符位于开头、结尾与多次出现 ----

    def test_reject_nul_at_start(self):
        """空字符位于 path 开头：同样的拒绝结果。"""
        self.run_nul_case("\x00中文 目录/坏文件.bin", "nul-start")

    def test_reject_nul_at_end(self):
        """空字符位于 path 结尾：同样的拒绝结果。"""
        self.run_nul_case("中文 目录/坏文件.bin\x00", "nul-end")

    def test_reject_multiple_nul_in_one_path(self):
        """同一条 path 含多个空字符：同样的拒绝结果。"""
        self.run_nul_case("坏\x00文件\x00.bin", "nul-multiple")

    # ---- restore --file 只选有效文件时仍整体拒绝 ----

    def test_restore_file_selection_still_rejects_nul_entry(self):
        """--file 只选择有效条目 note.txt，未选条目含空字符仍整体拒绝，
        且不创建 DEST。"""
        snapshot = self.prepare_nul_snapshot(DEMO_BAD_PATH, "nul-selected")
        dest = self.work / "restored-selected"
        self.assertFalse(os.path.lexists(dest))

        baselines = [
            (self.source, capture_tree(self.source)),
            (snapshot, capture_tree(snapshot)),
            (self.work, capture_tree(self.work)),
        ]

        proc = run_cmd(
            ["restore", str(snapshot), str(dest), "--file", NOTE_REL]
        )
        context = self.assert_rejected(
            proc, DEMO_BAD_PATH, "restore --file 仍整体拒绝", dest=dest,
        )
        self.assert_work_intact(baselines, context)

    # ---- 正常对照：中文、空格路径与含零字节的二进制内容 ----

    def test_valid_snapshot_roundtrip_with_zero_bytes_in_content(self):
        """有效版本 1 快照（中文、空格路径，二进制内容含零字节）：
        verify 输出原有 JSON 统计，restore 成功且字节不变。"""
        snapshot = self.make_snapshot("snap-valid")
        manifest = self.read_manifest(snapshot)
        # 测试前提：清单为版本 1、两个无摘要条目，路径含中文与空格。
        self.assertEqual(manifest.get("version"), 1)
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            sorted(BACKUP_FILES),
        )
        self.assertTrue(
            all("sha256" not in item for item in manifest["files"])
        )
        # 测试前提：二进制内容确实含零字节（内容零字节不是损坏路径）。
        self.assertIn(0, BINARY_BYTES)

        # verify：退出码 0、stderr 空、stdout 仅一个结果 JSON，2/0/2。
        proc = run_cmd(["verify", str(snapshot)])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        self.assertEqual(
            proc.returncode, 0,
            f"verify 应返回 0\nstdout={stdout!r}\nstderr={stderr!r}",
        )
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{stderr!r}")
        result = json.loads(stdout)
        self.assertEqual(result["snapshot"], str(snapshot.resolve()))
        self.assertEqual(result["files"], 2)
        self.assertEqual(result["verified"], 0)
        self.assertEqual(result["unchecked"], 2)

        # restore：退出码 0，恢复后相对路径集合与字节逐字不变。
        dest = self.work / "restored-valid"
        proc = run_cmd(["restore", str(snapshot), str(dest)])
        stdout = proc.stdout.decode("utf-8")
        stderr = proc.stderr.decode("utf-8")
        self.assertEqual(
            proc.returncode, 0,
            f"restore 应返回 0\nstdout={stdout!r}\nstderr={stderr!r}",
        )
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{stderr!r}")
        self.assertIn("已恢复文件数: 2", stdout)

        restored = {
            key: value[1]
            for key, value in capture_tree(dest).items()
            if value[0] == "file"
        }
        self.assertEqual(set(restored), set(BACKUP_FILES))
        for rel, data in BACKUP_FILES.items():
            self.assertEqual(
                restored[rel], data,
                f"恢复后文件字节与源不一致: {rel}",
            )
        # 显式固定公开语义：内容中的零字节逐字节保留。
        restored_bin = (dest / "嵌套 目录" / "二进制 文件.bin").read_bytes()
        self.assertEqual(restored_bin, BINARY_BYTES)
        self.assertIn(0, restored_bin)


if __name__ == "__main__":
    unittest.main()
