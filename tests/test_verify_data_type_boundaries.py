#!/usr/bin/env python3
"""verify 对清单引用数据的文件类型与路径边界的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py verify SNAPSHOT

只观察退出码、标准输出、标准错误与文件系统状态，不调用 backup.py 的
任何内部校验函数。仅依赖 Python 3 标准库；全部源目录、快照与快照外
目录都在独立临时目录中运行时准备，用例结束自动清理，不读写用户现有
目录，不依赖网络、第三方库或管理员权限。

夹具一律先用公开 backup 入口（不带 --checksum）生成版本 1 无摘要快照，
源目录包含：

- ``good.txt``：十六进制字节 ``68 69 0a``（即 ``hi\\n``）；
- ``嵌套 目录/数据.bin``：十六进制字节 ``00 ff 10``，含零字节的二进制。

备份产生的清单中 ``good.txt`` 条目自然排在 ``嵌套 目录/数据.bin`` 条目
之前（用例显式断言这一顺序，保证问题条目之前确实存在一个有效条目，
从而验证前面的有效条目检查通过也不会产生任何局部成功输出）。

覆盖约定：

1. 正常对照（不含任何链接与类型异常）：verify 退出码 0、标准错误为空、
   标准输出只有一个可解析 JSON 对象，其中 snapshot 为快照解析后的绝对
   路径，files/verified/unchecked 分别为 2/0/2。
2. 三份独立的问题快照（清单保持原样，每份只引入一种数据层问题）：
   - 普通目录拒绝：data 内的 ``嵌套 目录/数据.bin`` 普通文件被替换为
     同名空目录；退出码 2、标准输出完全为空，标准错误包含
     “清单引用的数据不是普通文件”与嵌套文件的完整相对路径；
   - 文件符号链接：该嵌套文件被替换为指向 data 内 ``good.txt`` 的文件
     符号链接（相对链接，真实目标保留）；标准错误包含
     “清单引用的数据是符号链接”与该相对路径；
   - 父目录越界链接：嵌套父目录 ``嵌套 目录`` 被替换为指向快照外目录
     的符号链接，外部目录位于同一临时工作区且含真实存在的同名普通
     文件 ``数据.bin``；标准错误包含“清单路径解析后越出数据目录”与
     该相对路径。
   三种情况标准输出都必须完全为空：前面的有效条目检查通过也不能产生
   局部成功输出。
3. 每次 verify 前后比较整个临时工作区：相对路径集合、条目类型、普通
   文件字节与符号链接指向一致——源目录、快照（含问题条目本身）与快照
   外目标保持原状，没有新增报告、恢复目录或其他文件。比较基准取在
   问题快照准备完成之后；比较基于 lstat（不跟随符号链接、不沿目录链接
   递归、不记录访问时间），纯粹由读取引起的访问时间变化不计为修改。

平台不支持创建符号链接或权限不足时，仅两个链接样例明确标记为跳过并
注明原因；普通目录拒绝与正常对照始终执行。产品命令自身的失败一律按
断言失败处理，不作为跳过理由。既有 backup、restore 与其余 verify
行为不在本文件中改动或重新约定。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKUP_SCRIPT = ROOT / "backup.py"

# 强制子进程按 UTF-8 输出，断言不依赖运行环境的区域设置。
CHILD_ENV = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

# 夹具固定内容：文本文件与嵌套中文目录下含零字节（0x00）的二进制文件。
GOOD_REL = "good.txt"
GOOD_BYTES = bytes.fromhex("68 69 0a")
NESTED_DIR_REL = "嵌套 目录"
BIN_REL = "嵌套 目录/数据.bin"
BIN_BYTES = bytes.fromhex("00 ff 10")

BACKUP_FILES = {
    GOOD_REL: GOOD_BYTES,
    BIN_REL: BIN_BYTES,
}

# 文件链接样例中链接的相对指向：data/嵌套 目录/数据.bin -> data/good.txt。
FILE_LINK_TARGET = "../good.txt"

# 父目录越界样例中，快照外同名普通文件的固定字节，与快照内字节不同。
OUTSIDE_BIN_BYTES = b"outside snapshot workspace, do not touch\n"
OUTSIDE_DIR_NAME = "快照外目录"

# 标准错误中的既有公开拒绝原因。
REASON_NOT_REGULAR = "清单引用的数据不是普通文件"
REASON_FILE_SYMLINK = "清单引用的数据是符号链接"
REASON_PATH_OUTSIDE = "清单路径解析后越出数据目录"


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

    类型为 "dir" / "file" / "symlink"；普通文件记录完整字节，符号链接
    记录链接目标字符串（基于 lstat，不跟随链接；os.walk 默认不沿目录
    链接递归），因此读取造成的访问时间变化不会出现在比较结果中。
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


def _symlink_supported():
    """探测当前环境是否允许创建符号链接（某些平台需特权）。"""
    with tempfile.TemporaryDirectory(prefix="symlink-probe-") as probe:
        try:
            os.symlink("target", Path(probe) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINK_SUPPORTED = _symlink_supported()
SYMLINK_SKIP_REASON = "当前环境不支持或无权限创建符号链接，跳过该用例"


class VerifyDataTypeBoundaryTests(unittest.TestCase):
    """verify 对清单引用数据的类型与路径边界的公开拒绝行为。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照、快照外目录及任何意外产物
        # 都受限于此，用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-data-type-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"

        write_files(self.source, BACKUP_FILES)

        # 用公开 backup 入口（不带 --checksum）生成无摘要版本 1 快照；
        # 夹具失败直接抛错，使测试明确出错而非误判为产品行为。
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 显式固定夹具前提：清单恰含两个无摘要条目，且有效条目 good.txt
        # 排在问题条目 嵌套 目录/数据.bin 之前。
        manifest = json.loads(
            (self.snapshot / "manifest.json").read_text("utf-8")
        )
        if manifest.get("version") != 1:
            raise RuntimeError(f"测试夹具：清单版本应为 1: {manifest}")
        entries = manifest.get("files")
        if not isinstance(entries, list):
            raise RuntimeError(f"测试夹具：清单 files 应为数组: {manifest}")
        paths = [entry.get("path") for entry in entries]
        if paths != sorted(BACKUP_FILES) or paths.index(GOOD_REL) >= paths.index(BIN_REL):
            raise RuntimeError(f"测试夹具：清单条目顺序不符合预期: {paths}")
        for entry in entries:
            if set(entry.keys()) != {"path"}:
                raise RuntimeError(f"测试夹具：基线快照不应携带摘要字段: {entry}")

    # ---- 公共断言 ----

    def _assert_workspace_intact(self, work_before, context):
        """verify 后整个临时工作区与基准逐路径、逐类型、逐字节一致。"""
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"verify 后临时工作区出现新增、改动或删除"
            f"（报告、恢复目录等）\n{context}",
        )

    def _assert_verify_rejected(self, label, expected_reason):
        """失败用例公共流程。

        基准在问题快照准备完成之后记录：退出码 2、标准输出完全为空
        （有效条目检查通过也不产生局部成功输出）、标准错误包含拒绝原因
        与嵌套文件完整相对路径，且整个工作区与基准一致。
        """
        work_before = capture_tree(self.work)

        proc = run_cmd(["verify", str(self.snapshot)])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {self.snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            stdout, "",
            f"失败后标准输出应完全为空（不得有局部成功输出）\n{context}",
        )
        self.assertIn(
            expected_reason, stderr,
            f"标准错误缺少拒绝原因“{expected_reason}”\n{context}",
        )
        self.assertIn(
            BIN_REL, stderr,
            f"标准错误应指出嵌套文件完整相对路径 {BIN_REL}\n{context}",
        )

        self._assert_workspace_intact(work_before, context)
        return context

    # ---- 正常对照（始终执行，不随符号链接能力跳过）----

    def test_clean_snapshot_verifies_successfully(self):
        """无异常快照：退出码 0、stderr 空、stdout 只有一个结果 JSON，
        files/verified/unchecked 为 2/0/2，工作区保持原状。"""
        # 对照夹具中确实不存在任何符号链接。
        fixture_kinds = {kind for kind, _ in capture_tree(self.work).values()}
        self.assertNotIn("symlink", fixture_kinds, "正常对照不应包含符号链接")

        work_before = capture_tree(self.work)

        proc = run_cmd(["verify", str(self.snapshot)])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: 正常对照\n快照 SNAPSHOT: {self.snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"正常对照应返回 0\n{context}")
        self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")

        # 标准输出有且仅有一个可解析的 JSON 对象（允许结尾换行）。
        nonempty_lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertEqual(
            len(nonempty_lines), 1,
            f"标准输出应只有一个 JSON 对象\n{context}",
        )
        result = json.loads(nonempty_lines[0])
        self.assertIsInstance(result, dict, f"输出应为 JSON 对象\n{context}")
        self.assertEqual(
            set(result),
            {"snapshot", "files", "verified", "unchecked"},
            f"输出 JSON 字段集合不符\n{context}",
        )
        self.assertEqual(
            result["snapshot"], str(self.snapshot.resolve()),
            f"snapshot 应为快照解析后的绝对路径\n{context}",
        )
        self.assertEqual(result["files"], 2, f"files 计数不符\n{context}")
        self.assertEqual(result["verified"], 0, f"verified 计数不符\n{context}")
        self.assertEqual(
            result["unchecked"], 2, f"unchecked 计数不符\n{context}",
        )

        self._assert_workspace_intact(work_before, context)

    # ---- 问题快照 1：嵌套文件被替换为空目录（始终执行）----

    def test_nested_file_replaced_by_empty_directory(self):
        """data 内嵌套文件被替换为同名空目录：报“不是普通文件”。"""
        data_bin = self.snapshot / "data" / BIN_REL
        self.assertTrue(data_bin.is_file(), "夹具：被替换前嵌套条目应为普通文件")
        self.assertEqual(data_bin.read_bytes(), BIN_BYTES)
        data_bin.unlink()
        data_bin.mkdir()
        # 夹具前提：替换后该路径是空目录，清单本身保持原样。
        self.assertTrue(data_bin.is_dir(), "夹具：空目录替换未生效")
        self.assertEqual(list(data_bin.iterdir()), [], "夹具：替换目录应为空")

        context = self._assert_verify_rejected(
            "嵌套文件替换为空目录", REASON_NOT_REGULAR,
        )

        # 显式复核：空目录、源目录与其余数据保持原状。
        self.assertTrue(data_bin.is_dir(), "调用后空目录类型发生变化")
        self.assertEqual(list(data_bin.iterdir()), [], "调用后空目录被写入内容")
        self.assertEqual(
            (self.snapshot / "data" / GOOD_REL).read_bytes(), GOOD_BYTES,
            "有效条目数据字节被改动",
        )
        self.assertEqual(
            capture_tree(self.source),
            {
                GOOD_REL: ("file", GOOD_BYTES),
                NESTED_DIR_REL: ("dir", None),
                BIN_REL: ("file", BIN_BYTES),
            },
            f"源目录内容发生变化\n{context}",
        )

    # ---- 问题快照 2：嵌套文件被替换为文件符号链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_nested_file_replaced_by_file_symlink(self):
        """嵌套文件被替换为指向 data 内 good.txt 的链接：报“是符号链接”。"""
        data_good = self.snapshot / "data" / GOOD_REL
        self.assertTrue(data_good.is_file(), "夹具：data/good.txt 应为普通文件")

        link = self.snapshot / "data" / BIN_REL
        self.assertTrue(link.is_file(), "夹具：被替换前嵌套条目应为普通文件")
        link.unlink()
        os.symlink(FILE_LINK_TARGET, link)

        # 真实目标保留：链接经解析后仍落在 data 内的普通文件 good.txt 上。
        self.assertTrue(link.is_symlink(), "夹具：文件符号链接未创建成功")
        self.assertEqual(os.readlink(link), FILE_LINK_TARGET)
        resolved = link.resolve(strict=True)
        self.assertEqual(resolved, data_good.resolve(strict=True))
        self.assertEqual(resolved.read_bytes(), GOOD_BYTES)

        self._assert_verify_rejected(
            "嵌套文件替换为文件符号链接", REASON_FILE_SYMLINK,
        )

        # 显式复核：链接本身、真实目标与源目录均保持原样。
        self.assertTrue(link.is_symlink(), "调用后文件链接不再是符号链接")
        self.assertEqual(os.readlink(link), FILE_LINK_TARGET, "文件链接指向被改动")
        self.assertEqual(
            data_good.read_bytes(), GOOD_BYTES,
            "链接指向的真实目标字节被改动",
        )

    # ---- 问题快照 3：嵌套父目录被替换为指向快照外的链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_nested_parent_replaced_by_outside_symlink(self):
        """嵌套父目录被替换为指向快照外目录的链接：报“越出数据目录”。"""
        outside_dir = self.work / OUTSIDE_DIR_NAME
        outside_dir.mkdir()
        outside_file = outside_dir / "数据.bin"
        outside_file.write_bytes(OUTSIDE_BIN_BYTES)

        nested_dir = self.snapshot / "data" / NESTED_DIR_REL
        self.assertTrue(nested_dir.is_dir(), "夹具：被替换前嵌套父目录应为目录")
        shutil.rmtree(nested_dir)
        os.symlink(outside_dir, nested_dir, target_is_directory=True)

        # 链接经解析后确实指向快照外目录中真实存在的同名普通文件。
        self.assertTrue(nested_dir.is_symlink(), "夹具：目录符号链接未创建成功")
        self.assertEqual(os.readlink(nested_dir), str(outside_dir))
        through_link = nested_dir / "数据.bin"
        self.assertTrue(through_link.is_file())
        self.assertEqual(through_link.read_bytes(), OUTSIDE_BIN_BYTES)

        self._assert_verify_rejected(
            "嵌套父目录替换为越界链接", REASON_PATH_OUTSIDE,
        )

        # 显式复核：目录链接、快照外目标与源目录均保持原样。
        self.assertTrue(nested_dir.is_symlink(), "调用后父目录链接不再是符号链接")
        self.assertEqual(os.readlink(nested_dir), str(outside_dir))
        self.assertTrue(outside_dir.is_dir(), "快照外目标目录类型发生变化")
        self.assertEqual(
            outside_file.read_bytes(), OUTSIDE_BIN_BYTES,
            "快照外同名普通文件字节被改动",
        )


if __name__ == "__main__":
    unittest.main()
