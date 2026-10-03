#!/usr/bin/env python3
"""backup 按 --exclude 精确排除文件的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum] [--exclude PATH]...
    python backup.py restore SNAPSHOT DEST

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中
运行时准备，用例结束自动清理，不读写用户现有目录，也不依赖网络或
额外安装包。

夹具源目录（与验收 demo 一致）：

- ``note.txt``：明确的 UTF-8 文本；
- ``logs/cache.log``：嵌套目录中的日志文本；
- ``empty.dat``：零字节空文件。

覆盖约定：

1. ``--checksum --exclude logs/cache.log``：退出码 0、标准错误为空，
   标准输出报告“已备份文件数: 2”；快照 data 只含另外两个文件的原始
   字节，manifest.json 为版本 1、恰好两个条目且路径排序不变、各带正确
   sha256、不含任何排除规则字段；随后 restore 只恢复这两个文件，
   源目录保持不变。
2. 不带 --exclude 的对照：仍备份全部三个文件，行为与旧版一致。
3. 重复指定同一排除路径只排除一次，计数不减少第二次。
4. 排除路径逐字匹配：含中文与空格的文件名按原样排除；仅大小写不同、
   目录名、不存在的路径、通配符字面量均按“排除项未匹配普通文件”拒绝。
5. 非法形态：空字符串、绝对路径、带盘符路径、含反斜杠、含空/``.``/``..``
   分量，一律退出码 2，标准错误包含“排除路径无效”与原始参数。
6. 全部文件被合法排除时仍成功：清单 files 为空数组、data 为空目录，
   报告文件数 0；该快照可恢复为空目录。
7. 排除参数不绕过源目录安全检查：源目录内含符号链接时，即使排除路径
   合法也仍按既有符号链接规则拒绝。
8. 所有失败用例：标准输出为空、不创建快照、源目录保持原样。
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

# 夹具中的三个相对路径（斜杠分隔，与验收 demo 一致）。
NOTE_REL = "note.txt"
LOGS_DIR_REL = "logs"
CACHE_REL = "logs/cache.log"
EMPTY_REL = "empty.dat"

NOTE_BYTES = "demo 的 note.txt 内容\n第二行\n".encode("utf-8")
CACHE_BYTES = "cache.log：应被排除的日志内容\n".encode("utf-8")
EMPTY_BYTES = b""

DEMO_FILES = {
    NOTE_REL: NOTE_BYTES,
    CACHE_REL: CACHE_BYTES,
    EMPTY_REL: EMPTY_BYTES,
}

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
BACKUP_SUMMARY_MARKERS = ("已创建快照目录", "已备份文件数")
ERROR_PREFIX = "错误"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_INVALID = "排除路径无效"
REASON_UNMATCHED = "排除项未匹配普通文件"
REASON_SYMLINK = "源目录中包含符号链接"


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


class BackupExcludeTests(unittest.TestCase):
    """--exclude 排除备份的成功路径、确定性失败与现场不变性。"""

    def setUp(self):
        # 每个用例独立临时工作区，样例之间互不污染。
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-exclude-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "demo"
        self.snapshot = self.work / "snap"

        write_files(self.source, DEMO_FILES)

    # ---- 通用执行与断言 ----

    def run_backup(self, excludes=None, checksum=False, snapshot=None):
        """以公开入口执行 backup；excludes 为 None 时不带任何 --exclude。"""
        argv = ["backup", str(self.source), str(snapshot or self.snapshot)]
        if checksum:
            argv.append("--checksum")
        for raw in excludes or []:
            argv.extend(["--exclude", raw])
        return run_cmd(argv)

    def assert_source_unchanged(self, source_before, context):
        """备份（无论成败）不得改动源目录的路径集合、类型与字节。"""
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"备份后源目录目录树发生变化\n{context}",
        )

    def run_and_assert_rejected(self, excludes, reasons, label, *, literal=True):
        """失败用例公共流程：退出码 2、报错原因、标准输出为空、快照不存在，
        且源目录相对调用前保持不变。reasons 为须全部出现的原因片段。
        """
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"用例前提：快照路径必须事先不存在（{label}）",
        )
        source_before = capture_tree(self.source)

        proc = self.run_backup(excludes)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n排除: {excludes!r}\n"
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
            # 拒绝时标准错误应逐字复述原始排除参数（空串无可复述内容）。
            for raw in excludes:
                if raw != "":
                    self.assertIn(
                        raw, stderr,
                        f"标准错误应逐字复述原始排除参数: {raw!r}\n{context}",
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

    # ---- 成功：验收场景，--checksum --exclude logs/cache.log ----

    def test_exclude_one_file_with_checksum_then_restore(self):
        """排除 logs/cache.log：快照只含另外两个文件，恢复也只得这两个。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup([CACHE_REL], checksum=True)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 验收场景排除单个文件\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"标准输出应包含快照目录绝对路径\n{context}",
        )
        self.assertIn(
            "已备份文件数: 2", stdout,
            f"标准输出应只计算收录文件数 2\n{context}",
        )

        # data 目录恰好含另外两个文件的原始字节，被排除文件完全不出现。
        data_tree = capture_tree(self.snapshot / "data")
        self.assertEqual(
            data_tree,
            {NOTE_REL: ("file", NOTE_BYTES), EMPTY_REL: ("file", EMPTY_BYTES)},
            f"快照 data 应只含 note.txt 与 empty.dat 的原始字节\n{context}",
        )

        # 清单：版本 1、恰好两个条目、保持路径排序、摘要正确、无排除字段。
        doc = self.read_manifest()
        self.assertEqual(
            set(doc.keys()), {"version", "files"},
            f"清单顶层不应新增排除规则字段\n{context}",
        )
        self.assertEqual(doc["version"], 1, f"清单版本应为 1\n{context}")
        paths = [item["path"] for item in doc["files"]]
        self.assertEqual(
            paths, sorted([NOTE_REL, EMPTY_REL]),
            f"清单应恰好含两个收录文件且保持既有排序\n{context}",
        )
        for item in doc["files"]:
            self.assertEqual(
                set(item.keys()), {"path", "sha256"},
                f"清单条目只应含 path 与 sha256\n{context}",
            )
        digests = {item["path"]: item["sha256"] for item in doc["files"]}
        self.assertEqual(digests[NOTE_REL], sha256_hex(NOTE_BYTES),
                         f"note.txt 摘要应与其原始字节一致\n{context}")
        self.assertEqual(digests[EMPTY_REL], sha256_hex(EMPTY_BYTES),
                         f"empty.dat 摘要应为空字节串摘要\n{context}")

        # 随后 restore：只恢复这两个文件，字节一致。
        restored = self.work / "restored"
        proc = run_cmd(["restore", str(self.snapshot), str(restored)])
        r_stdout = proc.stdout.decode("utf-8", errors="replace")
        r_stderr = proc.stderr.decode("utf-8", errors="replace")
        context += (
            f"\n恢复阶段:\nexit={proc.returncode}\n"
            f"stdout={r_stdout!r}\nstderr={r_stderr!r}"
        )
        self.assertEqual(proc.returncode, 0, f"恢复退出码应为 0\n{context}")
        self.assertIn("已恢复文件数: 2", r_stdout,
                      f"恢复文件数应为 2\n{context}")
        self.assertEqual(
            capture_tree(restored),
            {NOTE_REL: ("file", NOTE_BYTES), EMPTY_REL: ("file", EMPTY_BYTES)},
            f"恢复结果应只含两个被收录文件的原始字节\n{context}",
        )
        self.assertFalse(
            (restored / LOGS_DIR_REL).exists(),
            f"被排除的 logs 目录不应在恢复结果中出现\n{context}",
        )

        # 源目录全程保持原样。
        self.assert_source_unchanged(source_before, context)

    # ---- 成功对照：不带 --exclude 时行为不变 ----

    def test_without_exclude_backs_up_all_files(self):
        """不带 --exclude：仍备份全部三个文件，清单不含排除规则字段。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup(None, checksum=True)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 不带 --exclude 对照\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 3", stdout,
                      f"不带 --exclude 应备份全部三个文件\n{context}")
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {
                NOTE_REL: ("file", NOTE_BYTES),
                LOGS_DIR_REL: ("dir", None),
                CACHE_REL: ("file", CACHE_BYTES),
                EMPTY_REL: ("file", EMPTY_BYTES),
            },
            f"不带 --exclude 时 data 应含全部三个文件\n{context}",
        )
        doc = self.read_manifest()
        self.assertEqual(set(doc.keys()), {"version", "files"},
                         f"清单顶层不应新增字段\n{context}")
        self.assertEqual(
            [item["path"] for item in doc["files"]],
            sorted([NOTE_REL, CACHE_REL, EMPTY_REL]),
            f"清单应含全部三个文件且保持既有排序\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 成功：重复排除只排除一次 ----

    def test_duplicate_exclude_excluded_once(self):
        """重复指定同一排除路径：只排除一次，文件数为 2。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup([CACHE_REL, CACHE_REL])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 重复排除同一路径\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 2", stdout,
                      f"重复排除不应重复计数\n{context}")
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {NOTE_REL: ("file", NOTE_BYTES), EMPTY_REL: ("file", EMPTY_BYTES)},
            f"重复排除应只排除一次 logs/cache.log\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 成功：中文与空格文件名逐字排除 ----

    def test_exclude_chinese_and_space_path_verbatim(self):
        """含中文与空格的相对路径按原样逐字排除。"""
        extra_rel = "嵌套 目录/二进制 文件.bin"
        extra_bytes = bytes([0x00, 0xFF, 0x10, 0x00])
        write_files(self.source, {extra_rel: extra_bytes})
        source_before = capture_tree(self.source)

        proc = self.run_backup([extra_rel])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 中文与空格路径逐字排除\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn("已备份文件数: 3", stdout,
                      f"排除一个后应收录三个文件\n{context}")
        data_tree = capture_tree(self.snapshot / "data")
        self.assertNotIn(extra_rel, data_tree,
                         f"被排除的中文路径不应出现在 data 中\n{context}")
        self.assertEqual(
            sorted(key for key, entry in data_tree.items()
                   if entry[0] == "file"),
            sorted([NOTE_REL, CACHE_REL, EMPTY_REL]),
            f"data 应恰好含其余三个文件\n{context}",
        )
        doc = self.read_manifest()
        self.assertNotIn(
            extra_rel,
            [item["path"] for item in doc["files"]],
            f"被排除的中文路径不应出现在清单中\n{context}",
        )
        self.assert_source_unchanged(source_before, context)

    # ---- 成功：全部文件被合法排除 ----

    def test_exclude_all_files_succeeds_with_empty_snapshot(self):
        """排除全部三个文件：空 files 数组、空 data 目录、文件数 0。"""
        source_before = capture_tree(self.source)

        proc = self.run_backup(
            [NOTE_REL, CACHE_REL, EMPTY_REL], checksum=True,
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

    # ---- 失败：非法排除路径形态 ----

    def test_reject_empty_string_exclude(self):
        """空字符串排除参数：非法，退出码 2。"""
        self.run_and_assert_rejected(
            [""], [REASON_INVALID], "空字符串排除参数",
        )

    def test_reject_absolute_exclude(self):
        """绝对路径排除参数：非法，不与源内相对路径混淆。"""
        self.run_and_assert_rejected(
            ["/note.txt"], [REASON_INVALID], "绝对路径排除参数",
        )

    def test_reject_drive_letter_exclude(self):
        """带盘符的排除参数（C:/note.txt）：非法。"""
        self.run_and_assert_rejected(
            ["C:/note.txt"], [REASON_INVALID], "带盘符路径",
        )

    def test_reject_backslash_exclude(self):
        """含反斜杠的排除参数：非法，不转换分隔符。"""
        self.run_and_assert_rejected(
            ["logs\\cache.log"], [REASON_INVALID], "含反斜杠",
        )

    def test_reject_empty_component_exclude(self):
        """a//b：含空分量的排除参数非法。"""
        self.run_and_assert_rejected(
            ["logs//cache.log"], [REASON_INVALID], "含空分量",
        )

    def test_reject_dot_component_exclude(self):
        """./note.txt：含当前目录分量，不归一化为 note.txt。"""
        self.run_and_assert_rejected(
            ["./note.txt"], [REASON_INVALID], "含点分量",
        )

    def test_reject_parent_component_exclude(self):
        """../note.txt：含上级目录分量的排除参数非法。"""
        self.run_and_assert_rejected(
            ["../note.txt"], [REASON_INVALID], "含上级目录分量",
        )

    # ---- 失败：形态合法但不匹配普通文件 ----

    def test_reject_exclude_directory(self):
        """排除路径指向目录 logs：不匹配普通文件，不展开目录。"""
        self.run_and_assert_rejected(
            [LOGS_DIR_REL], [REASON_UNMATCHED], "排除路径指向目录",
        )

    def test_reject_exclude_nonexistent(self):
        """排除路径不存在：不匹配任何普通文件。"""
        self.run_and_assert_rejected(
            ["no-such-file.txt"], [REASON_UNMATCHED], "排除路径不存在",
        )

    def test_reject_exclude_case_different(self):
        """仅大小写不同的 Note.txt：不匹配 note.txt，不忽略大小写。"""
        self.run_and_assert_rejected(
            ["Note.txt"], [REASON_UNMATCHED], "仅大小写不同",
        )

    def test_reject_exclude_glob_literal(self):
        """*.txt 作为字面值：不做通配符展开，不匹配任何普通文件。"""
        self.run_and_assert_rejected(
            ["*.txt"], [REASON_UNMATCHED], "通配符字面量",
        )

    # ---- 失败：排除参数不绕过源目录安全检查 ----

    @unittest.skipUnless(hasattr(os, "symlink"), "平台不支持符号链接")
    def test_exclude_does_not_bypass_symlink_rejection(self):
        """源目录内含符号链接时，合法排除参数仍触发现有符号链接拒绝。"""
        link_rel = "link-to-note"
        os.symlink("note.txt", str(self.source / link_rel))

        self.run_and_assert_rejected(
            [CACHE_REL], [REASON_SYMLINK, link_rel],
            "符号链接不被排除参数绕过",
            # 报错来自源目录安全检查，不复述排除参数，故关闭逐字复述核对。
            literal=False,
        )


if __name__ == "__main__":
    unittest.main()
