#!/usr/bin/env python3
"""restore 对快照数据中符号链接整体拒绝的可执行回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH]

只观察退出码、标准输出、标准错误与文件结果，不调用任何内部校验函数。
仅依赖 Python 3 标准库；全部源目录、快照、快照外目录与恢复目标都在
独立临时目录中运行时准备，用例结束自动清理，不读写用户现有目录，也
不依赖网络或额外安装包。

夹具由公开 backup 入口生成**无摘要**快照（不传 --checksum，清单条目
只有 path 字段），源目录包含：

- ``good.txt``：字节 ``68 69 0a``（即 ``hi\\n``）；
- ``嵌套 目录/数据.bin``：字节 ``00 ff 10``，嵌套中文路径下含零字节
  的二进制文件。

清单按路径排序，``good.txt`` 位于问题条目之前，用于证明问题条目失败
时先前条目也不会触发任何目标写入。备份完成后只改动快照数据，制造两种
违规快照，每种都分别执行全量恢复与仅 ``--file good.txt`` 的选择恢复：

1. 文件符号链接：将 ``data/嵌套 目录/数据.bin`` 替换为指向 data 内
   ``good.txt`` 的文件符号链接（真实目标保留）。标准错误包含
   “清单引用的数据是符号链接”，并指出 ``嵌套 目录/数据.bin``。
2. 父目录越界链接：将 ``data/嵌套 目录`` 整个替换为指向快照外目录的
   符号链接，外部目录位于同一临时工作区且含同名普通文件
   ``数据.bin``。标准错误包含“清单路径解析后越出数据目录”，并指出
   ``嵌套 目录/数据.bin``。

即使只选择 good.txt，清单也始终整单校验，因此两种链接用例的选择恢复
与全量恢复行为一致：退出码 2、标准输出为空、恢复目标在调用前后均不
存在。

每次恢复调用前记录整个临时工作区的目录树快照，调用后逐路径比较条目
类型、普通文件字节与符号链接指向：源目录、快照、链接本身与快照外
目标均不得改变，工作区不得新增任何文件或目录。比较基于 lstat、不
跟随链接、不比较访问时间，因此读取导致的 atime 变化不算修改。

另设无链接正常对照：使用同样的文本与嵌套中文路径、含零字节的二进制
文件，恢复到全新目录后退出码 0、标准错误为空，成功输出包含目标绝对
路径与“已恢复文件数: 2”，恢复字节与快照数据逐字节一致。

若平台不支持创建符号链接或当前权限不足，仅四个符号链接用例明确标记
跳过并注明原因；正常对照始终执行。产品命令自身的失败一律按断言失败
处理，不作为跳过理由。不修改产品源码、README 或版本 1 清单格式。
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

# 夹具中的两个相对路径（斜杠分隔，目录名含空格与中文）。
GOOD_REL = "good.txt"
GOOD_BYTES = bytes([0x68, 0x69, 0x0A])
NESTED_DIR = "嵌套 目录"
NESTED_REL = "嵌套 目录/数据.bin"
NESTED_BYTES = bytes([0x00, 0xFF, 0x10])

# 无摘要快照的源目录固定内容。
SOURCE_FILES = {
    GOOD_REL: GOOD_BYTES,
    NESTED_REL: NESTED_BYTES,
}

# 快照外目录（与快照同处一个临时工作区）及其同名普通文件内容。
EXTERNAL_DIRNAME = "snapshot-external"
EXTERNAL_FILE_BYTES = b"regular file outside snapshot, do not touch\n"

# 文件符号链接的相对目标：链接位于 data/嵌套 目录/数据.bin，
# ../good.txt 解析后为 data/good.txt（保留的真实目标）。
FILE_LINK_TARGET = "../good.txt"

# 成功摘要的公开输出标记（README：成功时打印目标绝对路径与文件数）。
SUMMARY_MARKERS = ("已创建恢复目录", "已恢复文件数")
ERROR_PREFIX = "错误"

# 标准错误中应出现的既有公开拒绝原因（与 backup.py 公开文案对应）。
REASON_FILE_SYMLINK = "清单引用的数据是符号链接"
REASON_PATH_ESCAPE = "清单路径解析后越出数据目录"


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
    指向的目录），因此读取造成的访问时间变化不出现在比较结果中。
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
    """递归记录普通文件：相对路径 -> 字节（不记录符号链接与目录）。"""
    return {
        key: value[1]
        for key, value in capture_tree(root).items()
        if value[0] == "file"
    }


def _symlink_supported():
    """探测当前环境是否允许创建符号链接（Windows 上常需特权）。"""
    with tempfile.TemporaryDirectory(prefix="symlink-probe-") as probe:
        try:
            os.symlink("target", Path(probe) / "link")
        except (OSError, NotImplementedError):
            return False
    return True


SYMLINK_SUPPORTED = _symlink_supported()
SYMLINK_SKIP_REASON = "当前环境不支持或无权限创建符号链接，跳过该用例"


class RestoreSnapshotDataSymlinkTests(unittest.TestCase):
    """快照数据中的符号链接必须在创建恢复目标之前被整体拒绝。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-snap-symlink-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        self.snapshot = self.work / "snapshot"
        write_files(self.source, SOURCE_FILES)

        # 夹具快照必须由公开 backup 入口生成（无摘要）；失败则中止本用例。
        proc = run_cmd(["backup", str(self.source), str(self.snapshot)])
        if proc.returncode != 0 or not self.snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：基线快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )

        # 固定夹具前提：无摘要的版本 1 清单，good.txt 位于问题条目之前。
        manifest = json.loads(
            (self.snapshot / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest.get("version"), 1, "夹具清单版本应为 1")
        files = manifest.get("files")
        self.assertEqual(
            [entry.get("path") for entry in files],
            [GOOD_REL, NESTED_REL],
            "夹具清单应先列 good.txt，再列嵌套的 数据.bin",
        )
        for entry in files:
            self.assertEqual(
                set(entry.keys()), {"path"},
                f"夹具快照不应携带 sha256 等摘要字段: {entry}",
            )

        self.data_good = self.snapshot / "data" / GOOD_REL
        self.nested_in_data = self.snapshot / "data" / NESTED_REL
        self.nested_dir_in_data = self.snapshot / "data" / NESTED_DIR

    # ---- 夹具制造 ----

    def make_file_symlink(self):
        """将 data 内嵌套文件替换为指向 data/good.txt 的文件符号链接。

        真实目标 good.txt 保留。返回链接路径。
        """
        self.assertTrue(
            self.nested_in_data.is_file(),
            "夹具前提：被替换前 data 内的 数据.bin 应为普通文件",
        )
        self.nested_in_data.unlink()
        os.symlink(FILE_LINK_TARGET, self.nested_in_data)
        self.assertTrue(
            self.nested_in_data.is_symlink(),
            "夹具制造失败：数据.bin 应已是符号链接",
        )
        # 链接解析后确实落在 data 内的真实目标上。
        self.assertEqual(
            self.nested_in_data.resolve(strict=True),
            self.data_good.resolve(strict=True),
            "文件符号链接应指向 data 内保留的 good.txt",
        )
        return self.nested_in_data

    def make_parent_directory_symlink(self):
        """将 data 内嵌套父目录替换为指向快照外目录的符号链接。

        快照外目录位于同一临时工作区，含同名普通文件 数据.bin。
        返回 (链接路径, 外部同名文件路径)。
        """
        external_nested = self.work / EXTERNAL_DIRNAME / NESTED_DIR
        external_file = external_nested / "数据.bin"
        write_files(external_nested, {"数据.bin": EXTERNAL_FILE_BYTES})

        self.assertTrue(
            self.nested_dir_in_data.is_dir(),
            "夹具前提：被替换前 data 内的嵌套父目录应为普通目录",
        )
        shutil.rmtree(self.nested_dir_in_data)
        os.symlink(external_nested, self.nested_dir_in_data,
                   target_is_directory=True)
        self.assertTrue(
            self.nested_dir_in_data.is_symlink(),
            "夹具制造失败：嵌套父目录应已是符号链接",
        )
        # 链接解析后确实越出 data，指向快照外的同名普通文件。
        self.assertEqual(
            self.nested_in_data.resolve(strict=True),
            external_file.resolve(strict=True),
            "父目录链接应把清单路径解析到快照外的同名文件",
        )
        return self.nested_dir_in_data, external_file

    # ---- 通用执行与断言 ----

    def run_restore(self, dest, select_good_only=False):
        """以公开入口执行 restore；select_good_only 时仅选择 good.txt。"""
        argv = ["restore", str(self.snapshot), str(dest)]
        if select_good_only:
            argv.extend(["--file", GOOD_REL])
        return run_cmd(argv)

    def assert_rejected_before_target_created(
        self, label, proc, dest, reason, work_before,
    ):
        """链接用例的公共断言：

        - 退出码为 2；
        - 标准输出为空（不打印任何成功摘要）；
        - 标准错误含错误前缀、既有拒绝原因与嵌套路径 嵌套 目录/数据.bin；
        - 恢复目标调用前后均不存在；
        - 整个临时工作区（源目录、快照、链接、快照外目标）逐路径、
          逐类型、逐字节不变，即没有新增文件或目录（不比较 atime）。
        """
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n目标 DEST: {dest}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(stdout, "", f"失败时标准输出应为空\n{context}")
        self.assertIn(ERROR_PREFIX, stderr, f"标准错误缺少错误提示\n{context}")
        self.assertIn(
            reason, stderr,
            f"标准错误缺少拒绝原因“{reason}”\n{context}",
        )
        self.assertIn(
            NESTED_REL, stderr,
            f"标准错误应指出问题条目路径 {NESTED_REL}\n{context}",
        )
        for marker in SUMMARY_MARKERS:
            self.assertNotIn(marker, stdout, f"标准输出出现了成功摘要\n{context}")
        self.assertFalse(
            os.path.lexists(dest),
            f"失败后恢复目标仍被创建: {dest}\n{context}",
        )
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"失败后临时工作区出现新增、覆盖或删除\n{context}",
        )
        return stdout, stderr, context

    def assert_link_unchanged(self, link, target_before, label):
        """链接本身仍是符号链接且链接目标字符串不变（不跟随链接）。"""
        self.assertTrue(
            link.is_symlink(),
            f"用例 {label}：调用后链接不再是符号链接: {link}",
        )
        self.assertEqual(
            os.readlink(link), target_before,
            f"用例 {label}：符号链接指向被改动: {link}",
        )

    def assert_source_and_real_targets_intact(self, context):
        """源目录与 data/good.txt 真实目标的类型、字节不变。"""
        self.assertEqual(
            file_tree(self.source), SOURCE_FILES,
            f"源目录内容在恢复调用后发生变化\n{context}",
        )
        self.assertTrue(
            self.data_good.is_file() and not self.data_good.is_symlink(),
            f"data 内真实目标 good.txt 的类型被改变\n{context}",
        )
        self.assertEqual(
            self.data_good.read_bytes(), GOOD_BYTES,
            f"data 内真实目标 good.txt 的字节被改动\n{context}",
        )

    def _run_two_restores(self, mutate_label, reason, check_extra):
        """对已造好的违规快照依次执行全量恢复与仅选择 good.txt 的恢复。

        调用前夹具破坏必须已完成。两次恢复使用各自全新的、事先不存在
        的目标；每次调用前都重新记录夹具状态，check_extra 在每次断言
        后额外核对链接本身与链接目标。
        """
        for select_good_only, dest_name in (
            (False, "restored-full"),
            (True, "restored-selected"),
        ):
            label = f"{mutate_label} / {'仅选择 good.txt' if select_good_only else '全量恢复'}"
            dest = self.work / dest_name
            self.assertFalse(
                os.path.lexists(dest),
                f"用例前提：恢复目标必须事先不存在: {dest}",
            )
            # 夹具状态在每次恢复调用前记录（两次调用之间现场不应有变）。
            work_before = capture_tree(self.work)

            proc = self.run_restore(dest, select_good_only=select_good_only)
            _, _, context = self.assert_rejected_before_target_created(
                label, proc, dest, reason, work_before,
            )
            check_extra(context)
            self.assert_source_and_real_targets_intact(context)

    # ---- 用例 1：data 内嵌套文件是指向 data/good.txt 的符号链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_file_symlink_full_and_selected_restore(self):
        """文件符号链接：全量与仅选择 good.txt 都在创建目标前拒绝。"""
        link = self.make_file_symlink()

        def check_extra(context):
            # 链接本身不被替换、不被解引用删除，指向仍是 ../good.txt。
            self.assert_link_unchanged(link, FILE_LINK_TARGET, "文件符号链接")
            # 链接仍可解析到保留的真实普通文件。
            self.assertTrue(
                link.resolve(strict=True).is_file(),
                f"链接保留的真实目标应仍为普通文件\n{context}",
            )

        self._run_two_restores(
            "data 内文件符号链接", REASON_FILE_SYMLINK, check_extra,
        )

    # ---- 用例 2：嵌套父目录是指向快照外目录的符号链接 ----

    @unittest.skipUnless(SYMLINK_SUPPORTED, SYMLINK_SKIP_REASON)
    def test_reject_parent_dir_symlink_full_and_selected_restore(self):
        """父目录越界链接：全量与仅选择 good.txt 都在创建目标前拒绝。"""
        link, external_file = self.make_parent_directory_symlink()
        target_before = os.readlink(link)

        def check_extra(context):
            # 越界父目录链接本身不变；快照外同名普通文件不被读取覆盖或删除。
            self.assert_link_unchanged(link, target_before, "父目录越界链接")
            self.assertTrue(
                external_file.is_file() and not external_file.is_symlink(),
                f"快照外同名文件的类型被改变\n{context}",
            )
            self.assertEqual(
                external_file.read_bytes(), EXTERNAL_FILE_BYTES,
                f"快照外同名普通文件的字节被改动\n{context}",
            )

        self._run_two_restores(
            "嵌套父目录越界链接", REASON_PATH_ESCAPE, check_extra,
        )

    # ---- 正常对照：不含任何链接的同样夹具恢复成功 ----

    def test_clean_snapshot_restores_successfully(self):
        """无链接快照：文本与嵌套中文路径、含零字节二进制共 2 文件恢复成功。"""
        # 明确保证对照快照中不存在任何符号链接。
        snapshot_tree = capture_tree(self.snapshot)
        self.assertFalse(
            any(kind == "symlink" for kind, _ in snapshot_tree.values()),
            "正常对照的快照不应包含符号链接",
        )
        snapshot_before = dict(snapshot_tree)

        dest = self.work / "restored-clean"
        self.assertFalse(os.path.lexists(dest))

        proc = self.run_restore(dest)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"目标 DEST: {dest}\n"
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

        # 恢复结果的相对路径与字节逐一等于快照数据（即备份时的源字节）。
        expected = file_tree(self.snapshot / "data")
        restored = file_tree(dest)
        self.assertEqual(
            set(restored), set(expected),
            f"恢复后的相对路径集合与快照数据不一致\n{context}",
        )
        self.assertEqual(
            len(restored), 2,
            f"恢复结果应恰好含两个文件\n{context}",
        )
        for rel, data in expected.items():
            self.assertEqual(
                restored[rel], data,
                f"恢复文件字节与快照数据不一致: {rel}\n{context}",
            )

        # 显式固定公开语义：68 69 0a 文本与嵌套路径下 00 ff 10 逐字节保留。
        self.assertEqual(
            (dest / GOOD_REL).read_bytes(), bytes([0x68, 0x69, 0x0A]),
            f"good.txt 未按快照字节恢复\n{context}",
        )
        nested_bytes = (dest / NESTED_DIR / "数据.bin").read_bytes()
        self.assertEqual(
            nested_bytes, bytes([0x00, 0xFF, 0x10]),
            f"嵌套 数据.bin 未按快照字节恢复\n{context}",
        )
        self.assertIn(0, nested_bytes, f"二进制文件应保留零字节\n{context}")

        # 成功恢复后源目录与快照都保持原样（不比较访问时间）。
        self.assertEqual(
            file_tree(self.source), SOURCE_FILES,
            f"恢复成功后源目录内容发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(self.snapshot), snapshot_before,
            f"恢复成功后快照内容发生变化\n{context}",
        )


if __name__ == "__main__":
    unittest.main()
