#!/usr/bin/env python3
"""restore 复制阶段发生 I/O 错误后清理本次新建目标的回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST

不调用任何内部函数、不新增产品功能。唯一的测试装置是子进程内的
受控故障：测试通过同目录下的 restore_iofail_inject.py（以
``python -m restore_iofail_inject`` 运行）在该次 restore 进程内给
shutil.copyfileobj 打补丁，让复制在指定时刻抛出
``OSError("演示复制错误")``；补丁只作用于写入本次恢复目标的文件，
且在确认目标文件确实存在、已有非空内容之后才触发。backup.py 自身的
参数解析、清单校验、目录创建、复制与失败清理代码全部真实执行。

仅依赖 Python 3 标准库；全部源目录、快照、恢复目标及其共同父目录都
在独立临时目录中运行时准备，恢复目标事先不存在且位于快照之外，用例
结束自动清理，不依赖真实磁盘故障、管理员权限或外部服务。

夹具快照由不带 --checksum 的公开 backup 命令生成（版本 1），源目录：

- ``a.txt``：UTF-8 文本 ``old\\n``；
- ``nested/b.bin``：四个字节 0x00、0xFF、0x10、0x00。

覆盖两个确定的失败场景：

1. first：首个文件 a.txt 已写入部分字节时复制失败；
2. second：a.txt 已完整恢复，nested/b.bin 已写入部分字节时复制失败。

两种场景下 restore 必须返回退出码 2，标准输出为空，标准错误同时包含
“恢复文件失败”、出问题的相对路径与“演示复制错误”；调用结束后恢复
目标整体不存在（已完成文件、部分文件与嵌套目录全部移除），共同父目录
中的独立标记文件保留原路径和字节，源目录与快照的相对路径、目录结构
及全部文件字节与调用前一致。

故障解除后，对同一快照和同一目标再次恢复必须返回 0、标准错误为空，
标准输出包含目标绝对路径与“已恢复文件数: 2”，目标仅含上述两个文件
及必要目录，内容逐字节一致。已有 backup、restore --file、verify 的
行为不受本测试影响。
"""

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
INJECT_MODULE = "restore_iofail_inject"

# 强制子进程按 UTF-8 输出，断言不依赖运行环境的区域设置。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# 夹具固定内容（README 要求的源目录形态）。
A_REL = "a.txt"
NESTED_DIR = "nested"
B_REL = "nested/b.bin"
A_BYTES = "old\n".encode("utf-8")
B_BYTES = bytes([0x00, 0xFF, 0x10, 0x00])
SOURCE_FILES = {A_REL: A_BYTES, B_REL: B_BYTES}

# 共同父目录中与恢复目标平级的独立标记：清理恢复目标时不得波及它。
MARKER_REL = "sibling-marker.bin"
MARKER_BYTES = bytes([0xDE, 0xAD, 0xBE, 0xEF])

# 故障原因文本（与注入器中抛出的 OSError 文案一致）。
IO_ERROR_TEXT = "演示复制错误"
REASON_COPY_FAILED = "恢复文件失败"
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


def run_restore_with_fault(snapshot, dest, stage):
    """在子进程内安装受控复制故障后执行真实的 restore 命令行。

    故障参数仅通过本进程不复用的环境变量传入；命令行参数与 README
    的 ``restore SNAPSHOT DEST`` 完全一致。
    """
    env = dict(CHILD_ENV)
    # 模块搜索路径中加入 tests 目录，才能以 -m 找到注入器。
    env["PYTHONPATH"] = (
        str(TESTS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    )
    env["RESTORE_IOFAIL_TARGET"] = str(Path(dest).resolve(strict=False))
    env["RESTORE_IOFAIL_STAGE"] = stage
    return subprocess.run(
        [
            sys.executable,
            "-m",
            INJECT_MODULE,
            str(BACKUP_SCRIPT),
            "restore",
            str(snapshot),
            str(dest),
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
        for key, value in capture_tree(root).items()
        if value[0] == "file"
    }


class RestoreCopyIoErrorTests(unittest.TestCase):
    """复制阶段 I/O 错误后，本次新建的恢复目标被整体清理；重试可成功。"""

    def setUp(self):
        # 每个用例独立准备与清理：源目录、快照、恢复目标共享同一临时父目录。
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-iofail-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"
        self.dest = self.work / "restored"

        write_files(self.source, SOURCE_FILES)

        # 快照必须由不带 --checksum 的公开 backup 入口生成。
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 夹具前提：恢复目标事先不存在，且快照不在恢复目标之内。
        self.assertFalse(os.path.lexists(self.dest))
        self.assertFalse(
            self.dest.resolve(strict=False) == self.snapshot.resolve()
        )

        # 共同父目录中的独立标记文件，验证清理只针对本次新建的目标。
        write_files(self.work, {MARKER_REL: MARKER_BYTES})

    def _assert_failed_and_cleaned(self, proc, stage, failing_rel):
        """核对失败退出码、标准输出/错误、清理结果与源/快照不变。"""
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"故障场景: {stage}\n退出码={proc.returncode}\n"
            f"stdout={stdout!r}\nstderr={stderr!r}"
        )

        # 两种场景都返回 2。
        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        # 标准输出必须为空：没有任何成功摘要。
        self.assertEqual(stdout, "", f"失败后标准输出应为空\n{context}")
        # 标准错误同时包含失败阶段、出问题文件的相对路径与底层原因。
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        for fragment in (REASON_COPY_FAILED, failing_rel, IO_ERROR_TEXT):
            self.assertIn(
                fragment, stderr,
                f"标准错误缺少“{fragment}”\n{context}",
            )

        # 调用结束后恢复目标整体不存在（含已完成、部分写入的文件和嵌套目录）。
        self.assertFalse(
            os.path.lexists(self.dest),
            f"复制失败后恢复目标应被整体清理: {self.dest}\n{context}",
        )
        self.assertFalse(
            (self.work / NESTED_DIR).exists(),
            f"嵌套目录不应残留在恢复目标位置\n{context}",
        )

        # 共同父目录中的独立标记文件保留原路径和字节。
        self.assertEqual(
            (self.work / MARKER_REL).read_bytes(), MARKER_BYTES,
            f"清理恢复目标时波及父目录中的标记文件\n{context}",
        )

    def _assert_source_and_snapshot_unchanged(self, before, label):
        """源目录与快照的相对路径、目录结构及全部文件字节均与调用前一致。"""
        source_tree, snapshot_tree = before
        self.assertEqual(
            capture_tree(self.source), source_tree,
            f"调用后源目录目录树发生变化（{label}）",
        )
        self.assertEqual(
            capture_tree(self.snapshot), snapshot_tree,
            f"调用后快照目录树发生变化（{label}）",
        )

    def _run_fault_scenario(self, stage, failing_rel):
        """单个故障场景的完整流程：失败清理后，解除故障重试同一目标。"""
        # 调用前的源目录与快照应在重试阶段仍逐字节保持，先记录基线。
        before_failure = (
            capture_tree(self.source), capture_tree(self.snapshot),
        )

        proc = run_restore_with_fault(self.snapshot, self.dest, stage)
        self._assert_failed_and_cleaned(proc, stage, failing_rel)
        self._assert_source_and_snapshot_unchanged(
            before_failure, f"{stage} 故障后",
        )

        # 解除故障（普通公开命令，不安装任何补丁）：对同一快照与同一目标
        # 再次恢复必须完全成功。
        before_retry = (
            capture_tree(self.source), capture_tree(self.snapshot),
        )
        retry = run_cmd(["restore", str(self.snapshot), str(self.dest)])
        stdout = retry.stdout.decode("utf-8", errors="replace")
        stderr = retry.stderr.decode("utf-8", errors="replace")
        context = (
            f"故障解除后的重试（场景 {stage}）\n"
            f"退出码={retry.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(retry.returncode, 0, f"重试应返回 0\n{context}")
        self.assertEqual(stderr, "", f"重试成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.dest.resolve()), stdout,
            f"标准输出应包含恢复目标绝对路径\n{context}",
        )
        self.assertIn(
            "已恢复文件数: 2", stdout,
            f"标准输出应包含文件数 2\n{context}",
        )

        # 目标仅包含两个文件及必要目录，内容逐字节与快照/源一致。
        restored_files = file_tree(self.dest)
        self.assertEqual(
            set(restored_files), set(SOURCE_FILES),
            f"重试结果应恰好包含两个文件\n{context}",
        )
        self.assertEqual(
            capture_tree(self.dest),
            {
                A_REL: ("file", A_BYTES),
                NESTED_DIR: ("dir", None),
                B_REL: ("file", B_BYTES),
            },
            f"重试结果目录树（路径/类型/字节）与预期不符\n{context}",
        )
        self.assertEqual(
            restored_files[A_REL], A_BYTES,
            f"a.txt 重试后字节不正确\n{context}",
        )
        self.assertEqual(
            restored_files[B_REL], B_BYTES,
            f"nested/b.bin 重试后字节不正确\n{context}",
        )

        # 标记文件仍在，源目录与快照相对重试前同样不变。
        self.assertEqual(
            (self.work / MARKER_REL).read_bytes(), MARKER_BYTES,
            f"成功重试后标记文件被改动\n{context}",
        )
        self._assert_source_and_snapshot_unchanged(
            before_retry, f"{stage} 重试后",
        )

    def test_copy_fails_on_first_file_after_partial_bytes(self):
        """首个文件 a.txt 写入部分字节后失败：目标整体清理，重试成功。"""
        self._run_fault_scenario("first", A_REL)

    def test_copy_fails_on_nested_binary_after_first_file_completed(self):
        """a.txt 完整恢复后，nested/b.bin 写入部分字节失败：清理含已完成
        文件与嵌套目录在内的整个目标，随后重试成功。"""
        self._run_fault_scenario("second", B_REL)


if __name__ == "__main__":
    unittest.main()
