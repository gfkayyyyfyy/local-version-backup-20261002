#!/usr/bin/env python3
"""verify 对清单路径格式与重复路径的回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py verify SNAPSHOT

只观察退出码、标准输出、标准错误与文件原始字节，不调用 backup.py 的
任何内部校验函数。仅依赖 Python 3 标准库；全部源目录与快照在独立临时
目录中运行时准备，用例结束自动清理，不读写用户现有目录，不依赖网络、
第三方库、符号链接权限或固定机器路径。

夹具源数据由公开 backup 命令生成，含两个普通文件：

- ``note.txt``：内容 ``old`` 后接换行；
- ``嵌套 目录/二进制 文件.bin``：依次为 0、255、13、10 的四个字节。

覆盖约定：

1. 成功用例：不带 --checksum 的有效快照，退出码 0、标准错误为空、标准
   输出只有一个可解析 JSON 对象，snapshot 为快照解析后的绝对路径，
   files/verified/unchecked=2/0/2，中文与空格路径照常有效。
2. 失败用例：每例从有效快照独立准备，仅改动 manifest.json 中清单条目的
   path 内容（保持合法 JSON、整数版本 1 与原有数据完整），并让一个有效
   条目位于错误条目前面：
   - path 缺失、为 null、数字或空字符串：含“清单中的路径必须是非空字符串”；
   - 以 / 开头：含“清单包含绝对路径”与对应原始路径；
   - 出现连续 / 或独立的 . 分量：含“清单包含不规范的路径”与原始路径；
   - 含 .. 分量：含“清单包含上级目录分量”与原始路径；
   - 追加逐字相同的路径：含“清单包含重复文件路径”与对应原始路径。
   所有失败退出码为 2、标准输出为空，不输出已经检查过的有效条目的
   局部成功结果。
3. 所有用例以准备完成后的目录状态为比较基准，比较 verify 前后源目录、
   快照与整个临时工作区：相对路径集合、条目类型与普通文件字节一致，
   没有新增报告或恢复目录（清单的准备性修改不算 verify 的写入，读取
   造成的访问时间变化不参与比较）。
"""

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

# 夹具中的两个相对路径（斜杠分隔，目录与文件名含中文和空格）。
NOTE_REL = "note.txt"
BIN_REL = "嵌套 目录/二进制 文件.bin"

# 备份时的固定内容：``old`` 后接换行，以及依次为 0、255、13、10 的字节。
NOTE_BYTES = "old\n".encode("utf-8")
BINARY_BYTES = bytes([0, 255, 13, 10])

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
}

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_PATH_TYPE = "清单中的路径必须是非空字符串"
REASON_ABSOLUTE = "清单包含绝对路径"
REASON_UNNORMALIZED = "清单包含不规范的路径"
REASON_PARENT = "清单包含上级目录分量"
REASON_DUPLICATE = "清单包含重复文件路径"


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

    只记录条目类型与普通文件字节，不记录访问时间等读取易变属性。
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


class VerifyManifestPathTests(unittest.TestCase):
    """verify 对清单路径形态错误与重复路径的确定性公开结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照及任何意外产物都受限于此，
        # 用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-path-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"

        write_files(self.source, BACKUP_FILES)

    # ---- 通用工具与断言 ----

    def make_snapshot(self, name):
        """用公开 backup 命令在工作区内新建无摘要快照，失败则中止本用例。"""
        snapshot = self.work / name
        proc = run_cmd(["backup", str(self.source), str(snapshot)])
        if proc.returncode != 0 or not snapshot.is_dir():
            raise RuntimeError(
                "测试夹具：快照创建失败\n"
                f"exit={proc.returncode}\n"
                f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
            )
        return snapshot

    def read_manifest(self, snapshot):
        """读取快照清单（JSON 是公开快照格式的一部分）。"""
        return json.loads(
            (snapshot / "manifest.json").read_text(encoding="utf-8")
        )

    def write_manifest(self, snapshot, doc):
        """整体重写快照清单，用于在样例中引入唯一一种错误。"""
        (snapshot / "manifest.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def run_verify(self, snapshot):
        """以公开入口执行 verify。"""
        return run_cmd(["verify", str(snapshot)])

    def assert_work_intact(self, source_before, snapshot_before, work_before,
                           snapshot, context):
        """verify 不得改动源目录、快照，也不得在工作区新增任何文件。"""
        self.assertEqual(
            capture_tree(self.source), source_before,
            f"verify 后源目录目录树发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(snapshot), snapshot_before,
            f"verify 后快照目录树发生变化\n{context}",
        )
        self.assertEqual(
            capture_tree(self.work), work_before,
            f"verify 后工作区出现新增/改动文件（报告、恢复目录等）\n{context}",
        )

    def assert_verify_success(self, snapshot, expected_files,
                              expected_verified, expected_unchecked, label):
        """成功用例公共流程：退出码 0、stderr 空、stdout 仅一个结果 JSON，
        且源目录、快照与整个工作区相对调用前保持不变。"""
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(snapshot)
        work_before = capture_tree(self.work)

        proc = self.run_verify(snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 0, f"退出码应为 0\n{context}")
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
            result["snapshot"], str(snapshot.resolve()),
            f"snapshot 应为快照解析后的绝对路径\n{context}",
        )
        self.assertEqual(
            result["files"], expected_files, f"files 计数不符\n{context}",
        )
        self.assertEqual(
            result["verified"], expected_verified,
            f"verified 计数不符\n{context}",
        )
        self.assertEqual(
            result["unchecked"], expected_unchecked,
            f"unchecked 计数不符\n{context}",
        )

        self.assert_work_intact(
            source_before, snapshot_before, work_before, snapshot, context,
        )
        return result, context

    def prepare_tampered_snapshot(self, name, mutate, label, input_desc,
                                  expected_entry_count=2):
        """从公开 backup 命令生成的有效无摘要版本 1 快照出发，仅改动清单
        条目的 path 内容，保持合法 JSON、整数版本 1 与原有数据完整。

        mutate 接收清单的 files 数组并就地修改；原始清单中有效条目
        note.txt 排在被改动条目之前，满足“一个有效条目位于错误条目前面”。
        返回篡改完成后的快照；目录树比较基准由调用方在此之后采集。
        """
        snapshot = self.make_snapshot(name)
        manifest = self.read_manifest(snapshot)
        # 测试前提：起点是版本 1、两个无摘要条目的有效快照，note.txt 在前。
        self.assertEqual(
            manifest.get("version"), 1,
            f"测试前提：{label} 起点清单版本应为整数 1\n输入: {input_desc}",
        )
        self.assertIsInstance(manifest.get("version"), int)
        self.assertNotIsInstance(manifest.get("version"), bool)
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            sorted(BACKUP_FILES),
            f"测试前提：{label} 起点清单应恰好包含两个相对路径"
            f"\n输入: {input_desc}",
        )
        self.assertEqual(
            manifest["files"][0]["path"], NOTE_REL,
            f"测试前提：{label} 有效条目 note.txt 应排在错误条目前面"
            f"\n输入: {input_desc}",
        )
        self.assertTrue(
            all(set(item) == {"path"} for item in manifest["files"]),
            f"测试前提：{label} 起点条目均不应带 sha256 字段"
            f"\n输入: {input_desc}",
        )

        mutate(manifest["files"])
        self.write_manifest(snapshot, manifest)

        # 篡改后重新读取确认：清单仍是合法 JSON、版本仍为整数 1，
        # 保证拒绝原因只可能来自被改动的 path 内容本身。
        after = self.read_manifest(snapshot)
        self.assertEqual(
            after.get("version"), 1,
            f"测试前提：{label} 篡改后版本应保持整数 1\n输入: {input_desc}",
        )
        self.assertEqual(
            len(after["files"]), expected_entry_count,
            f"测试前提：{label} 篡改后条目数应为 {expected_entry_count}"
            f"\n输入: {input_desc}",
        )
        return snapshot

    def assert_verify_failure(self, snapshot, reasons, label, input_desc):
        """失败用例公共流程：退出码 2、stdout 为空、stderr 含全部原因片段，
        且源目录、快照与整个工作区相对调用前（篡改完成之后）保持不变。"""
        # 基准取自清单改动完成之后：此后的只读 verify 不得改动任何内容。
        source_before = capture_tree(self.source)
        snapshot_before = capture_tree(snapshot)
        work_before = capture_tree(self.work)

        proc = self.run_verify(snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\n输入路径: {input_desc}\n"
            f"快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            stdout, "",
            f"失败后标准输出应为空，不得输出已检查条目的局部成功结果"
            f"\n{context}",
        )
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少原因“{reason}”\n{context}",
            )

        self.assert_work_intact(
            source_before, snapshot_before, work_before, snapshot, context,
        )
        return stdout, stderr, context

    def run_bad_path_case(self, label, input_desc, mutate, reasons,
                          expected_entry_count=2):
        """路径失败用例统一入口：准备、执行、断言公开结果与目录不变。"""
        snapshot = self.prepare_tampered_snapshot(
            f"snap-{label}", mutate, label, input_desc,
            expected_entry_count=expected_entry_count,
        )
        self.assert_verify_failure(snapshot, reasons, label, input_desc)

    # ---- 成功：不带摘要的有效快照 ----

    def test_verify_valid_snapshot_without_checksums(self):
        """不带 --checksum 的有效快照：两个文件均无摘要，2/0/2，
        中文与空格路径照常有效。"""
        snapshot = self.make_snapshot("snap-valid")
        manifest = self.read_manifest(snapshot)
        # 测试前提：清单为版本 1、两个无摘要条目，路径含中文与空格。
        self.assertEqual(manifest.get("version"), 1)
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            sorted(BACKUP_FILES),
            "测试前提：清单应恰好包含两个相对路径",
        )
        self.assertTrue(
            all("sha256" not in item for item in manifest["files"]),
            "测试前提：无摘要快照不应有任何条目带 sha256 字段",
        )
        self.assert_verify_success(snapshot, 2, 0, 2, "无摘要有效快照")

    # ---- 失败：path 缺失或类型/取值不是非空字符串 ----

    def test_verify_rejects_missing_path(self):
        """错误条目缺少 path 字段且排在有效条目之后：非空字符串诊断。"""
        def mutate(files):
            del files[1]["path"]

        self.run_bad_path_case(
            "path-missing", "path 字段缺失（键不存在）", mutate,
            [REASON_PATH_TYPE],
        )

    def test_verify_rejects_null_path(self):
        """错误条目 path 显式为 null：不等同于字段缺省，仍按类型拒绝。"""
        def mutate(files):
            files[1]["path"] = None

        self.run_bad_path_case(
            "path-null", "null", mutate,
            [REASON_PATH_TYPE],
        )

    def test_verify_rejects_numeric_path(self):
        """错误条目 path 为数字：类型不符，按非空字符串诊断拒绝。"""
        def mutate(files):
            files[1]["path"] = 123

        self.run_bad_path_case(
            "path-number", "123（数字）", mutate,
            [REASON_PATH_TYPE],
        )

    def test_verify_rejects_empty_path(self):
        """错误条目 path 为空字符串：按非空字符串诊断拒绝。"""
        def mutate(files):
            files[1]["path"] = ""

        self.run_bad_path_case(
            "path-empty", '""（空字符串）', mutate,
            [REASON_PATH_TYPE],
        )

    # ---- 失败：绝对路径 ----

    def test_verify_rejects_absolute_path(self):
        """错误条目 path 以 / 开头：含“清单包含绝对路径”与原始路径。"""
        bad_path = "/etc/passwd"

        def mutate(files):
            files[1]["path"] = bad_path

        self.run_bad_path_case(
            "path-absolute", bad_path, mutate,
            [REASON_ABSOLUTE, bad_path],
        )

    # ---- 失败：不规范路径（连续 / 或独立的 . 分量）----

    def test_verify_rejects_consecutive_slashes(self):
        """错误条目 path 含连续 /：含“清单包含不规范的路径”与原始路径。"""
        bad_path = "嵌套 目录//二进制 文件.bin"

        def mutate(files):
            files[1]["path"] = bad_path

        self.run_bad_path_case(
            "path-double-slash", bad_path, mutate,
            [REASON_UNNORMALIZED, bad_path],
        )

    def test_verify_rejects_dot_component(self):
        """错误条目 path 含独立的 . 分量：不规范路径诊断与原始路径。"""
        bad_path = "嵌套 目录/./二进制 文件.bin"

        def mutate(files):
            files[1]["path"] = bad_path

        self.run_bad_path_case(
            "path-dot", bad_path, mutate,
            [REASON_UNNORMALIZED, bad_path],
        )

    # ---- 失败：上级目录分量 ----

    def test_verify_rejects_parent_component(self):
        """错误条目 path 含 .. 分量：含“清单包含上级目录分量”与原始路径。"""
        bad_path = "嵌套 目录/../note.txt"

        def mutate(files):
            files[1]["path"] = bad_path

        self.run_bad_path_case(
            "path-parent", bad_path, mutate,
            [REASON_PARENT, bad_path],
        )

    # ---- 失败：重复文件路径 ----

    def test_verify_rejects_duplicate_path(self):
        """在有效条目之后追加逐字相同的路径：含“清单包含重复文件路径”
        与该原始路径。"""
        def mutate(files):
            # 逐字复制已有的有效条目 note.txt，追加为第三个（错误）条目。
            self.assertEqual(files[0]["path"], NOTE_REL)
            files.append(dict(files[0]))

        self.run_bad_path_case(
            "path-duplicate", NOTE_REL, mutate,
            [REASON_DUPLICATE, NOTE_REL],
            expected_entry_count=3,
        )


if __name__ == "__main__":
    unittest.main()
