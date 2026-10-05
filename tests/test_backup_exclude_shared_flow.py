#!/usr/bin/env python3
"""backup 两类排除共用同一校验流程后的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum] [--exclude PATH]...
                            [--exclude-dir PATH]... [--dry-run]

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中
运行时准备，用例结束自动清理，不读写用户现有目录，也不依赖网络或
额外安装包。

所有用例共用同一个固定小目录（字节预先固定）：

- ``note.txt``：文本；
- ``cache/a.txt``：将被单文件排除的文件（同时位于被排除目录内）；
- ``cache/sub/b.bin``：将随被排除子目录一起排除的二进制文件；
- ``cache-old/a.txt``：名称前缀相近、不得被排除 cache 误伤的文件；
- ``empty/``：存在的空目录，合法的目录排除匹配项。

覆盖约定：

1. 组合排除（重复排除 cache，并排除其子目录 cache/sub、空目录 empty，
   另以 --exclude 排除 cache/a.txt）：预览始终只收录
   ``cache-old/a.txt`` 与 ``note.txt``（Unicode 码点升序），预览只输出
   原有一行 JSON、不创建任何目标（含不存在的父目录）。
2. 源数据在预览后保持不变时，带 --checksum 实际备份到新目标：退出码与
   成功输出不变，实际清单路径与文件数和预览完全一致，摘要仅对应收录
   文件且与源字节一致，被排除区域不出现在 data 中。
3. 合法地排除全部文件仍成功：files 为空数组、data 为空目录、文件数 0，
   该空快照可恢复为空目录。
4. 两类非法参数同时出现并交换命令行排列顺序：首个错误始终是文件排除
   的“排除路径无效”，目录参数即使排在前面也不先报错；形态合法但两类
   都未匹配时，首个错误同样始终是“排除项未匹配普通文件”。
5. 同类多个非法值按输入顺序报告首项。
6. 既有目标拒绝覆盖、目标不得位于源目录内的行为在组合排除参数下保持
   不变；所有失败用例标准输出为空、不创建快照、源目录保持原样。
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

# 固定小目录中的相对路径（斜杠分隔）。
NOTE_REL = "note.txt"
CACHE_A_REL = "cache/a.txt"
CACHE_SUB_REL = "cache/sub"
CACHE_B_REL = "cache/sub/b.bin"
CACHE_OLD_A_REL = "cache-old/a.txt"
EMPTY_DIR_REL = "empty"

# 预先固定的文件字节。
NOTE_BYTES = "note.txt 的固定内容\n".encode("utf-8")
CACHE_A_BYTES = b"cache/a\n"
CACHE_B_BYTES = bytes([0x00, 0x01, 0xFE, 0xFF])
CACHE_OLD_A_BYTES = b"cache-old/a\n"

FIXTURE_FILES = {
    NOTE_REL: NOTE_BYTES,
    CACHE_A_REL: CACHE_A_BYTES,
    CACHE_B_REL: CACHE_B_BYTES,
    CACHE_OLD_A_REL: CACHE_OLD_A_BYTES,
}

# 组合排除：重复排除 cache、排除其子目录、空目录，以及一个单文件排除。
COMBINED_EXCLUDE_DIRS = ["cache", "cache", CACHE_SUB_REL, EMPTY_DIR_REL]
COMBINED_EXCLUDES = [CACHE_A_REL]

# 收录结果始终只有这两个路径（Unicode 码点升序：c < n）。
EXPECTED_PATHS = [CACHE_OLD_A_REL, NOTE_REL]

ERROR_PREFIX = "错误"
REASON_INVALID_FILE = "排除路径无效"
REASON_INVALID_DIR = "排除目录路径无效"
REASON_UNMATCHED_FILE = "排除项未匹配普通文件"
REASON_UNMATCHED_DIR = "排除目录未匹配普通目录"
REASON_SNAPSHOT_EXISTS = "快照路径已存在，拒绝覆盖"
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

    类型为 "dir" / "file" / "symlink"；普通文件记录完整字节，空目录
    同样记录为 ("dir", None)。
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


class BackupExclusionSharedFlowTests(unittest.TestCase):
    """两类排除共用校验流程后的组合场景、错误优先级与现场不变性。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-excl-flow-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        # 预览目标的父目录同样故意不存在，用于核对预览不创建任何目标。
        self.preview_snapshot = self.work / "nested" / "preview-snap"
        self.snapshot = self.work / "snap"

        write_files(self.source, FIXTURE_FILES)
        # 存在的空目录：合法的 --exclude-dir 匹配项，且不出现在收录结果里。
        (self.source / EMPTY_DIR_REL).mkdir()

    # ---- 通用执行与断言 ----

    def run_backup(self, snapshot, *, exclude_dirs=(), excludes=(),
                   dry_run=False, checksum=False):
        """以公开入口执行 backup，--exclude 统一排在 --exclude-dir 之前。

        需要混合控制两类参数在命令行中的排列顺序时，改用
        run_backup_ordered。
        """
        argv = ["backup", str(self.source), str(snapshot)]
        if checksum:
            argv.append("--checksum")
        for raw in excludes:
            argv.extend(["--exclude", raw])
        for raw in exclude_dirs:
            argv.extend(["--exclude-dir", raw])
        if dry_run:
            argv.append("--dry-run")
        return run_cmd(argv)

    def run_backup_ordered(self, snapshot, ordered_args, *, dry_run=False):
        """按 ordered_args 中 (kind, raw) 的顺序混合排列两类排除参数。"""
        argv = ["backup", str(self.source), str(snapshot)]
        for kind, raw in ordered_args:
            argv.extend([f"--exclude{'-dir' if kind == 'dir' else ''}", raw])
        if dry_run:
            argv.append("--dry-run")
        return run_cmd(argv)

    def parse_preview(self, proc):
        """成功预览的标准输出必须是单行 JSON 对象加末尾换行。"""
        context = (
            f"exit={proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
        self.assertEqual(proc.returncode, 0, f"预览应退出 0\n{context}")
        self.assertEqual(proc.stderr, b"", f"成功预览标准错误应为空\n{context}")
        self.assertTrue(
            proc.stdout.endswith(b"\n"), f"预览输出应以换行结束\n{context}"
        )
        line = proc.stdout[:-1]
        self.assertNotIn(b"\n", line, f"预览输出应只有一行\n{context}")
        doc = json.loads(line.decode("utf-8"))
        self.assertEqual(
            set(doc.keys()), {"source", "snapshot", "files", "paths"},
            f"预览 JSON 只应含既有四个字段\n{context}",
        )
        return doc

    def read_manifest(self, snapshot=None):
        """读取快照 manifest.json 的 JSON 文档（测试侧独立解析）。"""
        manifest_path = (snapshot or self.snapshot) / "manifest.json"
        with open(manifest_path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    def assert_failure_leaves_no_trace(self, proc, snapshot, source_before,
                                       reasons, context, *,
                                       target_may_exist=False):
        """失败公共断言：退出码 2、stdout 空、原因齐全、源不变。

        target_may_exist 为 False（默认）时还要求快照路径不存在；
        目标已存在的拒绝用例传 True，改为由用例自行核对目标未被改动。
        """
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context += (
            f"\nexit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )
        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少拒绝原因“{reason}”\n{context}",
            )
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        if not target_may_exist:
            self.assertFalse(
                os.path.lexists(snapshot),
                f"失败后快照路径仍被创建: {snapshot}\n{context}",
            )
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"失败后源目录发生变化\n{context}",
        )
        return stdout, stderr

    # ---- 预览：组合排除始终只收录两个文件，且不创建任何目标 ----

    def test_preview_combined_exclusions_lists_only_two_paths(self):
        source_before = capture_tree(self.source)

        proc = self.run_backup(
            self.preview_snapshot,
            exclude_dirs=COMBINED_EXCLUDE_DIRS,
            excludes=COMBINED_EXCLUDES,
            dry_run=True,
        )
        doc = self.parse_preview(proc)

        self.assertEqual(doc["source"], str(self.source.resolve()))
        self.assertEqual(
            doc["snapshot"], str(self.preview_snapshot.resolve())
        )
        self.assertEqual(doc["files"], len(EXPECTED_PATHS))
        self.assertEqual(doc["paths"], EXPECTED_PATHS)

        # 预览不创建快照目标及其父目录，不产生任何副产物，源目录不变。
        self.assertFalse(os.path.lexists(self.preview_snapshot))
        self.assertFalse(os.path.lexists(self.preview_snapshot.parent))
        self.assertEqual(
            sorted(p.name for p in self.work.iterdir()), ["source"]
        )
        self.assertEqual(capture_tree(self.source), source_before)

    def test_preview_combined_exclusions_with_checksum_flag_identical(self):
        """--checksum 与预览同用：收录计划完全一致，仍不创建任何目标。"""
        proc = self.run_backup(
            self.preview_snapshot,
            exclude_dirs=COMBINED_EXCLUDE_DIRS,
            excludes=COMBINED_EXCLUDES,
            dry_run=True,
            checksum=True,
        )
        doc = self.parse_preview(proc)
        self.assertEqual(doc["files"], 2)
        self.assertEqual(doc["paths"], EXPECTED_PATHS)
        self.assertFalse(os.path.lexists(self.preview_snapshot))
        self.assertFalse(os.path.lexists(self.preview_snapshot.parent))

    # ---- 预览后源数据不变，带 --checksum 实际备份：清单与预览一致 ----

    def test_preview_then_checksum_backup_matches_preview(self):
        # 先对实际目标做一次预览，确认目标尚不存在且预览不创建它。
        proc = self.run_backup(
            self.snapshot,
            exclude_dirs=COMBINED_EXCLUDE_DIRS,
            excludes=COMBINED_EXCLUDES,
            dry_run=True,
        )
        preview = self.parse_preview(proc)
        self.assertFalse(os.path.lexists(self.snapshot))
        source_after_preview = capture_tree(self.source)

        # 源数据保持不变时，带 --checksum 实际备份到同一目标。
        proc = self.run_backup(
            self.snapshot,
            exclude_dirs=COMBINED_EXCLUDE_DIRS,
            excludes=COMBINED_EXCLUDES,
            checksum=True,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"实际备份阶段:\nexit={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )
        self.assertEqual(proc.returncode, 0, f"实际备份应退出 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"标准输出应包含快照目录绝对路径\n{context}",
        )

        # 文件数与预览一致；源目录在预览与备份前后均未改动。
        self.assertEqual(
            f"已备份文件数: {preview['files']}",
            next(line for line in stdout.splitlines()
                 if line.startswith("已备份文件数")),
            f"实际文件数应与预览一致\n{context}",
        )
        self.assertEqual(
            capture_tree(self.source), source_after_preview,
            f"实际备份后源目录发生变化\n{context}",
        )

        # 清单：版本 1、无新增字段、路径与文件数和预览完全一致。
        doc = self.read_manifest()
        self.assertEqual(
            set(doc.keys()), {"version", "files"},
            f"清单顶层不应新增排除规则字段\n{context}",
        )
        self.assertEqual(doc["version"], 1, f"清单版本应为 1\n{context}")
        manifest_paths = [item["path"] for item in doc["files"]]
        self.assertEqual(
            manifest_paths, preview["paths"],
            f"实际清单路径应与预览完全一致\n{context}",
        )
        self.assertEqual(len(doc["files"]), preview["files"])

        # 摘要仅对应收录文件，且与源文件字节一致；条目不含其他字段。
        expected_bytes = {
            CACHE_OLD_A_REL: CACHE_OLD_A_BYTES,
            NOTE_REL: NOTE_BYTES,
        }
        for item in doc["files"]:
            self.assertEqual(
                set(item.keys()), {"path", "sha256"},
                f"清单条目只应含 path 与 sha256\n{context}",
            )
            self.assertEqual(
                item["sha256"],
                sha256_hex(expected_bytes[item["path"]]),
                f"摘要应与收录文件源字节一致: {item['path']}\n{context}",
            )

        # data 中恰好只有两个收录文件的原始字节；被排除的 cache 与空
        # 目录 empty 完全不出现，cache-old 不被 cache 的前缀匹配误伤。
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {
                "cache-old": ("dir", None),
                CACHE_OLD_A_REL: ("file", CACHE_OLD_A_BYTES),
                NOTE_REL: ("file", NOTE_BYTES),
            },
            f"data 应只含两个收录文件及其父目录\n{context}",
        )
        self.assertFalse(
            (self.snapshot / "data" / "cache").exists(),
            f"被排除的 cache 不应出现在 data 中\n{context}",
        )
        self.assertFalse(
            (self.snapshot / "data" / EMPTY_DIR_REL).exists(),
            f"被排除的空目录不应出现在 data 中\n{context}",
        )

    # ---- 合法排除全部文件：空清单、空 data，可恢复为空目录 ----

    def test_legitimately_exclude_everything_succeeds_empty(self):
        source_before = capture_tree(self.source)

        # cache 与 cache-old 两个目录覆盖全部目录内文件，note.txt 用
        # --exclude 单文件排除；empty 是空目录，排不排除都不影响文件数。
        proc = self.run_backup(
            self.snapshot,
            exclude_dirs=["cache", "cache-old", EMPTY_DIR_REL],
            excludes=[NOTE_REL],
            checksum=True,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"全部文件合法排除:\nexit={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )
        self.assertEqual(proc.returncode, 0, f"应退出 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            "已备份文件数: 0", stdout,
            f"全部排除时应报告文件数 0\n{context}",
        )

        doc = self.read_manifest()
        self.assertEqual(doc["version"], 1)
        self.assertEqual(doc["files"], [], f"清单 files 应为空数组\n{context}")
        data_dir = self.snapshot / "data"
        self.assertTrue(data_dir.is_dir(), f"data 目录应存在\n{context}")
        self.assertEqual(
            capture_tree(data_dir), {}, f"data 应为空目录\n{context}"
        )

        # 空快照仍可恢复为空目录。
        restored = self.work / "restored-empty"
        proc = run_cmd(["restore", str(self.snapshot), str(restored)])
        r_stderr = proc.stderr.decode("utf-8", errors="replace")
        context += f"\n恢复阶段:\nexit={proc.returncode}\nstderr={r_stderr!r}"
        self.assertEqual(proc.returncode, 0, f"空快照恢复应退出 0\n{context}")
        self.assertTrue(restored.is_dir(), f"恢复目标应为目录\n{context}")
        self.assertEqual(
            capture_tree(restored), {}, f"应恢复为空目录\n{context}"
        )
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"源目录应保持原样\n{context}",
        )

    # ---- 两类非法参数同时出现且交换排列：首错误始终是文件排除 ----

    def assert_file_invalid_reported_first_regardless_of_order(
        self, ordered_a, ordered_b, *, file_reason, file_raw, dir_reason
    ):
        """两种参数排列下，首个错误都属于文件排除且两次报错完全一致。"""
        self.assertFalse(os.path.lexists(self.snapshot))
        source_before = capture_tree(self.source)
        stderrs = []

        for label, ordered in (("目录参数在前", ordered_a),
                               ("文件参数在前", ordered_b)):
            proc = self.run_backup_ordered(
                self.snapshot, ordered, dry_run=True
            )
            stdout, stderr = self.assert_failure_leaves_no_trace(
                proc, self.snapshot, source_before,
                [file_reason, file_raw],
                f"两类参数同时非法（{label}）: {ordered!r}",
            )
            # 一次只报告首个错误：目录排除的原因不得出现。
            self.assertNotIn(
                dir_reason, stderr,
                f"{label}时不应先报目录排除原因\n排列: {ordered!r}\n{stderr!r}",
            )
            stderrs.append(stderr)

        # 交换排列顺序后，首个错误保持一致（整行报错逐字相同）。
        self.assertEqual(
            stderrs[0], stderrs[1],
            f"交换排列后首个错误不一致:\n{stderrs[0]!r}\n{stderrs[1]!r}",
        )

    def test_invalid_values_both_kinds_file_reported_first(self):
        bad_file = "../bad-file.txt"
        bad_dir = "../bad-dir"
        # 排列 A：目录参数在命令行中更靠前；排列 B：文件参数更靠前。
        ordered_a = [("dir", bad_dir), ("file", bad_file)]
        ordered_b = [("file", bad_file), ("dir", bad_dir)]
        self.assert_file_invalid_reported_first_regardless_of_order(
            ordered_a, ordered_b,
            file_reason=REASON_INVALID_FILE,
            file_raw=bad_file,
            dir_reason=REASON_INVALID_DIR,
        )

    def test_unmatched_values_both_kinds_file_reported_first(self):
        """形态均合法但两类都未匹配：匹配阶段同样先报文件排除。"""
        missing_file = "no-such-file.txt"
        missing_dir = "no-such-dir"
        ordered_a = [("dir", missing_dir), ("file", missing_file)]
        ordered_b = [("file", missing_file), ("dir", missing_dir)]
        self.assert_file_invalid_reported_first_regardless_of_order(
            ordered_a, ordered_b,
            file_reason=REASON_UNMATCHED_FILE,
            file_raw=missing_file,
            dir_reason=REASON_UNMATCHED_DIR,
        )

    # ---- 同类多个非法值：按输入顺序报告首项 ----

    def test_first_invalid_file_value_reported_in_input_order(self):
        source_before = capture_tree(self.source)

        proc = self.run_backup(
            self.snapshot,
            excludes=["../first-invalid", ""],
            dry_run=True,
        )
        _, stderr = self.assert_failure_leaves_no_trace(
            proc, self.snapshot, source_before,
            [REASON_INVALID_FILE, "../first-invalid"],
            "文件排除多个非法值：首个按输入顺序",
        )
        # 只报告首个非法值，后续值不出现在报错中。
        self.assertEqual(stderr.count(REASON_INVALID_FILE), 1)

    def test_first_invalid_file_value_changes_with_input_order(self):
        """调换输入顺序后，首项改为空字符串值（仍为文件排除形态错误）。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup(
            self.snapshot,
            excludes=["", "../second-invalid"],
            dry_run=True,
        )
        self.assert_failure_leaves_no_trace(
            proc, self.snapshot, source_before,
            [REASON_INVALID_FILE],
            "文件排除多个非法值：空串排在首位",
        )
        self.assertNotIn(
            "../second-invalid", proc.stderr.decode("utf-8"),
            "不应报告排在后面的非法值",
        )

    # ---- 既有目标安全行为在组合排除参数下保持不变 ----

    def test_existing_target_still_rejected_with_combined_exclusions(self):
        self.snapshot.mkdir()
        marker = self.snapshot / "marker.txt"
        marker.write_bytes(b"existing marker\n")
        source_before = capture_tree(self.source)

        proc = self.run_backup(
            self.snapshot,
            exclude_dirs=COMBINED_EXCLUDE_DIRS,
            excludes=COMBINED_EXCLUDES,
            checksum=True,
        )
        self.assert_failure_leaves_no_trace(
            proc, self.snapshot, source_before,
            [REASON_SNAPSHOT_EXISTS],
            "目标已存在时拒绝覆盖",
            target_may_exist=True,
        )
        # 已有目标目录及其内容保持原样。
        self.assertEqual(marker.read_bytes(), b"existing marker\n")

    def test_target_inside_source_still_rejected_with_combined_exclusions(self):
        source_before = capture_tree(self.source)
        inside_snapshot = self.source / "snap-inside"

        proc = self.run_backup(
            inside_snapshot,
            exclude_dirs=COMBINED_EXCLUDE_DIRS,
            excludes=COMBINED_EXCLUDES,
        )
        self.assert_failure_leaves_no_trace(
            proc, inside_snapshot, source_before,
            [REASON_SNAPSHOT_INSIDE],
            "目标位于源目录内时拒绝",
        )
        self.assertFalse(
            os.path.lexists(inside_snapshot),
            "失败后源目录内不应出现快照目录",
        )


if __name__ == "__main__":
    unittest.main()
