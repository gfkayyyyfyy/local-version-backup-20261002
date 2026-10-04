#!/usr/bin/env python3
"""backup 清单写入阶段发生 I/O 错误后回滚本次新建快照的回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py backup SOURCE SNAPSHOT --checksum

不调用任何内部函数、不新增产品功能、不修改快照格式。唯一的测试装置
是子进程内的受控故障：测试通过同目录下的 manifest_iofail_inject.py
（以 ``python -m manifest_iofail_inject`` 运行）在该次 backup 进程内
给清单写入路径打补丁，使其在指定时刻抛出 ``OSError("演示清单错误")``。
覆盖清单落盘的两个位置：

- ``write``：临时清单 manifest.json.tmp 的内容只部分写出即中断；
- ``replace``：临时清单已完整写出，os.replace 替换为 manifest.json
  时失败。

两个位置都先由注入器确认源目录中的全部文件已逐字节完整复制到快照
data（避免把参数拒绝或复制失败误判为清单回滚成功），随后才触发故障；
backup.py 自身的参数解析、源目录校验、目录创建、复制、摘要计算、
清单生成与失败清理代码全部真实执行。

仅依赖 Python 3 标准库；每个用例在独立临时目录中运行时准备源目录与
独立的快照目标：快照父目录事先存在，快照目标本身不存在，源目录与
快照目标互为独立路径。用例结束自动清理，不依赖真实磁盘故障、权限
调整或外部服务。

夹具：

- 源目录 ``note.txt``：四个字节 0x6F、0x6C、0x64、0x0A；
- 源目录 ``nested/data.bin``：三个字节 0x00、0xFF、0x10；
- 快照父目录内（与快照目标平级）另有 ``keep.txt``：keep 后接换行。

对普通备份与 --checksum 备份分别覆盖上述两个失败位置：backup 必须
返回退出码 2，标准输出为空，标准错误同时包含“写入清单失败”与
“演示清单错误”，且不出现异常堆栈；调用结束后本次快照目录整体不
存在（已复制的文件、嵌套目录与临时清单均不残留），源目录与快照父
目录中的 keep.txt 的相对路径、条目类型与文件字节保持不变（读取造成
的访问时间变化不参与比较），事先存在的快照父目录保留。

故障解除后，用相同参数向同一个快照目标重试必须返回 0、标准错误为
空，标准输出包含快照绝对路径与“已备份文件数: 2”；data 内恰好包含
上述两个文件及必要目录，内容逐字节一致；清单仍为版本 1，文件路径
按既有顺序（nested/data.bin、note.txt）排列，普通备份不含 sha256
字段，摘要备份的 sha256 与各文件原始字节一致。重试同样不改动源目录
与 keep.txt。已有排除规则、拒绝覆盖、恢复、恢复预览和 verify 的公开
行为不受本测试影响。
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
ROOT = TESTS_DIR.parent
BACKUP_SCRIPT = ROOT / "backup.py"
# 注入器作为普通模块放在 tests 目录：文件名不以 test 开头，不会被
# unittest 当作用例导入；通过 -m 在子进程中运行，本测试进程自身不装补丁。
INJECT_MODULE = "manifest_iofail_inject"

# 强制子进程按 UTF-8 输出，断言不依赖运行环境的区域设置。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# 夹具固定内容（十六进制给出的源目录形态）。
NOTE_REL = "note.txt"
NESTED_DIR = "nested"
BIN_REL = "nested/data.bin"
NOTE_BYTES = bytes([0x6F, 0x6C, 0x64, 0x0A])
BIN_BYTES = bytes([0x00, 0xFF, 0x10])
SOURCE_FILES = {NOTE_REL: NOTE_BYTES, BIN_REL: BIN_BYTES}
# 清单中文件路径的既有顺序（相对路径按码点升序）。
MANIFEST_ORDER = [BIN_REL, NOTE_REL]

# 快照父目录内、与快照目标平级的标记文件：清理快照时不得波及它。
KEEP_NAME = "keep.txt"
KEEP_BYTES = b"keep\n"

# 快照内的固定名称（与 README 的快照结构一致）。
DATA_DIRNAME = "data"
MANIFEST_NAME = "manifest.json"

# 故障位置（与注入器的 BACKUP_MANIFEST_IOFAIL_MODE 取值一致）。
MODE_WRITE = "write"
MODE_REPLACE = "replace"

# 故障原因文本（与注入器中抛出的 OSError 文案一致）。
IO_ERROR_TEXT = "演示清单错误"
REASON_MANIFEST_FAILED = "写入清单失败"
ERROR_PREFIX = "错误"
TRACEBACK_MARK = "Traceback"


def run_cmd(argv):
    """通过公开命令行在项目根目录执行 backup.py，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), *argv],
        cwd=str(ROOT),
        env=CHILD_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def backup_argv(source, snapshot, checksum):
    argv = ["backup", str(source), str(snapshot)]
    if checksum:
        argv.append("--checksum")
    return argv


def run_backup_with_fault(source, snapshot, checksum, mode, proof_path):
    """在子进程内安装受控清单故障后执行真实的 backup 命令行。

    故障参数仅通过本次进程专用的环境变量传入；命令行参数与 README
    的 ``backup SOURCE SNAPSHOT [--checksum]`` 完全一致。
    """
    env = dict(CHILD_ENV)
    # 模块搜索路径中加入 tests 目录，才能以 -m 找到注入器。
    env["PYTHONPATH"] = (
        str(TESTS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    )
    env["BACKUP_MANIFEST_IOFAIL_PROOF"] = str(proof_path)
    env["BACKUP_MANIFEST_IOFAIL_MODE"] = mode
    return subprocess.run(
        [
            sys.executable,
            "-m",
            INJECT_MODULE,
            str(BACKUP_SCRIPT),
            *backup_argv(source, snapshot, checksum),
        ],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def write_files(base, files):
    """在 base 下按 ``相对POSIX路径 -> 字节`` 写入文件。"""
    base = Path(base)
    for rel, data in files.items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def capture_tree(root):
    """递归记录目录树：相对路径 -> (类型, 字节或链接目标)。

    只记录相对路径、条目类型（目录/普通文件/符号链接）与文件字节，
    不记录访问/修改时间，因此读取导致的访问时间变化不参与比较。
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


def marker_state(path):
    """记录单个标记条目的类型与字节（不记录时间）。"""
    st = os.lstat(path)
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISDIR(mode):
        return ("dir", None)
    if stat.S_ISREG(mode):
        return ("file", path.read_bytes())
    return ("other", None)


def expected_manifest_bytes(checksum):
    """按既有快照格式构造本次夹具对应的完整清单字节。"""
    if checksum:
        files = [
            {
                "path": rel,
                "sha256": hashlib.sha256(SOURCE_FILES[rel]).hexdigest(),
            }
            for rel in MANIFEST_ORDER
        ]
    else:
        files = [{"path": rel} for rel in MANIFEST_ORDER]
    manifest = {"version": 1, "files": files}
    return (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode(
        "utf-8"
    )


class BackupManifestIoErrorTests(unittest.TestCase):
    """清单写入 I/O 错误后，本次新建的快照被整体回滚；重试可成功。

    普通备份与 --checksum 备份各覆盖“部分内容写出后中断”与“完整写出
    后替换失败”两个故障位置，跑相同的失败-回滚-重试流程。
    """

    def setUp(self):
        # 每个用例独立准备与清理：源目录、快照父目录共享同一临时父目录。
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-manifest-iofail-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        # 快照父目录事先存在；快照目标本身不存在，且与源目录互为独立路径。
        self.snapshot_parent = self.work / "snapshots"
        self.snapshot_parent.mkdir()
        self.snapshot = self.snapshot_parent / "snapshot"
        # 标记文件放在快照父目录内、与快照目标平级。
        self.keep = self.snapshot_parent / KEEP_NAME

        write_files(self.source, SOURCE_FILES)
        self.keep.write_bytes(KEEP_BYTES)

        # 夹具前提：目标不存在、父目录与标记文件就绪。
        self.assertFalse(os.path.lexists(self.snapshot))
        self.assertTrue(self.snapshot_parent.is_dir())
        self.assertTrue(self.keep.is_file())

    def _outside_state(self):
        """源目录与快照父目录内 keep.txt 的当前状态（不含时间）。"""
        return capture_tree(self.source), marker_state(self.keep)

    def _assert_outside_unchanged(self, before, label):
        """失败/重试前后源目录、keep.txt 与快照父目录保持一致。"""
        source_before, keep_before = before
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"源目录发生变化（{label}）",
        )
        self.assertEqual(
            marker_state(self.keep), keep_before,
            f"快照父目录内的 keep.txt 发生变化（{label}）",
        )
        self.assertEqual(
            self.keep.read_bytes(), KEEP_BYTES,
            f"keep.txt 字节发生变化（{label}）",
        )
        self.assertTrue(
            self.snapshot_parent.is_dir(),
            f"快照父目录应保留（{label}）",
        )

    def _assert_failure(self, proc, checksum, mode, proof_path, before):
        """核对失败退出码、输出、故障证据、回滚结果与源目录不变。"""
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        label = f"{'checksum' if checksum else 'plain'}/{mode}"
        context = (
            f"备份模式: {label}\n退出码={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

        # 故障未按预期触发（例如注入器前提失败导致 traceback 退出）也算失败。
        self.assertEqual(
            proc.returncode, 2, f"清单写入失败应返回退出码 2\n{context}",
        )
        self.assertEqual(stdout, "", f"失败后标准输出应为空\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for fragment in (REASON_MANIFEST_FAILED, IO_ERROR_TEXT):
            self.assertIn(
                fragment, stderr,
                f"标准错误缺少“{fragment}”\n{context}",
            )
        self.assertNotIn(
            TRACEBACK_MARK, stderr,
            f"标准错误不应出现异常堆栈\n{context}",
        )

        # 注入器在确认复制完成与清单状态之后才落盘的故障证据。
        self.assertTrue(
            proof_path.is_file(),
            f"故障证据缺失：受控故障可能未按预期触发\n{context}",
        )
        proof = json.loads(proof_path.read_text(encoding="utf-8"))
        self.assertEqual(
            proof.get("mode"), mode, f"故障证据的模式不符\n{context}",
        )

        # 故障触发前两个源文件均已完整复制到 data：相对路径、字节数与
        # SHA-256 都与源文件原始字节一致。
        copied = proof.get("copied")
        self.assertIsInstance(copied, list, f"故障证据结构不符\n{context}")
        self.assertEqual(
            [entry.get("rel") for entry in copied],
            MANIFEST_ORDER,
            f"故障前 data 中的已复制文件与源目录不符\n{context}",
        )
        for entry, rel in zip(copied, MANIFEST_ORDER):
            original = SOURCE_FILES[rel]
            self.assertEqual(
                entry.get("size"), len(original),
                f"故障前 {rel} 应已完整复制\n{context}",
            )
            self.assertEqual(
                entry.get("sha256"), hashlib.sha256(original).hexdigest(),
                f"故障前 {rel} 的字节应与源文件逐字节一致\n{context}",
            )

        # 临时清单在故障时刻处于所选位置要求的状态。
        expected = expected_manifest_bytes(checksum)
        self.assertEqual(
            proof.get("expected_manifest_size"), len(expected),
            f"期望清单大小不符\n{context}",
        )
        self.assertEqual(
            proof.get("expected_manifest_sha256"),
            hashlib.sha256(expected).hexdigest(),
            f"期望清单内容不符\n{context}",
        )
        tmp_size = proof.get("tmp_size")
        if mode == MODE_WRITE:
            # 内容只部分写出：已落盘字节少于完整清单。
            self.assertIsInstance(tmp_size, int, f"故障证据结构不符\n{context}")
            self.assertGreater(
                tmp_size, 0,
                f"write 模式下临时清单应已有部分内容落盘\n{context}",
            )
            self.assertLess(
                tmp_size, len(expected),
                f"write 模式下临时清单应只部分写出\n{context}",
            )
        else:
            # 内容已完整写出：临时清单与期望清单逐字节一致。
            self.assertEqual(
                tmp_size, len(expected),
                f"replace 模式下临时清单应已完整写出\n{context}",
            )
            self.assertEqual(
                proof.get("tmp_sha256"), hashlib.sha256(expected).hexdigest(),
                f"replace 模式下临时清单内容应与期望清单一致\n{context}",
            )

        # 本次快照目录整体消失：已复制文件、嵌套目录与临时清单均不残留。
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"清单写入失败后快照目录应被整体清理: {self.snapshot}\n{context}",
        )
        # 事先存在的快照父目录保留，清理只能针对本次新建的快照目标。
        self.assertTrue(
            self.snapshot_parent.is_dir(),
            f"快照父目录应保留\n{context}",
        )

        self._assert_outside_unchanged(before, f"{label} 故障后")

    def _assert_retry_succeeds(self, checksum, before):
        """解除故障后以相同参数重试同一快照目标：完全成功且产物正确。"""
        retry = run_cmd(backup_argv(self.source, self.snapshot, checksum))
        stdout = retry.stdout.decode("utf-8", errors="replace")
        stderr = retry.stderr.decode("utf-8", errors="replace")
        mode = "checksum" if checksum else "plain"
        context = (
            f"故障解除后的重试（{mode}）\n退出码={retry.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(retry.returncode, 0, f"重试应返回 0\n{context}")
        self.assertEqual(stderr, "", f"重试成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"标准输出应包含快照绝对路径\n{context}",
        )
        self.assertIn(
            "已备份文件数: 2", stdout,
            f"标准输出应包含文件数 2\n{context}",
        )

        # data 内恰好包含两个文件及必要目录，内容与源逐字节一致。
        data_dir = self.snapshot / DATA_DIRNAME
        self.assertEqual(
            capture_tree(data_dir),
            {
                NOTE_REL: ("file", NOTE_BYTES),
                NESTED_DIR: ("dir", None),
                BIN_REL: ("file", BIN_BYTES),
            },
            f"重试快照 data 的路径/类型/字节与预期不符\n{context}",
        )

        # 清单仍为版本 1，路径按既有顺序排列；摘要字段按模式存在或缺席。
        manifest = json.loads(
            (self.snapshot / MANIFEST_NAME).read_bytes().decode("utf-8")
        )
        self.assertEqual(
            manifest.get("version"), 1, f"清单版本应为 1\n{context}",
        )
        files = manifest.get("files")
        self.assertIsInstance(files, list, f"清单 files 应为数组\n{context}")
        self.assertEqual(
            [entry.get("path") for entry in files],
            MANIFEST_ORDER,
            f"清单文件路径应按既有顺序排列\n{context}",
        )
        for entry, rel in zip(files, MANIFEST_ORDER):
            original = SOURCE_FILES[rel]
            if checksum:
                self.assertEqual(
                    set(entry), {"path", "sha256"},
                    f"摘要备份的清单条目字段不符: {rel}\n{context}",
                )
                self.assertEqual(
                    entry["sha256"], hashlib.sha256(original).hexdigest(),
                    f"摘要应与文件原始字节一致: {rel}\n{context}",
                )
            else:
                self.assertEqual(
                    set(entry), {"path"},
                    f"普通备份的清单条目不应包含 sha256: {rel}\n{context}",
                )

        self._assert_outside_unchanged(before, f"{mode} 重试后")

    def _run_failure_and_retry(self, checksum, mode):
        """完整流程：受控失败并回滚，随后解除故障用相同参数重试成功。"""
        proof_path = self.work / (
            f"proof-{'checksum' if checksum else 'plain'}-{mode}.json"
        )

        before = self._outside_state()
        proc = run_backup_with_fault(
            self.source, self.snapshot, checksum, mode, proof_path,
        )
        self._assert_failure(proc, checksum, mode, proof_path, before)

        # 故障已随失败子进程结束而解除；以相同参数向同一目标重试。
        self.assertFalse(os.path.lexists(self.snapshot))
        self._assert_retry_succeeds(checksum, self._outside_state())

    def test_plain_backup_manifest_write_ioerror_rolls_back_and_retry_ok(self):
        """普通备份：临时清单部分写出后中断，回滚后重试成功。"""
        self._run_failure_and_retry(False, MODE_WRITE)

    def test_plain_backup_manifest_replace_ioerror_rolls_back_and_retry_ok(self):
        """普通备份：临时清单完整写出、替换为 manifest.json 时失败。"""
        self._run_failure_and_retry(False, MODE_REPLACE)

    def test_checksum_backup_manifest_write_ioerror_rolls_back_and_retry_ok(self):
        """--checksum 备份：临时清单部分写出后中断，回滚后重试成功。"""
        self._run_failure_and_retry(True, MODE_WRITE)

    def test_checksum_backup_manifest_replace_ioerror_rolls_back_and_retry_ok(self):
        """--checksum 备份：临时清单完整写出、替换时失败，重试摘要正确。"""
        self._run_failure_and_retry(True, MODE_REPLACE)


if __name__ == "__main__":
    unittest.main()
