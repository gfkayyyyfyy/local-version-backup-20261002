#!/usr/bin/env python3
"""backup 复制阶段发生 I/O 错误后清理本次新建快照的回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT [--checksum]

不调用任何内部函数、不新增产品功能。唯一的测试装置是子进程内的受控
故障：测试通过同目录下的 backup_iofail_inject.py（以
``python -m backup_iofail_inject`` 运行）在该次 backup 进程内给内建
open 打补丁，让写入快照内 nested/b.bin 的目标文件在落盘首个字节后抛出
``OSError("演示复制错误")``；普通备份（shutil.copyfileobj）与
--checksum 备份（_copy_bytes_and_hash 的读写循环）都以 open(dst, "wb")
打开目标文件，因此同一装置覆盖两种模式。补丁触发前先确认 data/a.txt
已完整写入快照、目标文件已确有部分字节（确认结果写入证据文件供本测试
核对），随后才抛出预定错误。backup.py 自身的参数解析、源目录校验、
快照创建、复制与失败清理代码全部真实执行。

仅依赖 Python 3 标准库；全部源目录、快照目标及其共同父目录都在独立
临时目录中运行时准备，快照父目录事先存在、快照目标本身不存在且位于
源目录之外，用例结束自动清理，不依赖真实磁盘故障、管理员权限或外部
服务。

夹具源目录：

- ``a.txt``：字节 68 69 0a；
- ``nested/b.bin``：字节 00 ff 10。

源目录之外另有独立标记 ``keep.txt``（字节 6b 65 65 70 0a），用于
验证清理只针对本次新建的快照。

普通备份与 --checksum 备份覆盖同一个失败场景：a.txt 完整写入快照后，
nested/b.bin 的目标文件写入首个字节时触发 OSError。两种模式下 backup
都必须返回退出码 2，标准输出为空，标准错误同时包含“复制文件失败”、
nested/b.bin 与“演示复制错误”；调用结束后本次快照目录整体不存在
（已写完的文件、部分文件与嵌套目录全部移除），源目录与 keep.txt 的
相对路径、条目类型和文件字节与调用前一致（访问时间不参与比较）。

故障解除后，用相同参数向同一个快照目标重试必须返回 0、标准错误为空，
标准输出包含快照绝对路径与“已备份文件数: 2”；data 内恰好包含两个
文件及必要目录且内容逐字节一致，清单仍为版本 1、文件路径按既有顺序
排列，普通备份不含 sha256，--checksum 备份的摘要与文件原始字节一致；
重试同样保持源目录与 keep.txt 不变。已有 --exclude、拒绝覆盖、
restore 与 verify 的公开行为不受本测试影响。
"""

import hashlib
import json
import os
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

# 夹具固定内容（题目要求的源目录形态，字节按十六进制给出）。
A_REL = "a.txt"
NESTED_DIR = "nested"
B_REL = "nested/b.bin"
A_BYTES = bytes([0x68, 0x69, 0x0A])
B_BYTES = bytes([0x00, 0xFF, 0x10])
SOURCE_FILES = {A_REL: A_BYTES, B_REL: B_BYTES}

# 源目录之外的独立标记：清理快照时不得波及它。
KEEP_REL = "keep.txt"
KEEP_BYTES = bytes([0x6B, 0x65, 0x65, 0x70, 0x0A])

# 故障原因文本（与注入器中抛出的 OSError 文案一致）。
IO_ERROR_TEXT = "演示复制错误"
REASON_COPY_FAILED = "复制文件失败"
ERROR_PREFIX = "错误"

# 清单中文件路径的既有顺序（相对路径排序结果）。
MANIFEST_ORDER = [A_REL, B_REL]


def run_cmd(argv):
    """通过公开命令行在项目根目录执行 backup.py，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), *argv],
        cwd=str(ROOT),
        env=CHILD_ENV,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def run_backup_with_fault(source, snapshot, checksum, evidence):
    """在子进程内安装受控复制故障后执行真实的 backup 命令行。

    故障参数仅通过本进程不复用的环境变量传入；命令行参数与 README 的
    ``backup SOURCE SNAPSHOT [--checksum]`` 完全一致。
    """
    env = dict(CHILD_ENV)
    # 模块搜索路径中加入 tests 目录，才能以 -m 找到注入器。
    env["PYTHONPATH"] = (
        str(TESTS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    )
    env["BACKUP_IOFAIL_SNAPSHOT"] = str(Path(snapshot).resolve(strict=False))
    env["BACKUP_IOFAIL_EVIDENCE"] = str(evidence)
    argv = [
        sys.executable,
        "-m",
        INJECT_MODULE,
        str(BACKUP_SCRIPT),
        "backup",
        str(source),
        str(snapshot),
    ]
    if checksum:
        argv.append("--checksum")
    return subprocess.run(
        argv,
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
    """递归记录目录树：相对路径 -> (类型, 字节或链接目标)，与遍历顺序无关。

    只记录相对路径、条目类型与文件字节，不记录访问时间等元数据，
    因此读取文件导致的访问时间变化不参与比较。
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


class BackupCopyIoErrorTests(unittest.TestCase):
    """复制阶段 I/O 错误后，本次新建的快照被整体清理；重试可成功。"""

    def setUp(self):
        # 每个用例独立准备与清理：源目录、快照目标共享同一临时父目录。
        self._tmp = tempfile.TemporaryDirectory(prefix="backup-iofail-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"
        self.keep = self.work / KEEP_REL
        self.evidence = self.work / "fault-evidence.txt"

        write_files(self.source, SOURCE_FILES)
        write_files(self.work, {KEEP_REL: KEEP_BYTES})

        # 夹具前提：快照父目录事先存在，快照目标本身不存在，且位于源目录之外。
        self.assertTrue(self.work.is_dir())
        self.assertFalse(os.path.lexists(self.snapshot))
        self.assertFalse(
            self.snapshot.resolve(strict=False)
            == self.source.resolve(strict=False)
        )

    def _assert_source_and_keep_unchanged(self, before, label):
        """源目录与 keep.txt 的相对路径、条目类型和文件字节均与调用前一致。"""
        self.assertEqual(
            capture_tree(self.source), before,
            f"调用后源目录目录树发生变化（{label}）",
        )
        self.assertEqual(
            self.keep.read_bytes(), KEEP_BYTES,
            f"调用后 {KEEP_REL} 字节被改动（{label}）",
        )

    def _assert_failed_and_cleaned(self, proc, label):
        """核对失败退出码、标准输出/错误、故障证据与清理结果。"""
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"{label}\n退出码={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

        # 两种模式都返回 2。
        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        # 标准输出必须为空：没有任何成功摘要。
        self.assertEqual(stdout, "", f"失败后标准输出应为空\n{context}")
        # 标准错误同时包含失败阶段、出问题文件的相对路径与底层原因。
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for fragment in (REASON_COPY_FAILED, B_REL, IO_ERROR_TEXT):
            self.assertIn(
                fragment, stderr,
                f"标准错误缺少“{fragment}”\n{context}",
            )

        # 故障必须按预期触发：注入器在确认 a.txt 已完整写入快照、
        # nested/b.bin 的目标文件已确有部分字节之后，才抛出预定错误。
        self.assertTrue(
            self.evidence.is_file(),
            f"故障未按预期触发（缺少证据文件）\n{context}",
        )
        evidence_text = self.evidence.read_text(encoding="utf-8")
        self.assertIn(
            f"完整复制确认: {A_REL}", evidence_text,
            f"触发前未确认 a.txt 完整写入快照\n{context}",
        )
        self.assertIn(
            f"部分写入确认: {B_REL}", evidence_text,
            f"触发前未确认 nested/b.bin 部分写入\n{context}",
        )

        # 调用结束后本次快照目录整体不存在（含已写完文件、部分文件和嵌套目录）。
        self.assertFalse(
            os.path.lexists(self.snapshot),
            f"复制失败后本次快照目录应被整体清理: {self.snapshot}\n{context}",
        )

    def _assert_retry_succeeds(self, proc, checksum, label):
        """核对故障解除后的重试：成功摘要、快照内容与清单形态。"""
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"{label}\n退出码={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"重试应返回 0\n{context}")
        self.assertEqual(stderr, "", f"重试成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.snapshot.resolve()), stdout,
            f"标准输出应包含快照绝对路径\n{context}",
        )
        self.assertIn(
            "已备份文件数: 2", stdout,
            f"标准输出应包含文件数 2\n{context}",
        )

        # data 内恰好包含两个文件及必要目录，内容逐字节一致。
        self.assertEqual(
            capture_tree(self.snapshot / "data"),
            {
                A_REL: ("file", A_BYTES),
                NESTED_DIR: ("dir", None),
                B_REL: ("file", B_BYTES),
            },
            f"重试后 data 目录树（路径/类型/字节）与预期不符\n{context}",
        )

        # 清单仍为版本 1，文件路径按既有顺序排列。
        manifest_path = self.snapshot / "manifest.json"
        self.assertTrue(
            manifest_path.is_file(),
            f"重试后清单缺失: {manifest_path}\n{context}",
        )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            manifest.get("version"), 1,
            f"清单版本应为 1\n{context}",
        )
        files = manifest.get("files")
        self.assertIsInstance(files, list, f"清单 files 应为数组\n{context}")
        self.assertEqual(
            [entry.get("path") for entry in files], MANIFEST_ORDER,
            f"清单文件路径顺序应为 {MANIFEST_ORDER}\n{context}",
        )
        expected_bytes = {A_REL: A_BYTES, B_REL: B_BYTES}
        for entry in files:
            rel = entry["path"]
            if checksum:
                # 摘要备份：摘要与文件原始字节一致。
                self.assertEqual(
                    entry.get("sha256"),
                    hashlib.sha256(expected_bytes[rel]).hexdigest(),
                    f"{rel} 的 sha256 与原始字节不符\n{context}",
                )
            else:
                # 普通备份：不包含 sha256 字段。
                self.assertNotIn(
                    "sha256", entry,
                    f"普通备份的清单不应包含 sha256: {rel}\n{context}",
                )

    def _run_scenario(self, checksum, label):
        """单个模式的完整流程：复制失败清理快照后，解除故障重试同一目标。"""
        # 调用前的源目录应在重试阶段仍逐字节保持，先记录基线。
        before_failure = capture_tree(self.source)

        proc = run_backup_with_fault(
            self.source, self.snapshot, checksum, self.evidence
        )
        self._assert_failed_and_cleaned(proc, f"{label}：故障备份")
        self._assert_source_and_keep_unchanged(before_failure, f"{label} 故障后")

        # 解除故障（普通公开命令，不安装任何补丁）：用相同参数向同一个
        # 快照目标重试必须完全成功。
        before_retry = capture_tree(self.source)
        argv = ["backup", str(self.source), str(self.snapshot)]
        if checksum:
            argv.append("--checksum")
        retry = run_cmd(argv)
        self._assert_retry_succeeds(retry, checksum, f"{label}：故障解除后重试")
        self._assert_source_and_keep_unchanged(before_retry, f"{label} 重试后")

    def test_plain_backup_copy_ioerror_cleans_snapshot_then_retry(self):
        """普通备份：nested/b.bin 部分写入后失败，快照整体清理，重试成功。"""
        self._run_scenario(checksum=False, label="普通备份")

    def test_checksum_backup_copy_ioerror_cleans_snapshot_then_retry(self):
        """--checksum 备份：同一失败场景，快照整体清理，重试成功且摘要正确。"""
        self._run_scenario(checksum=True, label="摘要备份")


if __name__ == "__main__":
    unittest.main()
