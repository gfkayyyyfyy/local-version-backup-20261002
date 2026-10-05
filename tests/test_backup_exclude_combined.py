#!/usr/bin/env python3
"""backup 两类排除参数组合使用的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum] [--exclude PATH]...
                            [--exclude-dir PATH]... [--dry-run]

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照在独立临时目录中运行时准备，
用例结束自动清理，不读写用户现有目录，也不依赖网络或额外安装包。

夹具源目录（文件字节预先固定）：

- ``note.txt``：明确的 UTF-8 文本；
- ``cache/a.txt`` 与 ``cache/sub/b.bin``：嵌套目录中的两个文件；
- ``cache-old/a.txt``：与 cache 仅前缀相近的兄弟目录中的文件；
- ``empty/``：存在的空目录。

覆盖约定：

1. 组合排除：重复指定 ``--exclude-dir cache``，再排除 ``cache/sub``、
   ``empty`` 与单文件 ``cache/a.txt``，收录结果始终只有
   ``cache-old/a.txt`` 与 ``note.txt``，按 Unicode 码点升序。
2. 先预览后实际备份：--dry-run 退出码 0、标准错误为空、标准输出恰好
   一行 JSON（仅含 source、snapshot、files、paths），不创建任何目标；
   源数据不变时带 --checksum 实际备份，清单路径与文件数跟预览一致，
   摘要仅对应收录文件，data 只含收录文件的原始字节。
3. 合法排除全部文件仍成功：清单 files 为空数组、data 为空目录、
   报告文件数 0。
4. 两类非法参数同时出现时先报告文件排除（“排除路径无效”），即使命令中
   目录参数更靠前；交换参数排列顺序后首个错误保持一致。
5. 已有目标拒绝覆盖、目标不得位于源目录内的既有行为保持不变。
6. 所有失败用例：退出码 2、标准输出为空、不创建快照、源目录保持原样。
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

# 夹具中的相对路径（斜杠分隔）。
NOTE_REL = "note.txt"
CACHE_DIR_REL = "cache"
CACHE_A_REL = "cache/a.txt"
CACHE_B_REL = "cache/sub/b.bin"
OLD_DIR_REL = "cache-old"
OLD_A_REL = "cache-old/a.txt"
EMPTY_DIR_REL = "empty"

NOTE_BYTES = "note.txt：组合排除夹具\n".encode("utf-8")
CACHE_A_BYTES = "cache/a.txt：应被单文件排除\n".encode("utf-8")
CACHE_B_BYTES = bytes([0x00, 0xFF, 0x10])
OLD_A_BYTES = "cache-old/a.txt：应被收录\n".encode("utf-8")

DEMO_FILES = {
    NOTE_REL: NOTE_BYTES,
    CACHE_A_REL: CACHE_A_BYTES,
    CACHE_B_REL: CACHE_B_BYTES,
    OLD_A_REL: OLD_A_BYTES,
}

# 组合排除后应收录的相对路径，按 Unicode 码点升序。
EXPECTED_INCLUDED = sorted([OLD_A_REL, NOTE_REL])

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
BACKUP_SUMMARY_MARKERS = ("已创建快照目录", "已备份文件数")
ERROR_PREFIX = "错误"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_EXCLUDE_INVALID = "排除路径无效"
REASON_DIR_INVALID = "排除目录路径无效"
REASON_SNAPSHOT_EXISTS = "快照路径已存在"
REASON_SNAPSHOT_INSIDE = "快照目录不得位于源目录内"


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

    类型为 "dir" / "file" / "symlink"；普通文件记录完整字节。
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
    """字节串的 SHA-256，64 位小写十六进制。"""
    return hashlib.sha256(data).hexdigest()


class BackupExcludeCombinedTests(unittest.TestCase):
    """--exclude 与 --exclude-dir 组合的成功路径、失败顺序与现场不变性。"""

    def setUp(self):
        # 每个用例独立临时工作区，样例之间互不污染。
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-exclude-combined-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snap"

        write_files(self.source, DEMO_FILES)
        (self.source / EMPTY_DIR_REL).mkdir()

    # ---- 通用执行与断言 ----

    def exclusion_args(self):
        """组合排除参数：重复 cache、其子目录、空目录与目录内单文件。"""
        return [
            "--exclude-dir", CACHE_DIR_REL,
            "--exclude-dir", CACHE_DIR_REL,
            "--exclude-dir", "cache/sub",
            "--exclude-dir", EMPTY_DIR_REL,
            "--exclude", CACHE_A_REL,
        ]

    def run_backup(self, extra_args=None, checksum=False, dry_run=False,
                   snapshot=None):
        """以公开入口执行 backup；extra_args 按给定顺序原样追加。"""
        argv = ["backup", str(self.source), str(snapshot or self.snapshot)]
        if checksum:
            argv.append("--checksum")
        argv.extend(extra_args or [])
        if dry_run:
            argv.append("--dry-run")
        return run_cmd(argv)

    def assert_source_unchanged(self, source_before, context):
        """备份（无论成败）不得改动源目录的路径集合、类型与字节。"""
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"备份后源目录目录树发生变化\n{context}",
        )

    def assert_rejected(self, proc, reasons, label, snapshot=None):
        """失败用例公共断言：退出码 2、报错原因、标准输出为空、快照不存在。"""
        snapshot = snapshot or self.snapshot
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少拒绝原因“{reason}”\n{context}",
            )
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        for marker in BACKUP_SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        return stdout, stderr, context

    def read_manifest(self, snapshot=None):
        """读取快照 manifest.json 的 JSON 文档（测试侧独立解析）。"""
        manifest_path = (snapshot or self.snapshot) / "manifest.json"
        with open(manifest_path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    # ---- 成功：先预览，再带 --checksum 实际备份，结果一致 ----

    def test_preview_then_checksum_backup_matches(self):
        """组合排除：预览只输出一行 JSON 且不创建目标；实际清单与预览一致。"""
        source_before = capture_tree(self.source)

        # 第一步：预览。退出码 0、标准错误为空、标准输出恰好一行 JSON。
        proc = self.run_backup(self.exclusion_args(), checksum=True, dry_run=True)
        p_stdout = proc.stdout.decode("utf-8", errors="replace")
        p_stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 组合排除预览\n"
            f"exit={proc.returncode}\nstdout={p_stdout!r}\nstderr={p_stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"预览退出码应为 0\n{context}")
        self.assertEqual(p_stderr, "", f"预览成功时标准错误应为空\n{context}")
        self.assertTrue(
            p_stdout.endswith("\n") and "\n" not in p_stdout[:-1],
            f"预览标准输出应恰好一行 JSON 加末尾换行\n{context}",
        )
        preview = json.loads(p_stdout)
        self.assertEqual(
            set(preview.keys()), {"source", "snapshot", "files", "paths"},
            f"预览 JSON 应只含 source、snapshot、files、paths\n{context}",
        )
        self.assertEqual(preview["source"], str(self.source.resolve()),
                         f"预览 source 应为解析后的绝对路径\n{context}")
        self.assertEqual(preview["snapshot"], str(self.snapshot.resolve()),
                         f"预览 snapshot 应为解析后的绝对路径\n{context}")
        self.assertEqual(preview["files"], 2,
                         f"预览文件数应为 2\n{context}")
        self.assertEqual(preview["paths"], EXPECTED_INCLUDED,
                         f"预览路径应为码点升序的两个收录文件\n{context}")

        # 预览不创建任何目标，源目录保持原样。
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"预览不应创建快照路径: {self.snapshot}\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

        # 第二步：源数据不变，带 --checksum 实际备份到同一目标。
        proc = self.run_backup(self.exclusion_args(), checksum=True)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context += (
            f"\n实际备份阶段:\nexit={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"实际备份退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"标准输出应包含快照目录绝对路径\n{context}",
        )
        self.assertIn("已备份文件数: 2", stdout,
                      f"标准输出应只计算收录文件数 2\n{context}")

        # data 恰好含两个收录文件的原始字节，排除区域完全不出现。
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {
                OLD_DIR_REL: ("dir", None),
                OLD_A_REL: ("file", OLD_A_BYTES),
                NOTE_REL: ("file", NOTE_BYTES),
            },
            f"快照 data 应只含 cache-old/a.txt 与 note.txt 的原始字节\n{context}",
        )

        # 清单：版本 1、路径与文件数跟预览一致、摘要仅对应收录文件。
        doc = self.read_manifest()
        self.assertEqual(
            set(doc.keys()), {"version", "files"},
            f"清单顶层不应新增排除规则字段\n{context}",
        )
        self.assertEqual(doc["version"], 1, f"清单版本应为 1\n{context}")
        paths = [item["path"] for item in doc["files"]]
        self.assertEqual(paths, preview["paths"],
                         f"实际清单路径应与预览一致\n{context}")
        self.assertEqual(len(paths), preview["files"],
                         f"实际清单文件数应与预览一致\n{context}")
        for item in doc["files"]:
            self.assertEqual(
                set(item.keys()), {"path", "sha256"},
                f"清单条目只应含 path 与 sha256\n{context}",
            )
        digests = {item["path"]: item["sha256"] for item in doc["files"]}
        self.assertEqual(
            digests,
            {
                OLD_A_REL: sha256_hex(OLD_A_BYTES),
                NOTE_REL: sha256_hex(NOTE_BYTES),
            },
            f"摘要应仅对应两个收录文件的原始字节\n{context}",
        )

        self.assert_source_unchanged(source_before, context)

    # ---- 成功：合法排除全部文件仍成功，得到空清单 ----

    def test_exclude_all_files_succeeds_with_empty_manifest(self):
        """组合排除覆盖全部文件：空 files 数组、空 data 目录、文件数 0。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup(
            [
                "--exclude-dir", CACHE_DIR_REL,
                "--exclude-dir", OLD_DIR_REL,
                "--exclude-dir", EMPTY_DIR_REL,
                "--exclude", NOTE_REL,
            ],
            checksum=True,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 全部文件被合法排除\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 0", stdout,
                      f"全部排除时应报告文件数 0\n{context}")

        doc = self.read_manifest()
        self.assertEqual(doc["version"], 1, f"清单版本应为 1\n{context}")
        self.assertEqual(doc["files"], [], f"清单 files 应为空数组\n{context}")
        data_dir = self.snapshot / "data"
        self.assertTrue(data_dir.is_dir(), f"data 目录应存在\n{context}")
        self.assertEqual(
            capture_tree(data_dir), {},
            f"全部排除时 data 应为空目录\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 失败：两类非法参数同时出现，先报告文件排除，与排列顺序无关 ----

    def test_both_invalid_file_exclusion_reported_first(self):
        """目录参数在命令中更靠前：仍先报告“排除路径无效”及原始参数。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup([
            "--exclude-dir", "../dir-bad",
            "--exclude", "../file-bad",
        ])
        _, stderr, context = self.assert_rejected(
            proc, [REASON_EXCLUDE_INVALID, "../file-bad"],
            "目录参数靠前时仍先报告文件排除",
        )
        self.assertNotIn(
            REASON_DIR_INVALID, stderr,
            f"首个错误应为文件排除，不应出现目录排除报错\n{context}",
        )
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"失败后快照路径仍被创建: {self.snapshot}\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    def test_both_invalid_swapped_order_same_first_error(self):
        """交换排列顺序（文件参数靠前）：首个错误与目录参数靠前时一致。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup([
            "--exclude", "../file-bad",
            "--exclude-dir", "../dir-bad",
        ])
        _, stderr, context = self.assert_rejected(
            proc, [REASON_EXCLUDE_INVALID, "../file-bad"],
            "文件参数靠前时先报告文件排除",
        )
        self.assertNotIn(
            REASON_DIR_INVALID, stderr,
            f"首个错误应为文件排除，不应出现目录排除报错\n{context}",
        )
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"失败后快照路径仍被创建: {self.snapshot}\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

        # 与目录参数靠前的排列对比：首个错误逐字一致。
        proc_dir_first = self.run_backup([
            "--exclude-dir", "../dir-bad",
            "--exclude", "../file-bad",
        ])
        dir_first_stderr = proc_dir_first.stderr.decode("utf-8", errors="replace")
        self.assertEqual(
            stderr, dir_first_stderr,
            f"交换排列顺序后首个错误应保持一致\n"
            f"文件靠前: {stderr!r}\n目录靠前: {dir_first_stderr!r}",
        )

    # ---- 失败：既有目标安全检查不被排除参数绕过 ----

    def test_reject_existing_snapshot_with_combined_exclusions(self):
        """快照路径已存在：即使排除参数全部合法也仍按既有规则拒绝。"""
        self.snapshot.mkdir()
        source_before = capture_tree(self.source)

        proc = self.run_backup(self.exclusion_args())
        self.assert_rejected(
            proc, [REASON_SNAPSHOT_EXISTS], "快照路径已存在",
        )
        self.assertEqual(capture_tree(self.snapshot), {},
                         f"已存在的快照目录不应被改动")
        self.assert_source_unchanged(source_before, "快照路径已存在")

    def test_reject_snapshot_inside_source_with_combined_exclusions(self):
        """快照位于源目录内：即使排除参数全部合法也仍按既有规则拒绝。"""
        source_before = capture_tree(self.source)
        inside_snapshot = self.source / "snap-inside"

        proc = self.run_backup(self.exclusion_args(), snapshot=inside_snapshot)
        self.assert_rejected(
            proc, [REASON_SNAPSHOT_INSIDE], "快照位于源目录内",
            snapshot=inside_snapshot,
        )
        self.assertFalse(
            os.path.lexists(inside_snapshot),
            f"失败后源目录内不应出现快照目录: {inside_snapshot}",
        )
        self.assert_source_unchanged(source_before, "快照位于源目录内")


if __name__ == "__main__":
    unittest.main()
