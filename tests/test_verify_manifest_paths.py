#!/usr/bin/env python3
"""verify 对清单路径格式与重复路径的只读校验回归测试。

验收方式严格走 README 公开命令：

    python backup.py backup SOURCE SNAPSHOT
    python backup.py verify SNAPSHOT

只观察退出码、标准输出、标准错误与目录内容，不调用 backup.py 的任何
内部校验函数，也不以内部函数代替入口验证。仅依赖 Python 3 标准库；
全部源目录与快照在每个用例独立的临时目录中运行时准备，用例结束自动
清理，不读写用户现有目录，不依赖符号链接权限、网络或固定机器路径。

夹具源数据由公开 backup 命令生成（不带 --checksum 的无摘要版本 1
快照），含两个普通文件：

- ``note.txt``：内容为 ``old`` 后接换行；
- ``嵌套 目录/二进制 文件.bin``：依次为 0、255、13、10 的四个字节
  （目录名含中文与空格）。

覆盖约定：

1. 成功用例：退出码 0、标准错误为空、标准输出只有一个可解析 JSON
   对象，snapshot 为快照解析后的绝对路径，files/verified/unchecked
   为 2/0/2，中文与空格路径照常有效。
2. 失败用例：每例从有效快照独立准备，仅修改 manifest.json 中条目的
   path 内容（保持合法 JSON、整数版本 1 与原有 data 字节完整），并让
   一个有效条目排在错误条目前面：
   - path 缺失、为 null、数字或空字符串：
     标准错误包含“清单中的路径必须是非空字符串”；
   - path 以 / 开头：包含“清单包含绝对路径”及该原始路径；
   - path 含连续 / 或独立的 . 分量：包含“清单包含不规范的路径”及
     该原始路径；
   - path 含 .. 分量：包含“清单包含上级目录分量”及该原始路径；
   - 追加逐字相同的路径：包含“清单包含重复文件路径”及该路径。
   所有失败退出码为 2、标准输出为空（不输出已检查过的有效条目的
   局部成功结果）。
3. 所有用例以准备（含清单篡改）完成后的目录状态为比较基准，确认
   verify 前后源目录、快照与整个临时工作区的相对路径集合、条目类型
   及普通文件字节一致，没有新增报告或恢复目录；清单的准备性修改不算
   verify 的写入，读取造成的访问时间变化不参与比较。
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

# 夹具中的两个相对路径（斜杠分隔，目录名含中文与空格）。
NOTE_REL = "note.txt"
BIN_REL = "嵌套 目录/二进制 文件.bin"

# 备份时的固定内容：``old`` 后接换行，以及依次为 0、255、13、10 的
# 四个字节。
NOTE_BYTES = "old\n".encode("utf-8")
BINARY_BYTES = bytes([0, 255, 13, 10])

BACKUP_FILES = {
    NOTE_REL: NOTE_BYTES,
    BIN_REL: BINARY_BYTES,
}

# 标准错误中应出现的原因片段（与公开报错文案对应，不调用内部函数）。
REASON_NONEMPTY = "清单中的路径必须是非空字符串"
REASON_ABSOLUTE = "清单包含绝对路径"
REASON_IRREGULAR = "清单包含不规范的路径"
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
    """verify 对清单 path 形态与重复路径的确定性公开结果。"""

    def setUp(self):
        # 每个用例独立临时工作区：源目录、快照及任何意外产物都受限于此，
        # 用例结束随 TemporaryDirectory 一并清理。
        self._tmp = tempfile.TemporaryDirectory(prefix="verify-path-test-")
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        self.source = self.work / "source"
        write_files(self.source, BACKUP_FILES)

    # ---- 通用工具与断言 ----

    def make_snapshot(self, name="snapshot"):
        """用公开 backup 命令（不带 --checksum）新建有效无摘要快照。"""
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

    def make_valid_snapshot(self, label):
        """准备有效的无摘要版本 1 快照，并确认其清单前提。"""
        snapshot = self.make_snapshot(f"snap-{label}")
        manifest = self.read_manifest(snapshot)
        # 测试前提：起点是版本 1、两个无摘要条目的有效快照，且有效条目
        # note.txt 排在将被篡改的条目之前。
        self.assertEqual(
            manifest.get("version"), 1,
            f"测试前提：{label} 起点清单版本应为整数 1",
        )
        self.assertIs(type(manifest["version"]), int)
        self.assertEqual(
            [item["path"] for item in manifest["files"]],
            [NOTE_REL, BIN_REL],
            f"测试前提：{label} 起点清单应恰好包含两个相对路径",
        )
        self.assertTrue(
            all(set(item) == {"path"} for item in manifest["files"]),
            f"测试前提：{label} 起点条目均应只有 path 字段（无摘要）",
        )
        return snapshot

    def run_verify(self, snapshot):
        """以公开入口执行 verify。"""
        return run_cmd(["verify", str(snapshot)])

    def assert_work_intact(self, baselines, snapshot, context):
        """verify 不得改动源目录、快照，也不得在工作区新增任何文件。

        baselines 取自准备（含清单篡改）完成之后、verify 调用之前。
        """
        source_before, snapshot_before, work_before = baselines
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

    def capture_baselines(self, snapshot):
        """以当前目录状态为基准，记录源目录、快照与整个工作区。"""
        return (
            capture_tree(self.source),
            capture_tree(snapshot),
            capture_tree(self.work),
        )

    # ---- 成功：无摘要有效快照 ----

    def test_verify_valid_snapshot_without_checksums(self):
        """有效无摘要快照：退出码 0、stderr 空、stdout 只有一个结果 JSON，
        files/verified/unchecked 为 2/0/2，中文与空格路径照常有效。"""
        snapshot = self.make_valid_snapshot("valid")
        baselines = self.capture_baselines(snapshot)

        proc = self.run_verify(snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            "用例: 有效无摘要快照\n"
            f"快照 SNAPSHOT: {snapshot}\n"
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
        self.assertEqual(result["files"], 2, f"files 计数不符\n{context}")
        self.assertEqual(
            result["verified"], 0, f"verified 计数不符\n{context}",
        )
        self.assertEqual(
            result["unchecked"], 2, f"unchecked 计数不符\n{context}",
        )

        self.assert_work_intact(baselines, snapshot, context)

    # ---- 失败用例公共流程 ----

    def assert_verify_rejects_path(self, tamper, reasons, label, value_desc):
        """清单 path 失败用例公共流程。

        先用 tamper 篡改有效快照清单（仅改动 path 相关内容，保持合法
        JSON、整数版本 1 与原有 data 完整，且一个有效条目排在错误条目
        前面），再以篡改完成后的目录树为基准执行 verify：退出码 2、
        stdout 为空、stderr 含 reasons 中全部片段，且源目录、快照、
        工作区与篡改后基准完全一致。
        """
        snapshot = self.make_valid_snapshot(label)
        manifest = self.read_manifest(snapshot)
        tamper(manifest["files"])
        self.write_manifest(snapshot, manifest)

        # 篡改后重新读取确认：清单仍是合法 JSON、版本仍为整数 1，
        # 保证拒绝原因只可能来自 path 内容本身。
        after = self.read_manifest(snapshot)
        self.assertEqual(
            after.get("version"), 1,
            f"测试前提：{label} 篡改后版本应保持整数 1\n输入: {value_desc}",
        )
        self.assertIs(type(after["version"]), int)
        self.assertIsInstance(after.get("files"), list)
        # 测试前提：一个有效条目排在错误条目之前。
        self.assertEqual(
            after["files"][0],
            {"path": NOTE_REL},
            f"测试前提：{label} 首个条目应为有效条目\n输入: {value_desc}",
        )

        # 基准取自清单改动完成之后：此后的只读 verify 不得改动任何内容。
        baselines = self.capture_baselines(snapshot)

        proc = self.run_verify(snapshot)
        stdout = proc.stdout.decode("utf-8", errors="replace")
        stderr = proc.stderr.decode("utf-8", errors="replace")
        context = (
            f"用例: {label}\npath 输入: {value_desc}\n"
            f"快照 SNAPSHOT: {snapshot}\n"
            f"exit={proc.returncode}\nstdout={stdout!r}\nstderr={stderr!r}"
        )

        self.assertEqual(proc.returncode, 2, f"退出码应为 2\n{context}")
        self.assertEqual(
            stdout, "",
            f"失败后标准输出应为空（不得输出局部成功结果）\n{context}",
        )
        for reason in reasons:
            self.assertIn(
                reason, stderr,
                f"标准错误缺少原因“{reason}”\n{context}",
            )

        self.assert_work_intact(baselines, snapshot, context)

    def tamper_second_entry(self, value):
        """构造篡改函数：仅改动第二个（错误）条目的 path，首个有效条目
        保持原样排在前面。"""
        def tamper(files):
            self.assertEqual(
                len(files), 2, "测试前提：起点清单应恰好两个条目",
            )
            if value is _MISSING:
                del files[1]["path"]
            else:
                files[1]["path"] = value
        return tamper

    # ---- 失败：path 缺失或不是非空字符串 ----

    def test_verify_rejects_missing_path_field(self):
        """错误条目缺少 path 键：必须是非空字符串。"""
        self.assert_verify_rejects_path(
            self.tamper_second_entry(_MISSING),
            [REASON_NONEMPTY],
            "path-missing", "字段缺失（键不存在）",
        )

    def test_verify_rejects_null_path(self):
        """错误条目 path 显式为 null：不等同于字段缺省或非空字符串。"""
        self.assert_verify_rejects_path(
            self.tamper_second_entry(None),
            [REASON_NONEMPTY],
            "path-null", "null",
        )

    def test_verify_rejects_numeric_path(self):
        """错误条目 path 为数字：类型不符，必须是非空字符串。"""
        self.assert_verify_rejects_path(
            self.tamper_second_entry(1),
            [REASON_NONEMPTY],
            "path-number", "1（数字）",
        )

    def test_verify_rejects_empty_path(self):
        """错误条目 path 为空字符串：必须是非空字符串。"""
        self.assert_verify_rejects_path(
            self.tamper_second_entry(""),
            [REASON_NONEMPTY],
            "path-empty", '""（空字符串）',
        )

    # ---- 失败：绝对路径 ----

    def test_verify_rejects_absolute_path(self):
        """错误条目 path 以 / 开头：绝对路径，诊断含该原始路径。"""
        bad = "/etc/passwd"
        self.assert_verify_rejects_path(
            self.tamper_second_entry(bad),
            [REASON_ABSOLUTE, bad],
            "path-absolute", f"{bad!r}（以 / 开头）",
        )

    # ---- 失败：不规范路径（连续 / 或独立 . 分量）----

    def test_verify_rejects_consecutive_slashes(self):
        """错误条目 path 含连续 /（空分量）：不规范路径，含原始路径。"""
        bad = "嵌套 目录//二进制 文件.bin"
        self.assert_verify_rejects_path(
            self.tamper_second_entry(bad),
            [REASON_IRREGULAR, bad],
            "path-double-slash", f"{bad!r}（含连续 /）",
        )

    def test_verify_rejects_dot_component(self):
        """错误条目 path 含独立 . 分量：不规范路径，含原始路径。"""
        bad = "./note.txt"
        self.assert_verify_rejects_path(
            self.tamper_second_entry(bad),
            [REASON_IRREGULAR, bad],
            "path-dot", f"{bad!r}（含独立 . 分量）",
        )

    # ---- 失败：上级目录分量 ----

    def test_verify_rejects_parent_component(self):
        """错误条目 path 含 .. 分量：上级目录分量，含原始路径。"""
        bad = "../escape.txt"
        self.assert_verify_rejects_path(
            self.tamper_second_entry(bad),
            [REASON_PARENT, bad],
            "path-parent", f"{bad!r}（含 .. 分量）",
        )

    # ---- 失败：重复文件路径 ----

    def test_verify_rejects_duplicate_path(self):
        """追加与首个有效条目逐字相同的路径：重复文件路径，含该路径。"""
        def tamper(files):
            self.assertEqual(
                len(files), 2, "测试前提：起点清单应恰好两个条目",
            )
            # 逐字追加与首个有效条目相同的 path，错误条目排在最后。
            files.append({"path": NOTE_REL})

        self.assert_verify_rejects_path(
            tamper,
            [REASON_DUPLICATE, NOTE_REL],
            "path-duplicate", f"{NOTE_REL!r}（逐字重复追加）",
        )


# 哨兵：表示“删除 path 键”而非写入某个值。
_MISSING = object()


if __name__ == "__main__":
    unittest.main()
