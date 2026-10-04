#!/usr/bin/env python3
"""backup 复制阶段发生 I/O 错误后清理本次新建快照的回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py backup SOURCE SNAPSHOT --checksum

不调用任何内部函数、不新增产品功能。唯一的测试装置是子进程内的
受控故障：测试通过同目录下的 backup_iofail_inject.py（以
``python -m backup_iofail_inject`` 运行）在该次 backup 进程内给
内建 open 打补丁，使写入快照 data 的文件对象在指定时刻抛出
``OSError("演示复制错误")``。普通备份的 shutil.copyfileobj 与
--checksum 备份的手写复制循环都真实执行，补丁只作用于写入本次
快照 data 的文件，且在确认事实之后才触发：

1. a.txt 已逐字节完整写入快照（磁盘字节与源文件一致）；
2. nested/b.bin 的目标文件恰好写入源文件的首个字节并已落盘。

backup.py 自身的参数解析、源目录校验、目录创建、复制、摘要计算与
失败清理代码全部真实执行。

仅依赖 Python 3 标准库；源目录、快照目标、快照父目录与外部标记都
在独立临时目录中运行时准备：源目录与快照目标互为独立路径，快照
父目录事先存在，快照目标本身不存在。用例结束自动清理，不依赖真实
磁盘故障、管理员权限或外部服务。

夹具源目录：

- ``a.txt``：三个字节 0x68、0x69、0x0A；
- ``nested/b.bin``：三个字节 0x00、0xFF、0x10；
- 源目录外（与其平级）另有 ``keep.txt``：0x6B、0x65、0x65、0x70、0x0A。

对普通备份与 --checksum 备份分别覆盖同一个失败场景：backup 必须
返回退出码 2，标准输出为空，标准错误同时包含“复制文件失败”、
``nested/b.bin`` 与“演示复制错误”；调用结束后本次快照目录整体不
存在（已完整写入的文件、部分写入的文件与嵌套目录全部移除），源
目录与外部 keep.txt 的相对路径、条目类型与文件字节保持不变
（读取造成的访问时间变化不参与比较）。

故障解除后，用相同参数向同一个快照目标重试必须返回 0、标准错误
为空，标准输出包含快照绝对路径与“已备份文件数: 2”；data 内恰好
包含上述两个文件及必要目录，内容逐字节一致；清单仍为版本 1，
文件路径按既有顺序（a.txt、nested/b.bin）排列，普通备份不含
sha256 字段，摘要备份的 sha256 与文件原始字节一致。重试同样不改
动源目录与 keep.txt。已有排除规则、拒绝覆盖、恢复和 verify 的公开
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
INJECT_MODULE = "backup_iofail_inject"

# 强制子进程按 UTF-8 输出，断言不依赖运行环境的区域设置。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# 夹具固定内容（十六进制给出的源目录形态）。
A_REL = "a.txt"
NESTED_DIR = "nested"
B_REL = "nested/b.bin"
A_BYTES = bytes([0x68, 0x69, 0x0A])
B_BYTES = bytes([0x00, 0xFF, 0x10])
SOURCE_FILES = {A_REL: A_BYTES, B_REL: B_BYTES}

# 源目录之外、与其平级的独立标记文件：清理快照时不得波及它。
KEEP_NAME = "keep.txt"
KEEP_BYTES = bytes([0x6B, 0x65, 0x65, 0x70, 0x0A])

# 快照内的固定名称（与 README 的快照结构一致）。
DATA_DIRNAME = "data"
MANIFEST_NAME = "manifest.json"

# 故障原因文本（与注入器中抛出的 OSError 文案一致）。
IO_ERROR_TEXT = "演示复制错误"
REASON_COPY_FAILED = "复制文件失败"
ERROR_PREFIX = "错误"


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


def run_backup_with_fault(source, snapshot, checksum, proof_path):
    """在子进程内安装受控复制故障后执行真实的 backup 命令行。

    故障参数仅通过本次进程专用的环境变量传入；命令行参数与 README
    的 ``backup SOURCE SNAPSHOT [--checksum]`` 完全一致。
    """
    env = dict(CHILD_ENV)
    # 模块搜索路径中加入 tests 目录，才能以 -m 找到注入器。
    env["PYTHONPATH"] = (
        str(TESTS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    )
    env["BACKUP_IOFAIL_PROOF"] = str(proof_path)
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
    """记录源目录外单个标记条目的类型与字节（不记录时间）。"""
    st = os.lstat(path)
    mode = st.st_mode
    if stat.S_ISLNK(mode):
        return ("symlink", os.readlink(path))
    if stat.S_ISDIR(mode):
        return ("dir", None)
    if stat.S_ISREG(mode):
        return ("file", path.read_bytes())
    return ("other", None)


class BackupCopyIoErrorTests(unittest.TestCase):
    """复制阶段 I/O 错误后，本次新建的快照被整体清理；重试可成功。

    普通备份与 --checksum 备份各跑一遍完全相同的失败-清理-重试流程。
    """

    def setUp(self):
        # 每个用例独立准备与清理：源目录、快照父目录共享同一临时父目录。
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-iofail-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        # 快照父目录事先存在；快照目标本身不存在，且与源目录互为独立路径。
        self.snapshot_parent = self.work / "snapshots"
        self.snapshot_parent.mkdir()
        self.snapshot = self.snapshot_parent / "snapshot"
        self.keep = self.work / KEEP_NAME

        write_files(self.source, SOURCE_FILES)
        self.keep.write_bytes(KEEP_BYTES)

        # 夹具前提：目标不存在、源与目标互不在对方之内。
        self.assertFalse(os.path.lexists(self.snapshot))
        self.assertTrue(self.snapshot_parent.is_dir())

    def _outside_state(self):
        """源目录与外部 keep.txt 的当前状态（路径/类型/字节，不含时间）。"""
        return capture_tree(self.source), marker_state(self.keep)

    def _assert_outside_unchanged(self, before, label):
        """失败/重试前后源目录与 keep.txt 保持一致。"""
        source_before, keep_before = before
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"源目录发生变化（{label}）",
        )
        self.assertEqual(
            marker_state(self.keep), keep_before,
            f"源目录外的 keep.txt 发生变化（{label}）",
        )
        self.assertEqual(
            self.keep.read_bytes(), KEEP_BYTES,
            f"keep.txt 字节发生变化（{label}）",
        )

    def _assert_failure(self, proc, checksum, proof_path, before):
        """核对失败退出码、输出、故障证据、清理结果与源目录不变。"""
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        mode = "checksum" if checksum else "plain"
        context = (
            f"备份模式: {mode}\n退出码={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

        # 故障未按预期触发（例如注入器前提失败导致 traceback 退出）也算失败。
        self.assertEqual(
            proc.returncode, 2, f"复制失败应返回退出码 2\n{context}",
        )
        self.assertEqual(stdout, "", f"失败后标准输出应为空\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for fragment in (REASON_COPY_FAILED, B_REL, IO_ERROR_TEXT):
            self.assertIn(
                fragment, stderr,
                f"标准错误缺少“{fragment}”\n{context}",
            )

        # 注入器在确认完整复制与部分写入都已发生之后才落盘的故障证据。
        self.assertTrue(
            proof_path.is_file(),
            f"故障证据缺失：受控故障可能未按预期触发\n{context}",
        )
        proof = json.loads(proof_path.read_text(encoding="utf-8"))

        # a.txt 在故障前已完整复制：大小与 SHA-256 都与源文件原始字节一致。
        self.assertEqual(
            set(proof), {"completed", "partial"},
            f"故障证据结构不符\n{context}",
        )
        completed = proof["completed"]
        self.assertEqual(completed.get("rel"), A_REL, f"故障证据内容不符\n{context}")
        self.assertEqual(
            completed.get("size"), len(A_BYTES),
            f"故障前 a.txt 应已完整写入\n{context}",
        )
        self.assertEqual(
            completed.get("sha256"), hashlib.sha256(A_BYTES).hexdigest(),
            f"故障前 a.txt 的字节应与源文件逐字节一致\n{context}",
        )

        # nested/b.bin 在故障前恰好写入首个字节（0x00），即部分写入确实发生。
        partial = proof["partial"]
        self.assertEqual(partial.get("rel"), B_REL, f"故障证据内容不符\n{context}")
        self.assertEqual(
            partial.get("size"), 1,
            f"故障前 nested/b.bin 应只有部分字节\n{context}",
        )
        self.assertEqual(
            partial.get("first_byte_hex"), B_BYTES[:1].hex(),
            f"故障前 nested/b.bin 的首个字节应来自源文件\n{context}",
        )

        # 本次快照目录整体消失：已完成文件、部分文件与嵌套目录全部清理。
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"复制失败后快照目录应被整体清理: {self.snapshot}\n{context}",
        )
        # 事先存在的快照父目录保留，清理只能针对本次新建的快照目标。
        self.assertTrue(
            self.snapshot_parent.is_dir(),
            f"快照父目录应保留\n{context}",
        )

        self._assert_outside_unchanged(before, f"{mode} 故障后")

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
                A_REL: ("file", A_BYTES),
                NESTED_DIR: ("dir", None),
                B_REL: ("file", B_BYTES),
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
            [A_REL, B_REL],
            f"清单文件路径应按既有顺序排列\n{context}",
        )
        for entry, rel in zip(files, (A_REL, B_REL)):
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

    def _run_failure_and_retry(self, checksum):
        """完整流程：受控失败并清理，随后解除故障用相同参数重试成功。"""
        proof_path = self.work / (
            "proof-checksum.json" if checksum else "proof-plain.json"
        )

        before = self._outside_state()
        proc = run_backup_with_fault(
            self.source, self.snapshot, checksum, proof_path,
        )
        self._assert_failure(proc, checksum, proof_path, before)

        # 故障已随失败子进程结束而解除；以相同参数向同一目标重试。
        self.assertFalse(os.path.lexists(self.snapshot))
        self._assert_retry_succeeds(checksum, self._outside_state())

    def test_plain_backup_copy_ioerror_cleans_snapshot_and_retry_ok(self):
        """普通备份：a.txt 完整写入、b.bin 部分写入后失败，清理后重试成功。"""
        self._run_failure_and_retry(False)

    def test_checksum_backup_copy_ioerror_cleans_snapshot_and_retry_ok(self):
        """--checksum 备份：同一失败场景，清理后重试成功且摘要正确。"""
        self._run_failure_and_retry(True)


if __name__ == "__main__":
    unittest.main()
