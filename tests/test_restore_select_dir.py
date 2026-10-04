#!/usr/bin/env python3
"""restore 按 --dir 选择目录恢复的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH]... [--dir PATH]...
    python backup.py restore SNAPSHOT DEST [--file PATH]... [--dir PATH]... --dry-run

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中
运行时准备，用例结束自动清理。

夹具快照由公开 backup 命令从以下普通文件生成：

- ``note.txt``：根目录文本，用于与 --dir 合用取并集；
- ``docs/a.txt``、``docs/sub/b.bin``：被选择目录 docs 的后代；
- ``docs-old/a.txt``：前缀近似但非子目录，选择 docs 时不得收录；
- ``docs.txt``：前缀近似文件，选择 docs 时不得收录；
- ``other.txt``：未选择对照文件。

源目录中另外准备一个不含任何普通文件的空目录 ``empty-dir``（备份不
记录空目录），用于验证“未记录的空目录”同样按无后代拒绝。

覆盖约定：

1. 选择 docs 恢复 docs/a.txt 与 docs/sub/b.bin 两个文件并保留完整相对
   路径，不含 docs-old/a.txt、docs.txt 等前缀近似项；字节与快照一致。
2. --dir 与 --file 合用取并集；重复目录、父子目录重叠（docs 与
   docs/sub）、重复文件只恢复一次。
3. 两类选择都不提供时恢复全部文件（原有行为）。
4. --dry-run 输出现有一行 JSON（字段不增），files/paths 为去重结果且
   按 Unicode 码点升序，不创建目标；随后去掉 --dry-run 实际恢复，
   两次都选择相同的三个文件，字节与快照一致。
5. 非法目录形态（空串、绝对路径、盘符、反斜杠、空/./.. 分量）报
   “目录选择路径无效”并复述原始参数；形态合法但无清单后代（不存在的
   目录、未记录的空目录、指向清单文件、仅大小写不同、docs-old 式近似
   混淆的反向情形由成功用例覆盖）报“选择的目录未包含快照清单文件”，
   均退出码 2、标准输出为空、不创建目标。
6. 目录选择只依据清单路径：源目录删除后仍可恢复；data/ 中未列入清单
   的文件不纳入结果。
7. 任一选择不匹配（含合用的不存在 --file）整体失败；未选中文件数据
   缺失或摘要不一致仍整体失败，不创建目标。

每次恢复前后都比较源目录（仍存在时）与快照的完整目录树，确认恢复不
改动它们；损坏样例以调用恢复前的状态为比较基准。
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

CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

NOTE_REL = "note.txt"
DOCS_A_REL = "docs/a.txt"
DOCS_B_REL = "docs/sub/b.bin"
DOCS_OLD_REL = "docs-old/a.txt"
DOCS_TXT_REL = "docs.txt"
OTHER_REL = "other.txt"
UNICODE_REL = "资料 目录/f.txt"

BACKUP_FILES = {
    NOTE_REL: b"NOTE-BYTES\n",
    DOCS_A_REL: b"docs a text\n",
    DOCS_B_REL: bytes([0x00, 0xFF, 0x80, 0x00]),
    DOCS_OLD_REL: b"old sibling\n",
    DOCS_TXT_REL: b"similar file name\n",
    OTHER_REL: b"other unselected\n",
    UNICODE_REL: b"unicode content\n",
}

SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"
REASON_DIR_INVALID = "目录选择路径无效"
REASON_DIR_EMPTY = "选择的目录未包含快照清单文件"
REASON_FILE_NOT_IN_MANIFEST = "选择的路径未在快照清单中"
REASON_DATA_MISSING = "清单引用的数据缺失"
REASON_CHECKSUM_MISMATCH = "摘要校验不一致"


def run_cmd(argv):
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), *argv],
        cwd=str(ROOT),
        env=CHILD_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_files(base, files):
    base = Path(base)
    for rel, data in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def capture_tree(root):
    root = Path(root)
    tree = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_dir = Path(dirpath).relative_to(root)
        for name in dirnames:
            path = Path(dirpath) / name
            key = (rel_dir / name).as_posix()
            tree[key] = (
                "symlink" if path.is_symlink() else "dir",
                os.readlink(path) if path.is_symlink() else None,
            )
        for name in filenames:
            path = Path(dirpath) / name
            key = (rel_dir / name).as_posix()
            tree[key] = ("file", path.read_bytes())
    return tree


class RestoreDirSelectionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-dir-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

        write_files(self.source, BACKUP_FILES)
        # 备份不记录空目录；它只存在于源目录，用于验证选择不依赖源目录
        # 与“未记录的空目录”拒绝路径。
        (self.source / "empty-dir").mkdir(parents=True, exist_ok=True)

        proc = run_cmd(
            ["backup", "--checksum", str(self.source), str(self.snapshot)]
        )
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

    def run_restore(self, dest, files=None, dirs=None, dry_run=False):
        argv = ["restore", str(self.snapshot), str(dest)]
        for sel in files or []:
            argv.extend(["--file", sel])
        for sel in dirs or []:
            argv.extend(["--dir", sel])
        if dry_run:
            argv.append("--dry-run")
        return run_cmd(argv)

    def assert_rejected(self, proc, dest, reasons, raw_args=None, *,
                        label=""):
        self.assertEqual(
            proc.returncode, 2,
            f"退出码应为 2（{label}）\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}",
        )
        self.assertEqual(proc.stdout, b"", f"失败时标准输出应为空（{label}）")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        self.assertIn(ERROR_PREFIX, stderr, f"缺少错误前缀（{label}）")
        for reason in reasons:
            self.assertIn(reason, stderr, f"缺少原因“{reason}”（{label}）\n{stderr}")
        for raw in raw_args or []:
            if raw != "":
                self.assertIn(raw, stderr, f"应复述原始参数 {raw!r}（{label}）\n{stderr}")
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, proc.stdout.decode("utf-8", "replace"))
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后不得创建目标 {dest}（{label}）",
        )

    # ---- 成功：目录后代逐字匹配，保留完整路径 ----

    def test_dir_selects_descendants_only(self):
        dest = self.work / "restored-docs"
        proc = self.run_restore(dest, dirs=["docs"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")
        stdout = proc.stdout.decode("utf-8")
        self.assertIn("已恢复文件数: 2", stdout)

        tree = capture_tree(dest)
        self.assertEqual(
            sorted(k for k, v in tree.items() if v[0] == "file"),
            [DOCS_A_REL, DOCS_B_REL],
        )
        self.assertEqual(tree[DOCS_A_REL], ("file", BACKUP_FILES[DOCS_A_REL]))
        self.assertEqual(tree[DOCS_B_REL], ("file", BACKUP_FILES[DOCS_B_REL]))
        # 前缀近似项与未选择文件都不得出现。
        for absent in (DOCS_OLD_REL, DOCS_TXT_REL, NOTE_REL, OTHER_REL):
            self.assertFalse((dest / absent).exists(), f"不应恢复 {absent}")

    def test_subdir_selects_only_its_descendants(self):
        dest = self.work / "restored-sub"
        proc = self.run_restore(dest, dirs=["docs/sub"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tree = capture_tree(dest)
        self.assertEqual(
            sorted(k for k, v in tree.items() if v[0] == "file"),
            [DOCS_B_REL],
        )

    def test_unicode_and_space_dir(self):
        dest = self.work / "restored-unicode"
        proc = self.run_restore(dest, dirs=["资料 目录"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            (dest / UNICODE_REL).read_bytes(), BACKUP_FILES[UNICODE_REL]
        )

    def test_case_difference_is_literal_mismatch(self):
        """仅大小写不同的 Docs 不收录 docs 下任何文件（按无后代拒绝）。"""
        dest = self.work / "restored-case"
        proc = self.run_restore(dest, dirs=["Docs"])
        self.assert_rejected(
            proc, dest, [REASON_DIR_EMPTY, "Docs"], label="大小写不同"
        )

    # ---- 成功：与 --file 并集、重复与父子重叠去重 ----

    def test_dir_union_file_dedup_and_overlap(self):
        dest = self.work / "restored-union"
        proc = self.run_restore(
            dest,
            files=[NOTE_REL, DOCS_A_REL, NOTE_REL],
            dirs=["docs", "docs", "docs/sub"],
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stdout = proc.stdout.decode("utf-8")
        self.assertIn("已恢复文件数: 3", stdout)
        tree = capture_tree(dest)
        self.assertEqual(
            sorted(k for k, v in tree.items() if v[0] == "file"),
            sorted([NOTE_REL, DOCS_A_REL, DOCS_B_REL]),
        )
        self.assertEqual((dest / NOTE_REL).read_bytes(), BACKUP_FILES[NOTE_REL])

    def test_no_selection_restores_all(self):
        dest = self.work / "restored-all"
        proc = self.run_restore(dest)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("已恢复文件数: 7", proc.stdout.decode("utf-8"))

    # ---- 预览：字段不变、去重、码点升序，随后实际恢复一致 ----

    def test_dry_run_then_real_restore(self):
        dest = self.work / "restored-preview"
        proc = self.run_restore(
            dest, files=[NOTE_REL], dirs=["docs"], dry_run=True
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stderr, b"")
        self.assertTrue(proc.stdout.endswith(b"\n"))
        line = proc.stdout[:-1]
        self.assertNotIn(b"\n", line)
        doc = json.loads(line.decode("utf-8"))
        self.assertEqual(
            set(doc.keys()), {"snapshot", "destination", "files", "paths"}
        )
        expected_paths = sorted([NOTE_REL, DOCS_A_REL, DOCS_B_REL])
        self.assertEqual(doc["files"], 3)
        self.assertEqual(doc["paths"], expected_paths)
        self.assertFalse(os.path.lexists(dest))
        self.assertNotIn("restored-preview",
                         sorted(p.name for p in self.work.iterdir()))

        # 去掉 --dry-run：同样选择三个文件，字节与快照一致。
        proc = self.run_restore(dest, files=[NOTE_REL], dirs=["docs"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("已恢复文件数: 3", proc.stdout.decode("utf-8"))
        tree = capture_tree(dest)
        self.assertEqual(
            sorted(k for k, v in tree.items() if v[0] == "file"),
            expected_paths,
        )
        for rel in expected_paths:
            self.assertEqual(
                (dest / rel).read_bytes(),
                (self.snapshot / "data" / rel).read_bytes(),
                f"{rel} 字节应与快照一致",
            )

    # ---- 失败：非法目录形态 ----

    def test_invalid_dir_forms(self):
        cases = [
            ("空字符串", ""),
            ("绝对路径", "/docs"),
            ("盘符加斜杠", "C:/docs"),
            ("盘符", "C:docs"),
            ("反斜杠", "docs\\sub"),
            ("空分量", "docs//sub"),
            ("点分量", "./docs"),
            ("点分量在内", "docs/./sub"),
            ("父级分量", "../docs"),
            ("父级分量在内", "docs/../sub"),
            ("结尾斜杠", "docs/"),
        ]
        for label, raw in cases:
            with self.subTest(label=label, raw=raw):
                dest = self.work / ("bad-" + label.replace("/", "_"))
                proc = self.run_restore(dest, dirs=[raw])
                self.assert_rejected(
                    proc, dest, [REASON_DIR_INVALID], [raw], label=label
                )

    # ---- 失败：形态合法但无清单后代 ----

    def test_legal_dir_without_manifest_descendants(self):
        cases = [
            ("不存在的目录", "no-such-dir"),
            ("未记录的空目录", "empty-dir"),
            ("指向清单文件", DOCS_A_REL),
            ("同样无后代的其他目录", "docs-extra"),
        ]
        for label, raw in cases:
            with self.subTest(label=label, raw=raw):
                dest = self.work / ("empty-" + label)
                proc = self.run_restore(dest, dirs=[raw])
                self.assert_rejected(
                    proc, dest, [REASON_DIR_EMPTY], [raw], label=label
                )

    def test_docs_old_dir_selects_only_its_own_descendants(self):
        # 以 docs-old 作为目录选择本身合法且只收录其自身后代；
        # docs/ 与 docs.txt 都不受影响。
        dest = self.work / "restored-old"
        proc = self.run_restore(dest, dirs=["docs-old"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("已恢复文件数: 1", proc.stdout.decode("utf-8"))
        self.assertTrue((dest / DOCS_OLD_REL).is_file())
        self.assertFalse((dest / DOCS_A_REL).exists())

    # ---- 失败：合用的 --file 不存在仍整体失败 ----

    def test_bad_file_fails_even_with_good_dir(self):
        dest = self.work / "restored-bad-file"
        proc = self.run_restore(
            dest, files=["nope.txt"], dirs=["docs"]
        )
        self.assert_rejected(
            proc, dest,
            [REASON_FILE_NOT_IN_MANIFEST, "nope.txt"],
            label="合用不存在的文件",
        )

    # ---- 整单校验：未选中文件缺失/摘要不一致仍失败 ----

    def test_unselected_data_missing_fails(self):
        missing = self.snapshot / "data" / OTHER_REL
        missing.unlink()
        snapshot_before = capture_tree(self.snapshot)
        dest = self.work / "restored-missing"
        proc = self.run_restore(dest, dirs=["docs"], dry_run=True)
        self.assert_rejected(
            proc, dest, [REASON_DATA_MISSING, OTHER_REL],
            label="未选中数据缺失（预览）",
        )
        proc = self.run_restore(dest, dirs=["docs"])
        self.assert_rejected(
            proc, dest, [REASON_DATA_MISSING, OTHER_REL],
            label="未选中数据缺失",
        )
        self.assertEqual(capture_tree(self.snapshot), snapshot_before)

    def test_unselected_checksum_mismatch_fails(self):
        (self.snapshot / "data" / NOTE_REL).write_bytes(b"corrupt bytes\n")
        dest = self.work / "restored-corrupt"
        proc = self.run_restore(dest, dirs=["docs"])
        self.assert_rejected(
            proc, dest, [REASON_CHECKSUM_MISMATCH, NOTE_REL],
            label="未选中摘要不一致",
        )

    # ---- 只依据清单路径：不依赖源目录，不纳入清单外数据 ----

    def test_source_dir_not_required(self):
        import shutil
        shutil.rmtree(self.source)
        dest = self.work / "restored-no-source"
        proc = self.run_restore(dest, dirs=["docs"])
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(
            sorted(p.relative_to(dest).as_posix()
                   for p in dest.rglob("*") if p.is_file()),
            [DOCS_A_REL, DOCS_B_REL],
        )

    def test_extra_data_file_not_selected(self):
        rogue_dir = self.snapshot / "data" / "rogue"
        rogue_dir.mkdir(parents=True)
        (rogue_dir / "x.txt").write_bytes(b"rogue\n")
        dest = self.work / "restored-rogue"
        proc = self.run_restore(dest, dirs=["rogue"])
        self.assert_rejected(
            proc, dest, [REASON_DIR_EMPTY, "rogue"],
            label="清单外目录",
        )
        proc = self.run_restore(dest, dirs=["docs"], dry_run=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        doc = json.loads(proc.stdout[:-1].decode("utf-8"))
        self.assertNotIn("rogue/x.txt", doc["paths"])

    # ---- 目标安全规则对 --dir 同样生效 ----

    def test_existing_dest_rejected_with_dir(self):
        dest = self.work / "existing"
        dest.mkdir()
        proc = self.run_restore(dest, dirs=["docs"])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("恢复目标已存在", proc.stderr.decode("utf-8"))

    def test_dest_within_snapshot_rejected_with_dir(self):
        dest = self.snapshot / "inside"
        proc = self.run_restore(dest, dirs=["docs"])
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stdout, b"")
        self.assertIn("恢复目录不得位于快照目录内",
                      proc.stderr.decode("utf-8"))
        self.assertFalse(os.path.lexists(dest))


if __name__ == "__main__":
    unittest.main()
