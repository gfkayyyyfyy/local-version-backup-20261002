#!/usr/bin/env python3
"""restore 在写入前对无效快照整体拒绝的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py restore SNAPSHOT DEST

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部快照与目录在临时目录中运行时准备，
用例结束自动清理，不读写用户现有目录。
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

# 正常快照中的两个文件：文本文件与含零字节（0x00）的二进制文件。
VALID_FILES = {
    "note.txt": "笔记内容\n第二行\n".encode("utf-8"),
    "嵌套 目录/二进制 文件.bin": bytes(range(256)),
}

# 拒绝用例清单中放在问题条目之前的有效文件，用于证明后续条目失败时
# 先前文件也不会被写入恢复目标。
GOOD_FILE = "good.txt"
GOOD_BYTES = b"previously valid file\n"

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


def build_snapshot(work, files):
    """用公开 backup 命令从运行时准备的源目录生成版本 1 快照。"""
    source = Path(work) / "source"
    snapshot = Path(work) / "snapshot"
    write_files(source, files)
    proc = run_cmd(["backup", str(source), str(snapshot)])
    if proc.returncode != 0 or not snapshot.is_dir():
        raise RuntimeError(
            "测试夹具：基线快照创建失败\n"
            f"exit={proc.returncode}\nstdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )
    return snapshot


def write_manifest(snapshot, doc):
    (Path(snapshot) / "manifest.json").write_text(
        json.dumps(doc, ensure_ascii=False), encoding="utf-8"
    )


def write_raw_manifest(snapshot, raw):
    (Path(snapshot) / "manifest.json").write_bytes(raw)


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


def file_tree(root):
    """递归记录普通文件：相对路径 -> 字节。"""
    return {
        key: value[1]
        for key, value in snapshot_tree(root).items()
        if value[0] == "file"
    }


class RestoreValidationTests(unittest.TestCase):
    """无效快照必须在创建恢复目标之前被整体拒绝。"""

    def assertRejected(
        self, label, proc, dest, reason, snap_before, snapshot,
        work_before=None, work=None,
    ):
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
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )
        self.assertEqual(
            snapshot_tree(snapshot), snap_before,
            f"调用前后快照目录内容发生变化\n{context}",
        )
        if work is not None:
            # 路径越界用例：快照之外的标记文件字节不变，且没有任何越界文件生成。
            self.assertEqual(
                snapshot_tree(work), work_before,
                f"失败后临时工作区出现新增或改动（可能发生越界写入）\n{context}",
            )

    def _reject_case(self, label, reason, mutate, dest_subdir=None,
                     traversal=False):
        """运行单个拒绝用例的通用流程，每种用例只引入一种无效条件。"""
        with tempfile.TemporaryDirectory(prefix="restore-test-") as work:
            snapshot = build_snapshot(work, {GOOD_FILE: GOOD_BYTES})

            # 恢复目标父目录可预先存在，但恢复目标本身必须事先不存在。
            if dest_subdir is not None:
                area = Path(work) / dest_subdir
                area.mkdir(parents=True, exist_ok=True)
                dest = area / "restored"
            else:
                dest = Path(work) / "restored"
            self.assertFalse(os.path.lexists(dest))

            # 每种用例只在此处引入唯一的无效条件（含快照外标记文件）。
            mutate(Path(work), snapshot)

            # 基线在夹具全部就绪后、调用 restore 前拍摄。
            snap_before = snapshot_tree(snapshot)
            work_before = snapshot_tree(work) if traversal else None

            proc = run_cmd(["restore", str(snapshot), str(dest)])
            self.assertRejected(
                label, proc, dest, reason, snap_before, snapshot,
                work_before=work_before, work=work if traversal else None,
            )

    # ---- 正常对照 ----

    def test_valid_v1_snapshot_restores_successfully(self):
        """版本1快照（含零字节二进制文件与嵌套中文路径）恢复成功。"""
        with tempfile.TemporaryDirectory(prefix="restore-test-") as work:
            snapshot = build_snapshot(work, VALID_FILES)
            dest = Path(work) / "restored"

            proc = run_cmd(["restore", str(snapshot), str(dest)])
            stdout = proc.stdout.decode("utf-8")
            stderr = proc.stderr.decode("utf-8")

            self.assertEqual(
                proc.returncode, 0,
                f"正常对照应返回 0\nstdout={stdout!r}\nstderr={stderr!r}",
            )
            self.assertEqual(stderr, "", f"成功时标准错误应为空\n{stderr!r}")
            self.assertIn(
                str(dest.resolve()), stdout,
                f"标准输出应包含恢复目标绝对路径\nstdout={stdout!r}",
            )
            self.assertIn(
                "已恢复文件数: 2", stdout,
                f"标准输出应包含文件数 2\nstdout={stdout!r}",
            )

            expected = file_tree(snapshot / "data")
            restored = file_tree(dest)
            self.assertEqual(
                set(restored), set(expected),
                "恢复后的相对路径集合与快照不一致",
            )
            for rel, data in expected.items():
                self.assertEqual(
                    restored[rel], data,
                    f"文件字节与快照不一致: {rel}",
                )
            # 显式固定公开语义：嵌套路径与含零字节的二进制文件逐字节保留。
            self.assertEqual(
                (dest / "note.txt").read_bytes(), VALID_FILES["note.txt"]
            )
            bin_bytes = (dest / "嵌套 目录" / "二进制 文件.bin").read_bytes()
            self.assertEqual(bin_bytes, VALID_FILES["嵌套 目录/二进制 文件.bin"])
            self.assertIn(0, bin_bytes)

    # ---- 清单本身无效 ----

    def test_reject_corrupt_json(self):
        def mutate(work, snapshot):
            write_raw_manifest(snapshot, b"{ this is not valid json\n")

        self._reject_case(
            "损坏 JSON", "JSON", mutate,
        )

    def test_reject_version_2(self):
        def mutate(work, snapshot):
            write_manifest(
                snapshot,
                {"version": 2, "files": [{"path": GOOD_FILE}]},
            )

        self._reject_case(
            "version 为 2", "不支持的清单版本", mutate,
        )

    def test_reject_version_true(self):
        def mutate(work, snapshot):
            write_manifest(
                snapshot,
                {"version": True, "files": [{"path": GOOD_FILE}]},
            )

        self._reject_case(
            "version 为 true", "version", mutate,
        )

    def test_reject_files_not_array(self):
        def mutate(work, snapshot):
            write_manifest(snapshot, {"version": 1, "files": {}})

        self._reject_case(
            "files 不是数组", "files", mutate,
        )

    # ---- 路径非法（清单先含一个有效文件）----

    def test_reject_empty_path(self):
        def mutate(work, snapshot):
            write_manifest(
                snapshot,
                {
                    "version": 1,
                    "files": [{"path": GOOD_FILE}, {"path": ""}],
                },
            )

        self._reject_case(
            "path 为空字符串", "非空字符串", mutate,
        )

    def test_reject_duplicate_path(self):
        def mutate(work, snapshot):
            write_manifest(
                snapshot,
                {
                    "version": 1,
                    "files": [
                        {"path": GOOD_FILE},
                        {"path": GOOD_FILE},
                    ],
                },
            )

        self._reject_case(
            "重复文件路径", "重复", mutate,
        )

    def test_reject_parent_component_path(self):
        # "../../marker.bin" 从 snapshot/data 解析正好指向快照外的
        # work/marker.bin；恢复目标使用两级父目录，使越界目标指向同一标记。
        traversal_rel = "../../marker.bin"

        def mutate(work, snapshot):
            marker = work / "marker.bin"
            marker.write_bytes(b"outside snapshot, do not touch\n")
            write_manifest(
                snapshot,
                {
                    "version": 1,
                    "files": [
                        {"path": GOOD_FILE},
                        {"path": traversal_rel},
                    ],
                },
            )

        self._reject_case(
            "包含 .. 分量的路径", "上级目录", mutate,
            dest_subdir="restore_area", traversal=True,
        )

    def test_reject_absolute_path(self):
        def mutate(work, snapshot):
            marker = work / "abs_marker.bin"
            marker.write_bytes(b"absolute target, do not touch\n")
            write_manifest(
                snapshot,
                {
                    "version": 1,
                    "files": [
                        {"path": GOOD_FILE},
                        {"path": str(marker.resolve())},
                    ],
                },
            )

        self._reject_case(
            "以 / 开头的绝对路径", "绝对路径", mutate,
            dest_subdir="restore_area", traversal=True,
        )

    # ---- 清单引用的数据无效（清单先含一个有效文件）----

    def test_reject_missing_referenced_file(self):
        def mutate(work, snapshot):
            write_manifest(
                snapshot,
                {
                    "version": 1,
                    "files": [
                        {"path": GOOD_FILE},
                        {"path": "missing.txt"},
                    ],
                },
            )

        self._reject_case(
            "清单引用文件缺失", "数据缺失", mutate,
        )

    def test_reject_referenced_path_is_directory(self):
        def mutate(work, snapshot):
            (Path(snapshot) / "data" / "subdir").mkdir()
            write_manifest(
                snapshot,
                {
                    "version": 1,
                    "files": [
                        {"path": GOOD_FILE},
                        {"path": "subdir"},
                    ],
                },
            )

        self._reject_case(
            "引用路径实际为目录", "不是普通文件", mutate,
        )


if __name__ == "__main__":
    unittest.main()
