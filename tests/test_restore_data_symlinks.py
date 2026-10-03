#!/usr/bin/env python3
"""restore 拒绝快照数据中符号链接的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH]

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照、恢复目标与快照外目录均在
独立临时目录中运行时准备，用例结束自动清理，不读写用户现有目录，也
不依赖网络或额外安装包。

夹具一律先用公开 backup 入口（不带 --checksum）生成版本 1 无摘要快照，
源目录包含：

- ``good.txt``：十六进制字节 ``68 69 0a``（即 ``hi\\n``）；
- ``嵌套 目录/数据.bin``：十六进制字节 ``00 ff 10``，含零字节的二进制。

备份产生的清单中 ``good.txt`` 自然排在 ``嵌套 目录/数据.bin`` 之前
（用例显式断言这一顺序，保证问题条目之前确实存在一个有效条目）。

随后在快照的 data 目录中只引入一种符号链接条件，得到两份独立的问题快照：

1. 文件链接：将 data 内的 ``嵌套 目录/数据.bin`` 普通文件替换为指向
   data 内 ``good.txt`` 的文件符号链接（相对链接，真实目标保留）。
2. 父目录越界链接：将 data 内的嵌套父目录 ``嵌套 目录`` 替换为指向
   快照外普通目录的符号链接；外部目录位于同一临时工作区，其中含同名
   普通文件 ``数据.bin``。

每份问题快照分别执行：

- 不带 --file 的全量恢复；
- 仅 ``--file good.txt`` 的选择恢复（清单校验始终覆盖整个快照，未选中
  的问题条目同样必须导致整体拒绝）。

四种失败调用的共同约定：退出码 2、标准输出为空；文件链接用例的标准
错误包含“清单引用的数据是符号链接”，父目录越界用例包含“清单路径解析
后越出数据目录”，两者都指出 ``嵌套 目录/数据.bin``；恢复目标始终不
存在，源目录、快照（含链接本身的类型与指向）、快照外目标的路径、类型
与文件字节均保持调用前状态，工作区不新增任何文件或目录。状态比较基于
lstat（不跟随符号链接）且只记录类型、链接目标与文件字节，因此纯粹由
读取引起的访问时间变化不计为修改。

另设一个不含任何链接的正常对照：使用同样的文本、嵌套中文路径与含零
字节二进制文件，全量恢复到全新目录后退出码 0、标准错误为空，成功输出
包含目标绝对路径与“已恢复文件数: 2”，恢复字节与快照逐字节一致。

若当前平台不支持创建符号链接或权限不足，仅四个符号链接用例明确标记为
跳过并注明原因；正常对照始终执行。产品命令自身的失败一律按断言失败
处理，不作为跳过理由。既有 backup、摘要校验与只读 verify 的行为不在
本文件中改动或重新约定。
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

# 文件链接用例中链接的相对指向：data/嵌套 目录/数据.bin -> data/good.txt。
FILE_LINK_TARGET = "../good.txt"

# 父目录越界用例中，快照外同名普通文件的固定字节，与快照内字节不同。
OUTSIDE_BIN_BYTES = b"outside snapshot workspace, do not touch\n"
OUTSIDE_DIR_NAME = "快照外目录"

# 标准错误中的既有公开拒绝原因。
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
    记录链接目标字符串（基于 lstat，不跟随链接，os.walk 也不进入链接
    指向的目录），因此读取造成的访问时间变化不会出现在比较结果中。
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
    """递归记录普通文件：相对路径 -> 字节（不记录符号链接）。"""
    return {
        key: value[1]
        for key, value in capture_tree(root).items()
        if value[0] == "file"
    }


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


def prepare_base_snapshot(work):
    """准备源目录并用公开 backup 入口生成无摘要版本 1 快照。

    返回 (source, snapshot)；夹具失败时抛 RuntimeError，使测试明确出错
    而非把夹具问题误判为产品行为。
    """
    work = Path(work)
    source = work / "source"
    snapshot = work / "snapshot"
    write_files(source, BACKUP_FILES)

    proc = run_cmd(["backup", str(source), str(snapshot)])
    if proc.returncode != 0 or not snapshot.is_dir():
        raise RuntimeError(
            "测试夹具：基线快照创建失败\n"
            f"exit={proc.returncode}\n"
            f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
        )

    # 显式固定夹具前提：问题清单把 good.txt 放在链接条目前面，且不带摘要。
    manifest = json.loads((snapshot / "manifest.json").read_text("utf-8"))
    paths = [entry.get("path") for entry in manifest.get("files", [])]
    if paths != sorted(BACKUP_FILES) or paths.index(GOOD_REL) >= paths.index(BIN_REL):
        raise RuntimeError(f"测试夹具：清单条目顺序不符合预期: {paths}")
    for entry in manifest.get("files", []):
        if set(entry.keys()) != {"path"}:
            raise RuntimeError(f"测试夹具：基线快照不应携带摘要字段: {entry}")

    return source, snapshot


class RestoreDataSymlinkTests(unittest.TestCase):
    """restore 对快照 data 中符号链接（文件链接与父目录越界链接）的拒绝。"""

    # ---- 夹具：两种问题快照 ----

    def _prepare_file_link_fixture(self, work):
        """将 data 内嵌套文件替换为指向 data 内 good.txt 的文件符号链接。"""
        source, snapshot = prepare_base_snapshot(work)

        data_good = snapshot / "data" / GOOD_REL
        self.assertTrue(data_good.is_file(), "夹具：data/good.txt 应为普通文件")

        link = snapshot / "data" / BIN_REL
        self.assertTrue(link.is_file(), "夹具：被替换前嵌套条目应为普通文件")
        link.unlink()
        os.symlink(FILE_LINK_TARGET, link)

        # 真实目标保留：链接经解析后仍落在 data 内的普通文件 good.txt 上。
        self.assertTrue(link.is_symlink(), "夹具：文件符号链接未创建成功")
        self.assertEqual(os.readlink(link), FILE_LINK_TARGET)
        resolved = link.resolve(strict=True)
        self.assertEqual(resolved, data_good.resolve(strict=True))
        self.assertEqual(resolved.read_bytes(), GOOD_BYTES)
        return source, snapshot, link, FILE_LINK_TARGET

    def _prepare_parent_dir_link_fixture(self, work):
        """将嵌套父目录替换为指向快照外目录的链接，外部含同名普通文件。"""
        source, snapshot = prepare_base_snapshot(work)

        outside_dir = Path(work) / OUTSIDE_DIR_NAME
        outside_dir.mkdir()
        outside_file = outside_dir / "数据.bin"
        outside_file.write_bytes(OUTSIDE_BIN_BYTES)

        nested_dir = snapshot / "data" / NESTED_DIR_REL
        self.assertTrue(nested_dir.is_dir(), "夹具：被替换前嵌套父目录应为目录")
        shutil.rmtree(nested_dir)
        os.symlink(outside_dir, nested_dir, target_is_directory=True)

        # 链接经解析后确实指向快照外目录中的同名普通文件。
        self.assertTrue(nested_dir.is_symlink(), "夹具：目录符号链接未创建成功")
        self.assertEqual(os.readlink(nested_dir), str(outside_dir))
        through_link = nested_dir / "数据.bin"
        self.assertTrue(through_link.is_file())
        self.assertEqual(through_link.read_bytes(), OUTSIDE_BIN_BYTES)
        return source, snapshot, nested_dir, outside_dir

    # ---- 公共断言 ----

    def _assert_restore_rejected(
        self, label, work, snapshot, dest, expected_reason, extra_args=(),
    ):
        """执行一次恢复并验证“创建恢复目标之前整体拒绝”的全部约定。

        夹具状态在每次恢复调用前即时记录。
        """
        self.assertFalse(
            os.path.lexists(dest), f"用例 {label}：恢复目标应事先不存在",
        )
        work_before = capture_tree(work)

        proc = run_cmd(["restore", str(snapshot), str(dest), *extra_args])
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        self.assertIn(
            expected_reason, stderr,
            f"标准错误缺少拒绝原因“{expected_reason}”\n{context}",
        )
        self.assertIn(
            BIN_REL, stderr,
            f"标准错误应指出嵌套路径 {BIN_REL}\n{context}",
        )
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )

        # 整个临时工作区逐路径、逐类型、逐字节一致（不跟随链接、
        # 不比较访问时间）：源目录、快照、链接本身、外部目标均未改变，
        # 也没有任何新增文件或目录。
        self.assertEqual(
            capture_tree(work), work_before,
            f"失败后临时工作区出现新增、改动或删除\n{context}",
        )
        return context

    # ---- 用例 1：data 内文件符号链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_file_symlink_rejected_full_restore(self):
        """文件链接快照全量恢复：在创建目标前拒绝，报错指出嵌套路径。"""
        with tempfile.TemporaryDirectory(prefix="restore-data-symlink-") as work:
            source, snapshot, link, link_target = self._prepare_file_link_fixture(work)
            dest = Path(work) / "restored"

            self._assert_restore_rejected(
                "文件链接-全量恢复", work, snapshot, dest, REASON_FILE_SYMLINK,
            )

            # 显式复核：链接本身、真实目标与源目录均保持原样。
            self.assertTrue(link.is_symlink(), "调用后文件链接不再是符号链接")
            self.assertEqual(os.readlink(link), link_target, "文件链接指向被改动")
            self.assertEqual(
                (snapshot / "data" / GOOD_REL).read_bytes(), GOOD_BYTES,
                "链接指向的真实目标字节被改动",
            )
            self.assertEqual(file_tree(source), BACKUP_FILES, "源目录内容发生变化")

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_file_symlink_rejected_selecting_good_file(self):
        """文件链接快照仅选择 good.txt：未选中的问题条目仍致整体拒绝。"""
        with tempfile.TemporaryDirectory(prefix="restore-data-symlink-") as work:
            source, snapshot, link, link_target = self._prepare_file_link_fixture(work)
            dest = Path(work) / "restored"

            self._assert_restore_rejected(
                "文件链接-仅选择good.txt", work, snapshot, dest,
                REASON_FILE_SYMLINK, extra_args=("--file", GOOD_REL),
            )

            self.assertTrue(link.is_symlink(), "调用后文件链接不再是符号链接")
            self.assertEqual(os.readlink(link), link_target, "文件链接指向被改动")
            self.assertEqual(
                (snapshot / "data" / GOOD_REL).read_bytes(), GOOD_BYTES,
                "链接指向的真实目标字节被改动",
            )
            self.assertEqual(file_tree(source), BACKUP_FILES, "源目录内容发生变化")

    # ---- 用例 2：嵌套父目录指向快照外 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_parent_dir_symlink_rejected_full_restore(self):
        """父目录越界链接快照全量恢复：报“越出数据目录”并指出嵌套路径。"""
        with tempfile.TemporaryDirectory(prefix="restore-data-symlink-") as work:
            source, snapshot, link, outside_dir = \
                self._prepare_parent_dir_link_fixture(work)
            dest = Path(work) / "restored"

            self._assert_restore_rejected(
                "父目录越界-全量恢复", work, snapshot, dest, REASON_PATH_OUTSIDE,
            )

            # 显式复核：目录链接、快照外目标与源目录均保持原样。
            self.assertTrue(link.is_symlink(), "调用后父目录链接不再是符号链接")
            self.assertEqual(os.readlink(link), str(outside_dir))
            self.assertTrue(outside_dir.is_dir(), "快照外目标目录类型发生变化")
            self.assertEqual(
                (outside_dir / "数据.bin").read_bytes(), OUTSIDE_BIN_BYTES,
                "快照外同名普通文件字节被改动",
            )
            self.assertEqual(file_tree(source), BACKUP_FILES, "源目录内容发生变化")

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_parent_dir_symlink_rejected_selecting_good_file(self):
        """父目录越界快照仅选择 good.txt：清单整体校验，仍被拒绝。"""
        with tempfile.TemporaryDirectory(prefix="restore-data-symlink-") as work:
            source, snapshot, link, outside_dir = \
                self._prepare_parent_dir_link_fixture(work)
            dest = Path(work) / "restored"

            self._assert_restore_rejected(
                "父目录越界-仅选择good.txt", work, snapshot, dest,
                REASON_PATH_OUTSIDE, extra_args=("--file", GOOD_REL),
            )

            self.assertTrue(link.is_symlink(), "调用后父目录链接不再是符号链接")
            self.assertEqual(os.readlink(link), str(outside_dir))
            self.assertTrue(outside_dir.is_dir(), "快照外目标目录类型发生变化")
            self.assertEqual(
                (outside_dir / "数据.bin").read_bytes(), OUTSIDE_BIN_BYTES,
                "快照外同名普通文件字节被改动",
            )
            self.assertEqual(file_tree(source), BACKUP_FILES, "源目录内容发生变化")

    # ---- 无链接正常对照（始终执行，不随符号链接能力跳过）----

    def test_clean_snapshot_restores_successfully(self):
        """无链接快照（文本 + 嵌套中文路径 + 含零字节二进制）恢复成功。"""
        with tempfile.TemporaryDirectory(prefix="restore-data-symlink-") as work:
            source, snapshot = prepare_base_snapshot(work)
            dest = Path(work) / "restored"

            # 对照夹具中确实不存在任何符号链接。
            fixture_kinds = {
                kind for kind, _ in capture_tree(work).values()
            }
            self.assertNotIn("symlink", fixture_kinds, "正常对照不应包含符号链接")
            source_before = capture_tree(source)

            proc = run_cmd(["restore", str(snapshot), str(dest)])
            stdout = proc.stdout.decode("utf-8", errors="replace")
            stderr = proc.stderr.decode("utf-8", errors="replace")
            context = (
                f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
            )

            self.assertEqual(proc.returncode, 0, f"正常对照应返回 0\n{context}")
            self.assertEqual(stderr, "", f"成功时标准错误应为空\n{context}")
            self.assertIn(
                str(dest.resolve()), stdout,
                f"标准输出应包含恢复目标绝对路径\n{context}",
            )
            self.assertIn(
                "已恢复文件数: 2", stdout,
                f"标准输出应包含文件数 2\n{context}",
            )

            # 恢复结果的相对路径集合与字节与快照 data 完全一致。
            expected = file_tree(snapshot / "data")
            restored = file_tree(dest)
            self.assertEqual(
                set(restored), set(expected),
                "恢复后的相对路径集合与快照不一致",
            )
            for rel, data in expected.items():
                self.assertEqual(
                    restored[rel], data,
                    f"恢复字节与快照不一致: {rel}",
                )

            # 显式固定公开语义：文本字节与含零字节二进制逐字节保留。
            self.assertEqual(
                (dest / GOOD_REL).read_bytes(), bytes.fromhex("68 69 0a"),
            )
            bin_bytes = (dest / NESTED_DIR_REL / "数据.bin").read_bytes()
            self.assertEqual(bin_bytes, bytes.fromhex("00 ff 10"))
            self.assertIn(0, bin_bytes, "二进制文件应保留零字节 0x00")

            # 源目录保持原样（类型、字节一致；不比较访问时间）。
            self.assertEqual(
                capture_tree(source), source_before,
                "恢复成功后源目录内容发生变化",
            )


if __name__ == "__main__":
    unittest.main()
