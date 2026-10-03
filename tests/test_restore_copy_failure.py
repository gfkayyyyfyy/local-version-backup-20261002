#!/usr/bin/env python3
"""restore 复制阶段发生 I/O 错误后清理行为的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST

只观察退出码、标准输出、标准错误与文件原始字节，不调用 backup.py 的
任何内部函数，也不修改产品代码、命令、快照格式或错误语义。仅依赖
Python 3 标准库；全部源目录、快照与恢复目标在独立临时目录中运行时
准备，用例结束自动清理，不读写用户现有目录，不依赖真实磁盘故障、
网络、第三方库或管理员权限。

夹具源数据由不带 --checksum 的公开 backup 命令生成版本 1 快照：

- ``a.txt``：UTF-8 文本 ``old\\n``（4 字节）；
- ``nested/b.bin``：4 字节二进制 ``0x00 0xFF 0x10 0x00``。

故障注入方式：临时目录中放置一个 ``sitecustomize.py``，通过
PYTHONPATH 让 restore 子进程在启动时自动导入。它只包住 ``builtins.open``：
当恢复流程以二进制写模式打开“本次配置的目标相对路径”时，首个写入块
只实际写入前若干字节（部分写入），落盘后先确认目标文件确实存在且
已有非空内容，再抛出原因文本为“演示复制错误”的 OSError；其余文件
读写完全透传。故障只影响该次复制，清单与引用数据始终有效，备份工具
既有的复制失败清理流程（删除本次新建的恢复目录）原样实际执行。

覆盖两个确定的失败场景：

1. 首个文件 ``a.txt`` 已写入部分字节时失败：返回码 2，标准输出为空，
   标准错误包含“恢复文件失败”、相对路径 ``a.txt`` 与“演示复制错误”，
   恢复目标（含部分文件）整体被清理。
2. ``a.txt`` 已完整恢复、``nested/b.bin`` 已写入部分字节时失败：
   注入器在抛错前显式核对目标内 ``a.txt`` 已存在且字节完整，返回码与
   报错约定同上，恢复目标（含已完成文件、部分文件与 nested 目录）整体
   被清理。

两种场景下，恢复目标共同父目录中的独立标记文件路径与字节保持不变，
源目录与快照的相对路径、目录结构及全部文件字节与调用前一致。解除故障
后对同一快照、同一目标再次恢复：返回码 0、标准错误为空，标准输出包含
目标绝对路径与文件数 2，目标仅含两个文件及必要目录且字节逐字节一致。
清理在正常可写的临时目录中完成，本测试不覆盖删除操作本身再次失败的情况。
"""

import binascii
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

# 夹具中的两个相对路径（斜杠分隔）。
A_REL = "a.txt"
B_DIR_REL = "nested"
B_REL = "nested/b.bin"

# 源目录固定内容：UTF-8 文本与含零字节的嵌套二进制。
A_BYTES = b"old\n"
B_BYTES = bytes([0x00, 0xFF, 0x10, 0x00])
SOURCE_FILES = {
    A_REL: A_BYTES,
    B_REL: B_BYTES,
}

# 恢复目标共同父目录中的独立标记文件（字节含零字节与 0xFF）。
MARKER_REL = "parent-marker.bin"
MARKER_BYTES = b"marker outside restore target \x00\xff\n"

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
ERROR_PREFIX = "错误"
REASON_COPY_FAILED = "恢复文件失败"
FAULT_MESSAGE = "演示复制错误"

# 每个场景首个写入块实际落盘的字节数（均小于文件总长，确保是部分写入）。
PARTIAL_BYTES = 2

# 注入 restore 子进程的 sitecustomize.py 源码。
# 仅当环境变量 FAULT_COPY_REL 指定了目标相对路径时才武装故障；
# 对其他任何 open 调用（含快照数据的只读打开、文本模式打开）完全透传。
SITECUSTOMIZE_SOURCE = r'''import builtins
import os

_baseline_open = builtins.open

_rel = os.environ.get("FAULT_COPY_REL")
_after = int(os.environ.get("FAULT_COPY_AFTER_BYTES", "1"))
_message = os.environ.get("FAULT_COPY_MESSAGE", "演示复制错误")
_prereq_rel = os.environ.get("FAULT_COPY_PREREQ_REL")
_prereq_hex = os.environ.get("FAULT_COPY_PREREQ_HEX", "")
_suffix = os.sep + os.path.join(*_rel.split("/")) if _rel else None


class _FaultingBinaryWriter:
    """包装二进制写文件：首个 write 只落盘部分字节，确认非空后抛 OSError。"""

    def __init__(self, raw, path):
        self._raw = raw
        self._path = path
        self._fired = False

    def write(self, data):
        if self._fired:
            return self._raw.write(data)
        self._fired = True
        if not isinstance(data, (bytes, bytearray)):
            raise AssertionError("故障注入前提：首个写入块应为字节串")
        if len(data) <= _after:
            raise AssertionError(
                "故障注入前提：首个写入块仅 %d 字节，无法在写入 %d 字节后"
                "仍保留非空的部分内容" % (len(data), _after)
            )
        written = self._raw.write(bytes(data[:_after]))
        self._raw.flush()
        size = os.fstat(self._raw.fileno()).st_size
        if written <= 0 or not os.path.isfile(self._path) or size <= 0:
            raise AssertionError(
                "故障注入前提：触发失败前目标文件不存在或尚无任何字节落盘"
            )
        if _prereq_rel:
            root = self._path[: -len(_suffix)]
            prereq = os.path.join(root, *_prereq_rel.split("/"))
            with _baseline_open(prereq, "rb") as handle:
                actual = handle.read()
            if actual != bytes.fromhex(_prereq_hex):
                raise AssertionError(
                    "故障注入前提：前置已完成文件缺失或字节不完整: "
                    + _prereq_rel
                )
        raise OSError(_message)

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)


def _patched_open(file, mode="r", *args, **kwargs):
    if _suffix is not None and "w" in mode and "b" in mode:
        path = os.path.abspath(os.fspath(file))
        # 快照数据以 "rb" 只读打开，不会被匹配；此处只可能是恢复目标写入。
        if path.endswith(_suffix):
            return _FaultingBinaryWriter(
                _baseline_open(file, mode, *args, **kwargs), path
            )
    return _baseline_open(file, mode, *args, **kwargs)


builtins.open = _patched_open
'''


def run_cmd(argv, env=None):
    """通过公开命令行执行 backup.py，返回 CompletedProcess。"""
    return subprocess.run(
        [sys.executable, str(BACKUP_SCRIPT), *argv],
        cwd=str(ROOT),
        env=env if env is not None else CHILD_ENV,
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

    类型为 "dir" / "file" / "symlink"；普通文件记录完整字节，符号链接
    记录链接目标字符串（不跟随链接，也不进入链接指向的目录）。
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


def file_tree(root):
    """递归记录普通文件：相对路径 -> 字节。"""
    return {
        key: value[1]
        for key, value in capture_tree(root).items()
        if value[0] == "file"
    }


class RestoreCopyFailureTests(unittest.TestCase):
    """复制阶段 I/O 失败后清理本次新建目标，且故障解除后可正常重新恢复。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照、恢复目标与标记文件的共同父目录。
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-copy-fail-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"
        self.dest = self.work / "restored"
        self.marker = self.work / MARKER_REL

        write_files(self.source, SOURCE_FILES)
        self.marker.write_bytes(MARKER_BYTES)

        # 夹具快照必须由公开 backup 命令（不带 --checksum）生成。
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 夹具前提：版本 1、恰好两个文件且条目不带摘要字段。
        import json
        manifest = json.loads(
            (self.snapshot / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["version"], 1, "测试前提：清单版本应为 1")
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            [A_REL, B_REL],
            "测试前提：清单应恰好且按序包含 a.txt 与 nested/b.bin",
        )
        for item in manifest["files"]:
            self.assertNotIn(
                "sha256", item,
                "测试前提：不带 --checksum 的备份条目不应含摘要字段",
            )

        # 恢复目标在调用前不存在，且与快照同级（位于快照之外）。
        self.assertFalse(os.path.lexists(self.dest))

        # 仅供子进程导入的故障注入目录，也在工作区内（比对时应保持不变）。
        self.fault_dir = self.work / "_fault_injection"
        self.fault_dir.mkdir()
        (self.fault_dir / "sitecustomize.py").write_text(
            SITECUSTOMIZE_SOURCE, encoding="utf-8"
        )

    # ---- 通用流程 ----

    def run_restore_with_fault(self, target_rel, prereq_rel=None,
                               prereq_bytes=None):
        """以注入了可控复制故障的环境执行真实 restore 命令。"""
        env = dict(CHILD_ENV)
        # 导入注入用 sitecustomize 时不在工作区生成 __pycache__，
        # 保证调用前后共同父目录目录树严格可比。
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        existing_pythonpath = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join(
            [str(self.fault_dir), existing_pythonpath]
        ).strip(os.pathsep)
        env["FAULT_COPY_REL"] = target_rel
        env["FAULT_COPY_AFTER_BYTES"] = str(PARTIAL_BYTES)
        env["FAULT_COPY_MESSAGE"] = FAULT_MESSAGE
        if prereq_rel is not None:
            env["FAULT_COPY_PREREQ_REL"] = prereq_rel
            env["FAULT_COPY_PREREQ_HEX"] = binascii.hexlify(
                prereq_bytes
            ).decode("ascii")
        return run_cmd(
            ["restore", str(self.snapshot), str(self.dest)], env=env
        )

    def assert_failure_cleaned_up(self, target_rel, label):
        """失败场景公共断言：返回码/输出/报错、目标清理、父目录与地面不变。"""
        self.assertFalse(
            os.path.lexists(self.dest),
            f"用例前提：恢复目标必须事先不存在（{label}）",
        )
        # 调用前一瞬间拍摄整个共同父目录（含源、快照、标记文件、注入脚本）。
        work_before = capture_tree(self.work)

        proc = self.run_restore_with_fault(
            target_rel,
            prereq_rel=A_REL if target_rel == B_REL else None,
            prereq_bytes=A_BYTES if target_rel == B_REL else None,
        )
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n故障目标: {target_rel}\n目标 DEST: {self.dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"复制失败应返回 2\n{context}")
        self.assertEqual(stdout, "", f"失败后标准输出应为空\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        self.assertIn(
            REASON_COPY_FAILED, stderr,
            f"标准错误缺少“{REASON_COPY_FAILED}”\n{context}",
        )
        self.assertIn(
            target_rel, stderr,
            f"标准错误缺少失败文件的相对路径 {target_rel}\n{context}",
        )
        self.assertIn(
            FAULT_MESSAGE, stderr,
            f"标准错误缺少底层 I/O 原因“{FAULT_MESSAGE}”\n{context}",
        )

        # 恢复目标整体不存在：已完成文件、部分文件与嵌套目录全部移除。
        self.assertFalse(
            os.path.lexists(self.dest),
            f"失败后恢复目标仍被保留: {self.dest}\n{context}",
        )
        self.assertFalse(
            os.path.lexists(self.dest / A_REL),
            f"失败后已写入/已完成的 a.txt 未被清理\n{context}",
        )
        self.assertFalse(
            os.path.lexists(self.dest / B_DIR_REL / "b.bin"),
            f"失败后部分写入的嵌套二进制未被清理\n{context}",
        )
        self.assertFalse(
            os.path.lexists(self.dest / B_DIR_REL),
            f"失败后嵌套目录未被一并清理\n{context}",
        )

        # 共同父目录与调用前逐字节一致：标记保留、无源/快照改动、无新增残留。
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"失败后共同父目录出现新增、缺失或字节改动\n{context}",
        )
        self.assertTrue(
            self.marker.is_file(),
            f"父目录中的独立标记文件丢失\n{context}",
        )
        self.assertEqual(
            self.marker.read_bytes(), MARKER_BYTES,
            f"父目录中的独立标记文件字节被改动\n{context}",
        )
        return context

    def assert_rerun_succeeds(self, context_label):
        """解除故障后对同一快照、同一目标再次恢复：完整成功且字节准确。"""
        # 失败清理已保证目标不存在；再次显式确认后走不带故障的真实命令。
        self.assertFalse(os.path.lexists(self.dest))
        proc = run_cmd(["restore", str(self.snapshot), str(self.dest)])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例（故障解除后重跑）: {context_label}\n目标 DEST: {self.dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"重跑应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
        self.assertIn(
            str(self.dest.resolve()), stdout,
            f"标准输出应包含恢复目标绝对路径\n{context}",
        )
        self.assertIn(
            "已恢复文件数: 2", stdout,
            f"标准输出应包含文件数 2\n{context}",
        )

        # 目标仅含两个文件及必要目录，相对路径集合与字节逐一等于源数据。
        self.assertEqual(
            file_tree(self.dest), SOURCE_FILES,
            f"重跑后恢复结果的路径集合或字节与源数据不一致\n{context}",
        )
        self.assertEqual(
            (self.dest / A_REL).read_bytes(), A_BYTES,
            f"a.txt 未逐字节恢复为 old\\n\n{context}",
        )
        b_bin = self.dest / B_DIR_REL / "b.bin"
        self.assertEqual(
            b_bin.read_bytes(), B_BYTES,
            f"nested/b.bin 未按 0x00 0xFF 0x10 0x00 逐字节恢复\n{context}",
        )

        # 重跑后标记文件、源目录与快照依旧不变。
        self.assertEqual(
            self.marker.read_bytes(), MARKER_BYTES,
            f"重跑后父目录标记文件被改动\n{context}",
        )
        self.assertEqual(
            file_tree(self.source), SOURCE_FILES,
            f"重跑后源目录内容发生变化\n{context}",
        )
        snapshot_files = file_tree(self.snapshot / "data")
        self.assertEqual(
            snapshot_files, SOURCE_FILES,
            f"重跑后快照 data 内容发生变化\n{context}",
        )

    # ---- 场景 1：首个文件写入部分字节时失败 ----

    def test_fail_during_first_file_then_cleanup_and_rerun_ok(self):
        """a.txt 写入部分字节即失败：目标整体清理；解除故障后重跑成功。"""
        context = self.assert_failure_cleaned_up(
            A_REL, "首个文件 a.txt 复制中途 I/O 错误"
        )
        self.assert_rerun_succeeds("首个文件复制失败后")
        # context 仅用于失败时提供现场，保留引用便于阅读断言顺序。
        self.assertIn(A_REL, context)

    # ---- 场景 2：a.txt 已完整、嵌套二进制写入部分字节时失败 ----

    def test_fail_during_nested_file_after_first_completed_then_rerun_ok(self):
        """a.txt 已完整恢复、nested/b.bin 部分写入时失败：连同已完成文件与
        嵌套目录整体清理；解除故障后重跑成功。"""
        # 注入器在抛出 OSError 前会核对目标内 a.txt 已存在且字节完整，
        # 因此本场景“首个文件已完整恢复”由子进程侧确定性保证。
        self.assert_failure_cleaned_up(
            B_REL, "a.txt 已完成、nested/b.bin 复制中途 I/O 错误"
        )
        self.assert_rerun_succeeds("嵌套二进制复制失败后")


if __name__ == "__main__":
    unittest.main()
