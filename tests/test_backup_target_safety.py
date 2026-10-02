#!/usr/bin/env python3
"""backup 目标路径安全边界的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
覆盖两项既有约定：

1. 快照目标路径已存在（普通文件 / 目录 / 符号链接）时拒绝覆盖；
2. 快照目标解析后位于源目录内时拒绝创建。

仅依赖 Python 3 标准库；全部夹具在临时目录中运行时准备，用例结束自动
清理，不读写用户现有目录，不依赖网络或额外安装包。环境不支持创建符号
链接时仅跳过相关用例，其余用例照常执行。
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

# 源目录固定内容：文本文件与嵌套中文目录下含零字节（0x00）的二进制文件。
SOURCE_FILES = {
    "note.txt": "笔记内容\n第二行\n".encode("utf-8"),
    "嵌套 目录/二进制 文件.bin": bytes(range(256)),
}

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建快照目录", "已备份文件数")
ERROR_PREFIX = "错误"

REASON_OVERWRITE = "快照路径已存在，拒绝覆盖"
REASON_INSIDE_SOURCE = "快照目录不得位于源目录内"


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


def snapshot_tree(root):
    """递归记录目录树：相对路径 -> (类型, 字节或链接目标)，与遍历顺序无关。"""
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


class BackupTargetSafetyTests(unittest.TestCase):
    """backup 对不安全目标路径必须在写入前整体拒绝。"""

    # ---- 通用断言与流程 ----

    def assertRejected(self, label, proc, reason):
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )
        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        self.assertIn(reason, stderr, f"标准错误缺少拒绝原因“{reason}”\n{context}")
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        return context

    def _reject_case(self, label, reason, prepare, check_target=None):
        """运行单个拒绝用例：每种用例只由 prepare 引入一种目标问题。

        prepare(work, source) 返回目标路径（字符串形式，可含 .. 分量）。
        调用前后对整个临时工作区拍摄树快照，确认源目录、现有目标及
        工作区内没有任何新增、覆盖或删除。
        """
        with tempfile.TemporaryDirectory(prefix="backup-target-test-") as work:
            work = Path(work)
            source = work / "source"
            write_files(source, SOURCE_FILES)

            # 每种用例只在此处引入唯一的目标问题。
            snapshot_arg = prepare(work, source)

            # 基线在夹具全部就绪后、调用 backup 前拍摄。
            work_before = snapshot_tree(work)

            proc = run_cmd(["backup", str(source), snapshot_arg])
            context = self.assertRejected(label, proc, reason)

            if check_target is not None:
                check_target(context)

            self.assertEqual(
                snapshot_tree(work), work_before,
                f"调用前后工作区内容发生变化（新增/覆盖/删除）\n{context}",
            )

    # ---- 约定一：目标已存在，拒绝覆盖 ----

    def test_reject_target_is_regular_file(self):
        """目标已是普通文件时拒绝覆盖。"""
        def prepare(work, source):
            target = work / "snapshot"
            target.write_bytes(b"occupied, do not overwrite\n")
            return str(target)

        self._reject_case(
            "目标为普通文件", REASON_OVERWRITE, prepare,
        )

    def test_reject_target_is_directory_with_marker(self):
        """目标已是含标记文件的目录时拒绝覆盖。"""
        def prepare(work, source):
            target = work / "snapshot"
            target.mkdir()
            (target / "marker.txt").write_bytes(b"keep me\n")
            return str(target)

        self._reject_case(
            "目标为含标记文件的目录", REASON_OVERWRITE, prepare,
        )

    def _make_symlink(self, link_path, link_target):
        """创建符号链接；环境不支持时跳过当前用例并说明原因。"""
        try:
            os.symlink(link_target, link_path)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"当前环境无法创建符号链接，跳过符号链接用例: {exc}")

    def test_reject_target_is_symlink_to_file(self):
        """目标是指向现有文件的符号链接时拒绝覆盖，链接与被指向文件均不变。"""
        pointed_bytes = b"pointed-to file, do not touch\n"

        def prepare(work, source):
            pointed = work / "pointed.txt"
            pointed.write_bytes(pointed_bytes)
            target = work / "snapshot"
            self._make_symlink(target, str(pointed))
            return str(target)

        self._reject_case(
            "目标为有效符号链接", REASON_OVERWRITE, prepare,
        )

    def test_reject_target_is_dangling_symlink(self):
        """目标是指向不存在路径的符号链接时同样拒绝覆盖。"""
        def prepare(work, source):
            target = work / "snapshot"
            self._make_symlink(target, str(work / "nonexistent-dest"))
            return str(target)

        self._reject_case(
            "目标为悬空符号链接", REASON_OVERWRITE, prepare,
        )

    # ---- 约定二：目标解析后位于源目录内，拒绝创建 ----

    def _assert_target_absent(self, snapshot_arg, context):
        self.assertFalse(
            os.path.lexists(snapshot_arg),
            f"失败后目标仍被创建: {snapshot_arg}\n{context}",
        )

    def test_reject_target_inside_source_direct_child(self):
        """源目录内尚不存在的直接子目录作为目标时拒绝。"""
        holder = {}

        def prepare(work, source):
            target = source / "snap"
            holder["arg"] = str(target)
            self.assertFalse(os.path.lexists(target))
            return str(target)

        self._reject_case(
            "目标为源目录直接子目录", REASON_INSIDE_SOURCE, prepare,
            check_target=lambda ctx: self._assert_target_absent(holder["arg"], ctx),
        )

    def test_reject_target_inside_source_nested(self):
        """源目录内尚不存在的嵌套目录作为目标时拒绝（父目录事先存在）。"""
        holder = {}

        def prepare(work, source):
            parent = source / "sub"
            parent.mkdir()
            target = parent / "snap"
            holder["arg"] = str(target)
            self.assertFalse(os.path.lexists(target))
            return str(target)

        self._reject_case(
            "目标为源目录嵌套子目录", REASON_INSIDE_SOURCE, prepare,
            check_target=lambda ctx: self._assert_target_absent(holder["arg"], ctx),
        )

    def test_reject_target_with_dotdot_resolving_into_source(self):
        """含 .. 分量且解析后落入源目录的目标同样拒绝。"""
        holder = {}

        def prepare(work, source):
            # 字面路径先走出再折返，解析后位于源目录内。
            target = work / "elsewhere" / ".." / "source" / "snap"
            holder["arg"] = str(target)
            holder["resolved"] = source / "snap"
            self.assertFalse(os.path.lexists(holder["resolved"]))
            return str(target)

        self._reject_case(
            "含 .. 分量解析后落入源目录", REASON_INSIDE_SOURCE, prepare,
            check_target=lambda ctx: self._assert_target_absent(
                holder["resolved"], ctx
            ),
        )

    # ---- 正常对照：前缀相似的同级名称不算位于源目录内 ----

    def test_sibling_with_similar_prefix_backups_successfully(self):
        """目标为同级 source-copy 目录时备份成功，前缀相似不被误判。"""
        with tempfile.TemporaryDirectory(prefix="backup-target-test-") as work:
            work = Path(work)
            source = work / "source"
            write_files(source, SOURCE_FILES)
            snapshot = work / "source-copy"
            self.assertFalse(os.path.lexists(snapshot))

            source_before = snapshot_tree(source)

            proc = run_cmd(["backup", str(source), str(snapshot)])
            stdout = proc.stdout.decode("utf-8")
            stderr = proc.stderr.decode("utf-8")

            self.assertEqual(
                proc.returncode, 0,
                f"正常对照应返回 0\nstdout={stdout!r}\nstderr={stderr!r}",
            )
            self.assertEqual(stderr, "", f"成功时标准错误应为空\n{stderr!r}")
            self.assertIn(
                str(snapshot.resolve()), stdout,
                f"标准输出应包含目标绝对路径\nstdout={stdout!r}",
            )
            self.assertIn(
                "已备份文件数: 2", stdout,
                f"标准输出应包含文件数 2\nstdout={stdout!r}",
            )

            # data 中的相对路径与原始字节和源文件一致。
            for rel, data in SOURCE_FILES.items():
                backed = (snapshot / "data" / rel).read_bytes()
                self.assertEqual(backed, data, f"备份字节与源文件不一致: {rel}")
            bin_bytes = (snapshot / "data" / "嵌套 目录" / "二进制 文件.bin").read_bytes()
            self.assertIn(0, bin_bytes, "二进制文件应保留零字节")

            # manifest.json 保持版本 1 且只列出这两个路径。
            manifest = json.loads(
                (snapshot / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest.get("version"), 1, "清单版本应为 1")
            paths = [entry.get("path") for entry in manifest.get("files", [])]
            self.assertEqual(
                paths, sorted(SOURCE_FILES),
                "清单应恰好按序包含这两个相对路径",
            )

            # 源目录内容不变。
            self.assertEqual(
                snapshot_tree(source), source_before,
                "备份成功后源目录内容发生变化",
            )


if __name__ == "__main__":
    unittest.main()
