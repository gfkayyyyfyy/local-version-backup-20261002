#!/usr/bin/env python3
"""backup 按 --exclude-dir 排除整个目录的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum] [--exclude-dir PATH]...
    python backup.py restore SNAPSHOT DEST

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中
运行时准备，用例结束自动清理，不读写用户现有目录，也不依赖网络或
额外安装包。

夹具源目录（与验收 demo 一致）：

- ``note.txt``：内容为 ``old`` 加换行；
- ``cache/sub/tmp.bin``：嵌套目录中的两个字节 00 FF。

覆盖约定：

1. 验收场景 ``--checksum --exclude-dir cache``：退出码 0、标准错误为空，
   标准输出报告“已备份文件数: 1”；快照 data 只含 note.txt 的原始字节，
   manifest.json 为版本 1、恰好一个条目且带正确 sha256、不含任何排除
   规则字段；随后 restore 退出码 0、文件数 1，dest/note.txt 字节不变，
   快照与恢复目录均无 cache；源目录保持不变。
2. 不带 --exclude-dir 的对照：仍备份全部文件，行为与旧版一致。
3. 逐字匹配：排除 cache 不影响 cache-old/ 目录或 cache.txt 文件；
   含中文与空格的目录名按原样排除。
4. 重复指定同一目录、父子目录重叠指定时取并集，计数不重复。
5. 存在的空目录是合法匹配；全部文件被排除时仍成功：清单 files 为空
   数组、data 为空目录、文件数 0，快照可恢复为空目录。
6. 与 --exclude 同用：单文件排除按完整源目录判断，即使该文件同时位于
   被排除目录内也不报错。
7. 非法形态：空字符串、绝对路径、带盘符路径、含反斜杠、含空/``.``/``..``
   分量，一律退出码 2，标准错误包含“排除目录路径无效”与原始参数。
8. 形态合法但不存在或指向普通文件：退出码 2，标准错误包含
   “排除目录未匹配普通目录”与原始参数。
9. 排除目录不绕过源目录安全检查：被排除目录内部含符号链接时仍按既有
   符号链接规则拒绝；快照已存在或位于源目录内仍按既有规则拒绝。
10. 所有失败用例：标准输出为空、不创建快照、源目录保持原样。
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

# 夹具中的相对路径（斜杠分隔，与验收 demo 一致）。
NOTE_REL = "note.txt"
CACHE_DIR_REL = "cache"
CACHE_SUB_REL = "cache/sub"
TMP_REL = "cache/sub/tmp.bin"

NOTE_BYTES = b"old\n"
TMP_BYTES = bytes([0x00, 0xFF])

DEMO_FILES = {
    NOTE_REL: NOTE_BYTES,
    TMP_REL: TMP_BYTES,
}

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
BACKUP_SUMMARY_MARKERS = ("已创建快照目录", "已备份文件数")
ERROR_PREFIX = "错误"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_INVALID = "排除目录路径无效"
REASON_UNMATCHED = "排除目录未匹配普通目录"
REASON_SYMLINK = "源目录中包含符号链接"
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


class BackupExcludeDirTests(unittest.TestCase):
    """--exclude-dir 排除目录的成功路径、确定性失败与现场不变性。"""

    def setUp(self):
        # 每个用例独立临时工作区，样例之间互不污染。
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-exclude-dir-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snap"

        write_files(self.source, DEMO_FILES)

    # ---- 通用执行与断言 ----

    def run_backup(self, exclude_dirs=None, excludes=None, checksum=False,
                   snapshot=None):
        """以公开入口执行 backup；exclude_dirs 为 None 时不带 --exclude-dir。"""
        argv = ["backup", str(self.source), str(snapshot or self.snapshot)]
        if checksum:
            argv.append("--checksum")
        for raw in excludes or []:
            argv.extend(["--exclude", raw])
        for raw in exclude_dirs or []:
            argv.extend(["--exclude-dir", raw])
        return run_cmd(argv)

    def assert_source_unchanged(self, source_before, context):
        """备份（无论成败）不得改动源目录的路径集合、类型与字节。"""
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"备份后源目录目录树发生变化\n{context}",
        )

    def run_and_assert_rejected(self, exclude_dirs, reasons, label, *,
                                literal=True, excludes=None):
        """失败用例公共流程：退出码 2、报错原因、标准输出为空、快照不存在，
        且源目录相对调用前保持不变。reasons 为须全部出现的原因片段。
        """
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"用例前提：快照路径必须事先不存在（{label}）",
        )
        source_before = capture_tree(self.source)

        proc = self.run_backup(exclude_dirs, excludes=excludes)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n排除目录: {exclude_dirs!r}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少拒绝原因“{reason}”\n{context}",
            )
        if literal:
            # 拒绝时标准错误应逐字复述原始排除目录参数（空串无可复述内容）。
            for raw in exclude_dirs:
                if raw != "":
                    self.assertIn(
                        raw, stderr,
                        f"标准错误应逐字复述原始排除目录参数: {raw!r}\n{context}",
                    )
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        for marker in BACKUP_SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"失败后快照路径仍被创建: {self.snapshot}\n{context}",
        )

        self.assert_source_unchanged(source_before, context)
        return stdout, stderr, context

    def read_manifest(self, snapshot=None):
        """读取快照 manifest.json 的 JSON 文档（测试侧独立解析）。"""
        manifest_path = (snapshot or self.snapshot) / "manifest.json"
        with open(manifest_path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))

    # ---- 成功：验收场景，--checksum --exclude-dir cache ----

    def test_acceptance_exclude_dir_with_checksum_then_restore(self):
        """排除 cache：快照只含 note.txt，恢复也只得 note.txt。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup([CACHE_DIR_REL], checksum=True)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 验收场景排除 cache 目录\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"标准输出应包含快照目录绝对路径\n{context}",
        )
        self.assertIn(
            "已备份文件数: 1", stdout,
            f"标准输出应只计算收录文件数 1\n{context}",
        )

        # data 目录恰好只含 note.txt 的原始字节，cache 完全不出现。
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {NOTE_REL: ("file", NOTE_BYTES)},
            f"快照 data 应只含 note.txt 的原始字节\n{context}",
        )

        # 清单：版本 1、恰好一个条目、摘要正确、无排除规则字段。
        doc = self.read_manifest()
        self.assertEqual(
            set(doc.keys()), {"version", "files"},
            f"清单顶层不应新增排除规则字段\n{context}",
        )
        self.assertEqual(doc["version"], 1, f"清单版本应为 1\n{context}")
        self.assertEqual(
            [item["path"] for item in doc["files"]], [NOTE_REL],
            f"清单应恰好含 note.txt 一个条目\n{context}",
        )
        self.assertEqual(
            set(doc["files"][0].keys()), {"path", "sha256"},
            f"清单条目只应含 path 与 sha256\n{context}",
        )
        self.assertEqual(
            doc["files"][0]["sha256"], sha256_hex(NOTE_BYTES),
            f"note.txt 摘要应与其原始字节一致\n{context}",
        )

        # 随后 restore：退出码 0、文件数 1、note.txt 字节不变、无 cache。
        dest = self.work / "dest"
        proc = run_cmd(["restore", str(self.snapshot), str(dest)])
        r_stdout = proc.stdout.decode("utf-8", errors="replace")
        r_stderr = proc.stderr.decode("utf-8", errors="replace")
        context += (
            f"\n恢复阶段:\nexit={proc.returncode}\n"
            f"stdout={r_stdout!r}\nstderr={r_stderr!r}"
        )
        self.assertEqual(proc.returncode, 0, f"恢复退出码应为 0\n{context}")
        self.assertIn("已恢复文件数: 1", r_stdout,
                      f"恢复文件数应为 1\n{context}")
        self.assertEqual(
            capture_tree(dest),
            {NOTE_REL: ("file", NOTE_BYTES)},
            f"恢复结果应只含 note.txt 的原始字节\n{context}",
        )
        self.assertFalse(
            (dest / CACHE_DIR_REL).exists(),
            f"被排除的 cache 目录不应在恢复结果中出现\n{context}",
        )

        # 源目录全程保持原样。
        self.assert_source_unchanged(source_before, context)

    # ---- 成功对照：不带 --exclude-dir 时行为不变 ----

    def test_without_exclude_dir_backs_up_all_files(self):
        """不带 --exclude-dir：仍备份全部两个文件，清单不含排除规则字段。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup(None, checksum=True)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 不带 --exclude-dir 对照\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 2", stdout,
                      f"不带 --exclude-dir 应备份全部两个文件\n{context}")
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {
                CACHE_DIR_REL: ("dir", None),
                CACHE_SUB_REL: ("dir", None),
                NOTE_REL: ("file", NOTE_BYTES),
                TMP_REL: ("file", TMP_BYTES),
            },
            f"不带 --exclude-dir 时 data 应含全部两个文件\n{context}",
        )
        doc = self.read_manifest()
        self.assertEqual(set(doc.keys()), {"version", "files"},
                         f"清单顶层不应新增字段\n{context}")
        self.assertEqual(
            [item["path"] for item in doc["files"]],
            sorted([NOTE_REL, TMP_REL]),
            f"清单应含全部两个文件且保持既有排序\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 成功：逐字匹配，cache 不影响 cache-old 或 cache.txt ----

    def test_exclude_dir_does_not_match_sibling_prefixes(self):
        """排除 cache 不影响 cache-old/ 目录与 cache.txt 文件。"""
        extra_files = {
            "cache-old/keep.bin": b"keep-old\n",
            "cache.txt": b"keep-file\n",
        }
        write_files(self.source, extra_files)
        source_before = capture_tree(self.source)

        proc = self.run_backup([CACHE_DIR_REL])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 逐字匹配不影响近似名\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 3", stdout,
                      f"应收录 note.txt、cache-old/keep.bin 与 cache.txt\n{context}")
        data_tree = capture_tree(self.snapshot / "data")
        self.assertNotIn(TMP_REL, data_tree,
                         f"被排除目录内的文件不应出现在 data 中\n{context}")
        self.assertEqual(data_tree["cache-old/keep.bin"],
                         ("file", extra_files["cache-old/keep.bin"]),
                         f"cache-old 目录内容应完整保留\n{context}")
        self.assertEqual(data_tree["cache.txt"],
                         ("file", extra_files["cache.txt"]),
                         f"cache.txt 文件应完整保留\n{context}")
        doc = self.read_manifest()
        self.assertEqual(
            [item["path"] for item in doc["files"]],
            sorted([NOTE_REL, "cache-old/keep.bin", "cache.txt"]),
            f"清单应恰好含三个保留文件\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 成功：中文与空格目录名逐字排除 ----

    def test_exclude_dir_chinese_and_space_verbatim(self):
        """含中文与空格的目录名按原样逐字排除。"""
        extra_rel = "缓存 目录/子 目录/数据.bin"
        extra_bytes = bytes([0x10, 0x00, 0xFF])
        write_files(self.source, {extra_rel: extra_bytes})
        source_before = capture_tree(self.source)

        proc = self.run_backup(["缓存 目录"])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 中文与空格目录名逐字排除\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 2", stdout,
                      f"排除一个目录后应收录两个文件\n{context}")
        data_tree = capture_tree(self.snapshot / "data")
        self.assertNotIn(extra_rel, data_tree,
                         f"被排除的中文目录内容不应出现在 data 中\n{context}")
        self.assertNotIn("缓存 目录", data_tree,
                         f"被排除的中文目录不应出现在 data 中\n{context}")
        doc = self.read_manifest()
        self.assertEqual(
            [item["path"] for item in doc["files"]],
            sorted([NOTE_REL, TMP_REL]),
            f"清单应恰好含两个保留文件\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 成功：重复指定与父子目录重叠取并集 ----

    def test_duplicate_exclude_dir_excluded_once(self):
        """重复指定同一目录：只排除一次，文件数为 1。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup([CACHE_DIR_REL, CACHE_DIR_REL])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 重复排除同一目录\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 1", stdout,
                      f"重复排除不应重复计数\n{context}")
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {NOTE_REL: ("file", NOTE_BYTES)},
            f"重复排除应只排除一次 cache\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    def test_nested_exclude_dirs_take_union(self):
        """父子目录重叠指定（cache 与 cache/sub）：取并集，文件数为 1。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup([CACHE_DIR_REL, CACHE_SUB_REL])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 父子目录重叠排除\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 1", stdout,
                      f"父子目录重叠应取并集\n{context}")
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {NOTE_REL: ("file", NOTE_BYTES)},
            f"重叠排除后 data 应只含 note.txt\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    def test_exclude_subdir_only_keeps_sibling_files(self):
        """只排除子目录 cache/sub：cache 下其他文件仍保留。"""
        write_files(self.source, {"cache/top.txt": b"top\n"})
        source_before = capture_tree(self.source)

        proc = self.run_backup([CACHE_SUB_REL])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 只排除子目录\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 2", stdout,
                      f"只排除 cache/sub 后应收录两个文件\n{context}")
        data_tree = capture_tree(self.snapshot / "data")
        self.assertNotIn(TMP_REL, data_tree,
                         f"被排除子目录内的文件不应出现在 data 中\n{context}")
        self.assertEqual(data_tree["cache/top.txt"], ("file", b"top\n"),
                         f"cache 下其他文件应保留\n{context}")
        self.assert_source_unchanged(source_before, context)

    # ---- 成功：存在的空目录是合法匹配 ----

    def test_exclude_empty_directory_is_valid(self):
        """存在的空目录是合法匹配：排除成功，其余文件照常备份。"""
        (self.source / "empty-dir").mkdir()
        source_before = capture_tree(self.source)

        proc = self.run_backup(["empty-dir"])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 排除空目录\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 2", stdout,
                      f"排除空目录不影响文件计数\n{context}")
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {
                CACHE_DIR_REL: ("dir", None),
                CACHE_SUB_REL: ("dir", None),
                NOTE_REL: ("file", NOTE_BYTES),
                TMP_REL: ("file", TMP_BYTES),
            },
            f"排除空目录后其余文件应照常备份\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 成功：全部文件被排除 ----

    def test_exclude_all_files_succeeds_with_empty_snapshot(self):
        """排除目录覆盖全部文件：空 files 数组、空 data 目录、文件数 0。"""
        write_files(self.source, {"other/keep.txt": b"keep\n"})
        source_before = capture_tree(self.source)

        # note.txt 用 --exclude 单文件排除，两个目录用 --exclude-dir 排除，
        # 组合后全部文件均被排除。
        proc = self.run_backup(
            [CACHE_DIR_REL, "other"], excludes=[NOTE_REL], checksum=True,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 全部文件被排除\n"
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

        # 空快照仍可恢复为空目录。
        restored = self.work / "restored-empty"
        proc = run_cmd(["restore", str(self.snapshot), str(restored)])
        r_stderr = proc.stderr.decode("utf-8", errors="replace")
        context += f"\n恢复阶段:\nexit={proc.returncode}\nstderr={r_stderr!r}"
        self.assertEqual(proc.returncode, 0, f"空快照恢复退出码应为 0\n{context}")
        self.assertTrue(restored.is_dir(), f"恢复目标应为目录\n{context}")
        self.assertEqual(capture_tree(restored), {},
                         f"空快照应恢复为空目录\n{context}")

        self.assert_source_unchanged(source_before, context)

    # ---- 成功：与 --exclude 同用，单文件排除按完整源目录判断 ----

    def test_exclude_file_inside_excluded_dir_still_matches(self):
        """--exclude 指向被排除目录内的文件：仍按完整源目录匹配，不报错。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup(
            [CACHE_DIR_REL], excludes=[TMP_REL], checksum=True,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 单文件排除命中被排除目录内的文件\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 1", stdout,
                      f"应只收录 note.txt\n{context}")
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {NOTE_REL: ("file", NOTE_BYTES)},
            f"data 应只含 note.txt\n{context}",
        )
        doc = self.read_manifest()
        self.assertEqual(
            [item["path"] for item in doc["files"]], [NOTE_REL],
            f"清单应恰好含 note.txt\n{context}",
        )
        self.assertEqual(
            doc["files"][0]["sha256"], sha256_hex(NOTE_BYTES),
            f"note.txt 摘要应与其原始字节一致\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 失败：非法排除目录路径形态 ----

    def test_reject_empty_string_exclude_dir(self):
        """空字符串排除目录参数：非法，退出码 2。"""
        self.run_and_assert_rejected(
            [""], [REASON_INVALID], "空字符串排除目录参数",
        )

    def test_reject_absolute_exclude_dir(self):
        """绝对路径排除目录参数：非法，不与源内相对路径混淆。"""
        self.run_and_assert_rejected(
            ["/cache"], [REASON_INVALID], "绝对路径排除目录参数",
        )

    def test_reject_drive_letter_exclude_dir(self):
        """带盘符的排除目录参数（C:/cache）：非法。"""
        self.run_and_assert_rejected(
            ["C:/cache"], [REASON_INVALID], "带盘符路径",
        )

    def test_reject_backslash_exclude_dir(self):
        """含反斜杠的排除目录参数：非法，不转换分隔符。"""
        self.run_and_assert_rejected(
            ["cache\\sub"], [REASON_INVALID], "含反斜杠",
        )

    def test_reject_empty_component_exclude_dir(self):
        """cache//sub：含空分量的排除目录参数非法。"""
        self.run_and_assert_rejected(
            ["cache//sub"], [REASON_INVALID], "含空分量",
        )

    def test_reject_trailing_slash_exclude_dir(self):
        """cache/：末尾空分量同样非法，不归一化为 cache。"""
        self.run_and_assert_rejected(
            ["cache/"], [REASON_INVALID], "末尾空分量",
        )

    def test_reject_dot_component_exclude_dir(self):
        """./cache：含当前目录分量，不归一化为 cache。"""
        self.run_and_assert_rejected(
            ["./cache"], [REASON_INVALID], "含点分量",
        )

    def test_reject_parent_component_exclude_dir(self):
        """../cache：含上级目录分量的排除目录参数非法。"""
        self.run_and_assert_rejected(
            ["../cache"], [REASON_INVALID], "含上级目录分量",
        )

    # ---- 失败：形态合法但不匹配真实目录 ----

    def test_reject_exclude_dir_nonexistent(self):
        """排除目录不存在：未匹配普通目录。"""
        self.run_and_assert_rejected(
            ["no-such-dir"], [REASON_UNMATCHED], "排除目录不存在",
        )

    def test_reject_exclude_dir_pointing_to_file(self):
        """排除目录指向普通文件 note.txt：未匹配普通目录。"""
        self.run_and_assert_rejected(
            [NOTE_REL], [REASON_UNMATCHED], "排除目录指向普通文件",
        )

    def test_reject_exclude_dir_case_different(self):
        """仅大小写不同的 Cache：不匹配 cache，不忽略大小写。"""
        self.run_and_assert_rejected(
            ["Cache"], [REASON_UNMATCHED], "仅大小写不同",
        )

    def test_reject_exclude_dir_glob_literal(self):
        """* 作为字面值：不做通配符展开，不匹配任何目录。"""
        self.run_and_assert_rejected(
            ["*"], [REASON_UNMATCHED], "通配符字面量",
        )

    # ---- 失败：排除目录不绕过源目录安全检查 ----

    @unittest.skipUnless(hasattr(os, "symlink"), "平台不支持符号链接")
    def test_exclude_dir_does_not_bypass_symlink_rejection(self):
        """被排除目录内部含符号链接时，仍触发现有符号链接拒绝。"""
        link_rel = "cache/sub/link-to-tmp"
        os.symlink("tmp.bin", str(self.source / link_rel))

        self.run_and_assert_rejected(
            [CACHE_DIR_REL], [REASON_SYMLINK, link_rel],
            "被排除目录内的符号链接不被绕过",
            # 报错来自源目录安全检查，不复述排除目录参数，故关闭逐字复述核对。
            literal=False,
        )

    # ---- 失败：既有快照路径检查不被排除目录参数绕过 ----

    def test_reject_existing_snapshot_with_exclude_dir(self):
        """快照路径已存在：即使排除目录合法也仍按既有规则拒绝。"""
        self.snapshot.mkdir()
        source_before = capture_tree(self.source)

        proc = self.run_backup([CACHE_DIR_REL])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 快照路径已存在\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(REASON_SNAPSHOT_EXISTS, stderr,
                      f"标准错误应指出快照已存在\n{context}")
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        self.assertEqual(capture_tree(self.snapshot), {},
                         f"已存在的快照目录不应被改动\n{context}")
        self.assert_source_unchanged(source_before, context)

    def test_reject_snapshot_inside_source_with_exclude_dir(self):
        """快照位于源目录内：即使排除目录合法也仍按既有规则拒绝。"""
        source_before = capture_tree(self.source)
        inside_snapshot = self.source / "snap-inside"

        proc = self.run_backup([CACHE_DIR_REL], snapshot=inside_snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 快照位于源目录内\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertIn(REASON_SNAPSHOT_INSIDE, stderr,
                      f"标准错误应指出快照不得位于源目录内\n{context}")
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        self.assertFalse(
            os.path.lexists(inside_snapshot),
            f"失败后源目录内不应出现快照目录\n{context}",
        )
        self.assert_source_unchanged(source_before, context)


if __name__ == "__main__":
    unittest.main()
