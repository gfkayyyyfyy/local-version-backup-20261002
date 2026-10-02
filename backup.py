#!/usr/bin/env python3
"""本地目录备份与恢复最小工具（快照版本 1）。

用法:
    python backup.py backup SOURCE SNAPSHOT
    python backup.py restore SNAPSHOT DEST [--file PATH ...]

restore 的 --file 可重复指定，按清单中的相对路径（/ 分隔，逐字精确
匹配）只恢复选中的普通文件；不提供 --file 时恢复全部文件。

快照目录结构:
    SNAPSHOT/data/...        保留相对目录结构的文件原始字节
    SNAPSHOT/manifest.json   UTF-8 JSON 清单

任何可预先判定的错误都在创建目标目录之前检出；所有失败向标准错误
输出原因并以退出码 2 结束，成功时退出码为 0。
"""

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

MANIFEST_NAME = "manifest.json"
DATA_DIRNAME = "data"
FORMAT_VERSION = 1
COPY_BUFFER_SIZE = 1024 * 1024

EXIT_OK = 0
EXIT_ERROR = 2


class BackupError(Exception):
    """可向用户报告的预期错误。"""


def fail(message):
    raise BackupError(message)


def _is_within(child, parent):
    """判断已解析的路径 child 是否位于 parent 之内（含相等）。"""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _split_relpath(rel):
    """校验并拆分清单中的相对路径，返回路径分量；非法则失败。"""
    if not isinstance(rel, str) or rel == "":
        fail("清单中的路径必须是非空字符串")
    if rel.startswith("/"):
        fail(f"清单包含绝对路径: {rel}")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            if part == "..":
                fail(f"清单包含上级目录分量: {rel}")
            fail(f"清单包含不规范的路径: {rel}")
    return parts


def _validate_within(base_resolved, parts, label, raw):
    """确认 base/parts 解析后仍位于 base 之内（防御符号链接逃逸）。"""
    candidate = base_resolved.joinpath(*parts)
    # strict=False：末端文件可能尚不存在，但其已存在的父级会被真实解析。
    resolved = candidate.resolve(strict=False)
    if not _is_within(resolved, base_resolved):
        fail(f"清单路径解析后越出{label}: {raw}")
    return resolved


def _collect_source_files(source):
    """递归收集源目录中的普通文件，返回使用斜杠分隔的相对路径列表。"""
    collected = []

    def scan(dir_path, rel_prefix):
        try:
            entries = list(os.scandir(dir_path))
        except OSError as exc:
            fail(f"无法读取源目录 {dir_path}: {exc}")
        for entry in entries:
            rel = f"{rel_prefix}{entry.name}" if rel_prefix else entry.name
            # is_symlink 基于 lstat，不跟随符号链接。
            if entry.is_symlink():
                fail(f"源目录中包含符号链接: {rel}")
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                fail(f"无法读取源条目 {rel}: {exc}")
            mode = st.st_mode
            if stat.S_ISDIR(mode):
                scan(entry.path, rel + "/")
            elif stat.S_ISREG(mode):
                collected.append(rel)
            else:
                fail(f"源目录中包含非普通文件: {rel}")

    scan(str(source), "")
    collected.sort()
    return collected


def _copy_bytes(src_path, dst_path):
    """逐字节复制文件内容，不保留或依赖源文件之外的任何状态。"""
    with open(src_path, "rb") as src_f:
        with open(dst_path, "wb") as dst_f:
            shutil.copyfileobj(src_f, dst_f, length=COPY_BUFFER_SIZE)


def _remove_tree(path):
    shutil.rmtree(path, ignore_errors=True)


def cmd_backup(source_arg, snapshot_arg):
    source = Path(source_arg)
    snapshot = Path(snapshot_arg)

    # ---- 校验源目录（此时绝不创建任何目标）----
    try:
        root_st = os.lstat(source)
    except OSError:
        fail(f"源目录不存在: {source_arg}")
    if stat.S_ISLNK(root_st.st_mode):
        fail(f"源目录自身是符号链接: {source_arg}")
    if not stat.S_ISDIR(root_st.st_mode):
        fail(f"源路径不是目录: {source_arg}")

    try:
        source_resolved = source.resolve(strict=True)
    except OSError as exc:
        fail(f"无法解析源目录: {exc}")

    # ---- 校验快照路径 ----
    if os.path.lexists(snapshot):
        fail(f"快照路径已存在，拒绝覆盖: {snapshot_arg}")
    snapshot_resolved = snapshot.resolve(strict=False)
    if _is_within(snapshot_resolved, source_resolved):
        fail("快照目录不得位于源目录内")

    # ---- 完整遍历源目录，确认全部为普通文件/目录 ----
    rel_paths = _collect_source_files(source_resolved)

    # ---- 所有预先可判定的检查通过后，才创建快照目录 ----
    created = False
    try:
        os.mkdir(snapshot)
        created = True
        data_dir = snapshot / DATA_DIRNAME
        os.mkdir(data_dir)

        for rel in rel_paths:
            parts = rel.split("/")
            src_file = source_resolved.joinpath(*parts)
            dst_file = data_dir.joinpath(*parts)
            os.makedirs(dst_file.parent, exist_ok=True)
            try:
                _copy_bytes(src_file, dst_file)
            except OSError as exc:
                fail(f"复制文件失败 {rel}: {exc}")

        manifest = {
            "version": FORMAT_VERSION,
            "files": [{"path": rel} for rel in rel_paths],
        }
        manifest_tmp = snapshot / (MANIFEST_NAME + ".tmp")
        try:
            with open(manifest_tmp, "w", encoding="utf-8") as f:
                json.dump(manifest, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(manifest_tmp, snapshot / MANIFEST_NAME)
        except OSError as exc:
            fail(f"写入清单失败: {exc}")
    except BaseException:
        if created:
            _remove_tree(snapshot)
        raise

    print(f"已创建快照目录: {snapshot.resolve()}")
    print(f"已备份文件数: {len(rel_paths)}")
    return EXIT_OK


def _load_manifest(snapshot):
    """读取并严格校验快照清单，返回 (清单条目列表, data 真实路径)。"""
    try:
        snap_st = os.lstat(snapshot)
    except OSError:
        fail(f"快照目录不存在: {snapshot}")
    if stat.S_ISLNK(snap_st.st_mode):
        fail(f"快照目录自身是符号链接: {snapshot}")
    if not stat.S_ISDIR(snap_st.st_mode):
        fail(f"快照路径不是目录: {snapshot}")

    snapshot_resolved = snapshot.resolve(strict=True)
    data_dir = snapshot_resolved / DATA_DIRNAME
    try:
        data_st = os.lstat(data_dir)
    except OSError:
        fail(f"快照数据目录缺失: {data_dir}")
    if stat.S_ISLNK(data_st.st_mode) or not stat.S_ISDIR(data_st.st_mode):
        fail("快照数据目录不是普通目录")

    manifest_path = snapshot / MANIFEST_NAME
    try:
        manifest_st = os.lstat(manifest_path)
    except OSError:
        fail(f"快照清单缺失: {manifest_path}")
    if stat.S_ISLNK(manifest_st.st_mode):
        fail("快照清单是符号链接")
    if not stat.S_ISREG(manifest_st.st_mode):
        fail("快照清单不是普通文件")

    try:
        with open(manifest_path, "rb") as f:
            raw = f.read()
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"快照清单无法读取或 JSON 损坏: {exc}")

    if not isinstance(doc, dict):
        fail("清单顶层必须是 JSON 对象")

    version = doc.get("version")
    # bool 是 int 的子类，需显式排除。
    if not isinstance(version, int) or isinstance(version, bool):
        fail("清单 version 字段类型不符（必须为整数）")
    if version != FORMAT_VERSION:
        fail(f"不支持的清单版本: {version}")

    files = doc.get("files")
    if not isinstance(files, list):
        fail("清单 files 字段类型不符（必须为数组）")

    seen = set()
    entries = []
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            fail(f"清单第 {index} 项类型不符（必须为对象）")
        rel = item.get("path")
        parts = _split_relpath(rel)
        normalized = "/".join(parts)
        if normalized in seen:
            fail(f"清单包含重复文件路径: {normalized}")
        seen.add(normalized)

        # 字面路径用于检查条目本身（lstat 不跟随末端符号链接）。
        data_file = data_dir.joinpath(*parts)
        # 解析路径用于越界检查（会跟随末端及父级符号链接）。
        resolved = data_file.resolve(strict=False)
        if not _is_within(resolved, data_dir):
            fail(f"清单路径解析后越出数据目录: {normalized}")
        try:
            fst = os.lstat(data_file)
        except OSError:
            fail(f"清单引用的数据缺失: {normalized}")
        if stat.S_ISLNK(fst.st_mode):
            fail(f"清单引用的数据是符号链接: {normalized}")
        if not stat.S_ISREG(fst.st_mode):
            fail(f"清单引用的数据不是普通文件: {normalized}")

        entries.append((normalized, parts, data_file))

    return snapshot_resolved, data_dir, entries


def _validate_selection(rel):
    """校验单个 --file 选择值，返回路径分量；非法则以退出码 2 失败。

    选择值与清单 path 逐字精确匹配，因此只接受与清单路径同样的
    规范相对形式：非空、非绝对、无空/./.. 分量，不做任何归一化。
    """
    if rel == "":
        fail("无效的选择路径: 空字符串")
    if rel.startswith("/"):
        fail(f"无效的选择路径（绝对路径）: {rel}")
    parts = rel.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            fail(f"无效的选择路径（含空、. 或 .. 分量）: {rel}")
    return parts


def _select_entries(entries, selected):
    """按选择去重后的顺序从已校验的清单条目中筛出待恢复条目。"""
    by_path = {normalized: (parts, data_file) for normalized, parts, data_file in entries}
    chosen = []
    seen = set()
    for rel in selected:
        parts = _validate_selection(rel)
        normalized = "/".join(parts)
        if normalized not in by_path:
            fail(f"选择的路径未在快照清单中: {rel}")
        if normalized in seen:
            continue
        seen.add(normalized)
        entry_parts, data_file = by_path[normalized]
        chosen.append((normalized, entry_parts, data_file))
    return chosen


def cmd_restore(snapshot_arg, dest_arg, selected=None):
    snapshot = Path(snapshot_arg)
    dest = Path(dest_arg)

    # ---- 读取并完整校验清单及其引用的数据（不创建任何目标）----
    # 校验始终针对整个快照执行，与是否使用 --file 选择无关。
    snapshot_resolved, _, entries = _load_manifest(snapshot)

    # ---- 校验选择并筛出待恢复条目（未提供 --file 时恢复全部）----
    if selected:
        entries = _select_entries(entries, selected)

    # ---- 校验恢复目标路径 ----
    if os.path.lexists(dest):
        fail(f"恢复目标已存在，拒绝覆盖: {dest_arg}")
    dest_resolved = dest.resolve(strict=False)
    if _is_within(dest_resolved, snapshot_resolved):
        fail("恢复目录不得位于快照目录内")

    # 解析后不得越出恢复目录。
    for normalized, parts, _ in entries:
        _validate_within(dest_resolved, parts, "恢复目录", normalized)

    # ---- 所有预先可判定的检查通过后，才创建恢复目录 ----
    created = False
    try:
        os.mkdir(dest)
        created = True
        for normalized, parts, data_file in entries:
            dst_file = dest_resolved.joinpath(*parts)
            os.makedirs(dst_file.parent, exist_ok=True)
            try:
                _copy_bytes(data_file, dst_file)
            except OSError as exc:
                fail(f"恢复文件失败 {normalized}: {exc}")
    except BaseException:
        if created:
            _remove_tree(dest)
        raise

    print(f"已创建恢复目录: {dest.resolve()}")
    print(f"已恢复文件数: {len(entries)}")
    return EXIT_OK


def build_parser():
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="本地目录备份与恢复最小工具",
    )
    subparsers = parser.add_subparsers(dest="command")

    p_backup = subparsers.add_parser(
        "backup", help="备份源目录到新建的快照目录"
    )
    p_backup.add_argument("source")
    p_backup.add_argument("snapshot")

    p_restore = subparsers.add_parser(
        "restore", help="将快照恢复到新建的目标目录"
    )
    p_restore.add_argument("snapshot")
    p_restore.add_argument("dest")
    p_restore.add_argument(
        "--file",
        dest="files",
        action="append",
        metavar="PATH",
        default=None,
        help="只恢复清单中的指定相对路径，可重复；缺省恢复全部文件",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "backup":
        action = lambda: cmd_backup(args.source, args.snapshot)
    elif args.command == "restore":
        action = lambda: cmd_restore(args.snapshot, args.dest, args.files)
    else:
        parser.print_usage(sys.stderr)
        print("错误: 必须指定 backup 或 restore 命令", file=sys.stderr)
        return EXIT_ERROR

    try:
        return action()
    except BackupError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except OSError as exc:
        print(f"错误: 文件读写失败: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
