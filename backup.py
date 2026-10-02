#!/usr/bin/env python3
"""本地目录快照备份与恢复工具（最小功能版本）。

子命令:
    python backup.py backup  SOURCE   SNAPSHOT
    python backup.py restore SNAPSHOT DEST

备份在 SNAPSHOT 中生成:
    data/          按源目录相对结构保存的普通文件字节
    manifest.json  UTF-8 JSON 清单 (version=1, files=[{path: ...}, ...])

所有失败均向标准错误输出原因并以退出码 2 结束；成功退出码为 0。
"""

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

MANIFEST_NAME = "manifest.json"
DATA_DIR_NAME = "data"
MANIFEST_VERSION = 1
COPY_BUFFER_SIZE = 1024 * 1024
EXIT_FAILURE = 2


class BackupError(Exception):
    """可向用户报告的预期失败。"""


def fail(message):
    raise BackupError(message)


def lstat_or_none(path):
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        fail(f"cannot access {path}: {exc}")


def describe_mode(mode):
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "special file"


def require_absent(path, label):
    """目标路径必须事先不存在（包括符号链接，即使是悬空链接）。"""
    st = lstat_or_none(path)
    if st is not None:
        fail(
            f"{label} already exists ({describe_mode(st.st_mode)}), "
            f"refusing to overwrite: {path}"
        )


def require_not_within(child_real, parent_real, message):
    """child_real 不得等于 parent_real 或位于其中（参数均为已解析的绝对路径字符串）。

    位于其中是预期的安全状态；越界（不位于其中）才失败。
    """
    try:
        common = os.path.commonpath([child_real, parent_real])
    except ValueError:
        fail(message)  # 不同盘符等情况：不可能位于其中，视为越界
        return
    if common == parent_real:
        fail(message)


def require_within(child_real, parent_real, message):
    """child_real 必须等于 parent_real 或位于其中，否则视为路径越界。"""
    try:
        common = os.path.commonpath([child_real, parent_real])
    except ValueError:
        fail(message)
        return
    if common != parent_real:
        fail(message)


def copy_bytes(src, dst):
    try:
        with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst, length=COPY_BUFFER_SIZE)
    except OSError as exc:
        fail(f"failed to copy {src} to {dst}: {exc}")


def collect_regular_files(source):
    """预遍历源目录：拒绝符号链接和非普通文件/目录，返回排序后的斜杠相对路径。"""
    rel_paths = []

    def walk(directory):
        try:
            with os.scandir(directory) as entries:
                entries = list(entries)
        except OSError as exc:
            fail(f"cannot read directory {directory}: {exc}")
        for entry in sorted(entries, key=lambda e: e.name):
            if entry.is_symlink():
                fail(f"symlinks are not allowed in source: {entry.path}")
            try:
                if entry.is_dir(follow_symlinks=False):
                    walk(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    rel = Path(entry.path).relative_to(source).as_posix()
                    rel_paths.append(rel)
                else:
                    fail(f"not a regular file or directory: {entry.path}")
            except OSError as exc:
                fail(f"cannot inspect {entry.path}: {exc}")

    walk(source)
    rel_paths.sort()
    return rel_paths


def backup(source_arg, snapshot_arg):
    source = Path(source_arg)
    snapshot = Path(snapshot_arg)

    # —— 可预先判定的检查（全部在创建快照目录之前完成）——
    st = lstat_or_none(source)
    if st is None:
        fail(f"source does not exist: {source}")
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        fail(f"source is not a directory: {source}")

    require_absent(snapshot, "snapshot directory")

    source_real = str(source.resolve())
    snapshot_real = str(snapshot.resolve())
    require_not_within(
        snapshot_real,
        source_real,
        f"snapshot directory must not be located inside source directory: {snapshot}",
    )

    rel_paths = collect_regular_files(source)

    # —— 校验通过后才创建快照目录 ——
    created = False
    try:
        os.mkdir(snapshot)
        created = True
        data_dir = snapshot / DATA_DIR_NAME
        os.mkdir(data_dir)

        for rel in rel_paths:
            parts = rel.split("/")
            src_file = source.joinpath(*parts)
            dst_file = data_dir.joinpath(*parts)
            os.makedirs(dst_file.parent, exist_ok=True)
            copy_bytes(src_file, dst_file)

        manifest = {
            "version": MANIFEST_VERSION,
            "files": [{"path": rel} for rel in rel_paths],
        }
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
        tmp_path = snapshot / (MANIFEST_NAME + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(manifest_text)
            os.replace(tmp_path, snapshot / MANIFEST_NAME)
        except OSError as exc:
            if tmp_path.exists():
                os.unlink(tmp_path)
            fail(f"failed to write manifest: {exc}")
    except BackupError:
        if created:
            shutil.rmtree(snapshot, ignore_errors=True)
        raise
    except OSError as exc:
        if created:
            shutil.rmtree(snapshot, ignore_errors=True)
        fail(f"backup failed: {exc}")

    print(str(snapshot.resolve()))
    print(f"{len(rel_paths)} files")


def validate_and_resolve_entries(raw_bytes, data_dir):
    """解析并校验清单及其引用的数据文件；返回 [(rel, parts, data_file_path), ...]。"""
    try:
        manifest_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        fail(f"manifest is not valid UTF-8: {exc}")
    try:
        manifest = json.loads(manifest_text)
    except ValueError as exc:
        fail(f"manifest JSON is corrupt: {exc}")

    if not isinstance(manifest, dict):
        fail("manifest root must be a JSON object")

    version = manifest.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        fail(f"manifest field 'version' must be an integer, got {type(version).__name__}")
    if version != MANIFEST_VERSION:
        fail(f"unsupported manifest version: {version} (supported: {MANIFEST_VERSION})")

    files = manifest.get("files")
    if not isinstance(files, list):
        fail("manifest field 'files' must be a list")

    entries = []
    seen = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            fail(f"manifest files[{index}] must be an object")
        rel = item.get("path")
        if not isinstance(rel, str):
            fail(f"manifest files[{index}].path must be a string")
        if rel == "" or rel.startswith("/"):
            fail(f"invalid path in manifest (absolute or empty): {rel!r}")
        parts = rel.split("/")
        if any(part in ("", ".", "..") for part in parts):
            fail(f"invalid path component in manifest: {rel!r}")
        if any("\x00" in part for part in parts):
            fail(f"invalid path in manifest: {rel!r}")
        if rel in seen:
            fail(f"duplicate path in manifest: {rel!r}")
        seen.add(rel)
        entries.append((rel, tuple(parts)))

    # 一个清单路径不能同时充当另一个清单路径的目录分量。
    all_parts = {parts for _, parts in entries}
    for rel, parts in entries:
        for i in range(1, len(parts)):
            if parts[:i] in all_parts:
                fail(f"path conflict in manifest: {rel!r} passes through a listed file")

    data_st = lstat_or_none(data_dir)
    if data_st is None:
        fail(f"snapshot data directory is missing: {data_dir}")
    if stat.S_ISLNK(data_st.st_mode) or not stat.S_ISDIR(data_st.st_mode):
        fail(f"snapshot data path is not a directory: {data_dir}")
    data_real = str(data_dir.resolve())

    resolved = []
    for rel, parts in entries:
        candidate = data_dir.joinpath(*parts)
        for i in range(1, len(parts)):
            ancestor = data_dir.joinpath(*parts[:i])
            ast = lstat_or_none(ancestor)
            if ast is None:
                fail(f"data missing for manifest path {rel!r}: {ancestor}")
            if stat.S_ISLNK(ast.st_mode):
                fail(f"data path for {rel!r} escapes through symlink: {ancestor}")
            if not stat.S_ISDIR(ast.st_mode):
                fail(f"data path component for {rel!r} is not a directory: {ancestor}")

        fst = lstat_or_none(candidate)
        if fst is None:
            fail(f"data file missing for manifest path {rel!r}: {candidate}")
        if stat.S_ISLNK(fst.st_mode):
            fail(f"data file is a symlink: {candidate}")
        if not stat.S_ISREG(fst.st_mode):
            fail(f"data file is not a regular file: {candidate}")
        try:
            real = str(candidate.resolve())
        except OSError as exc:
            fail(f"cannot resolve data file {candidate}: {exc}")
        require_within(real, data_real, f"manifest path escapes data directory: {rel!r}")
        resolved.append((rel, parts, candidate))

    return resolved


def restore(snapshot_arg, dest_arg):
    snapshot = Path(snapshot_arg)
    dest = Path(dest_arg)

    # —— 可预先判定的检查（全部在创建恢复目录之前完成）——
    try:
        sst = os.stat(snapshot)
    except FileNotFoundError:
        fail(f"snapshot does not exist: {snapshot}")
    except OSError as exc:
        fail(f"cannot access snapshot {snapshot}: {exc}")
    if not stat.S_ISDIR(sst.st_mode):
        fail(f"snapshot is not a directory: {snapshot}")

    require_absent(dest, "destination directory")

    snapshot_real = str(snapshot.resolve())
    dest_real = str(dest.resolve())
    require_not_within(
        dest_real,
        snapshot_real,
        f"destination directory must not be located inside snapshot directory: {dest}",
    )

    manifest_path = snapshot / MANIFEST_NAME
    mst = lstat_or_none(manifest_path)
    if mst is None:
        fail(f"snapshot manifest is missing: {manifest_path}")
    if stat.S_ISLNK(mst.st_mode):
        fail(f"snapshot manifest is a symlink: {manifest_path}")
    if not stat.S_ISREG(mst.st_mode):
        fail(f"snapshot manifest is not a regular file: {manifest_path}")
    try:
        with open(manifest_path, "rb") as f:
            raw_bytes = f.read()
    except OSError as exc:
        fail(f"failed to read manifest {manifest_path}: {exc}")

    entries = validate_and_resolve_entries(raw_bytes, snapshot / DATA_DIR_NAME)

    # —— 全部校验通过后才创建恢复目录 ——
    created = False
    try:
        os.mkdir(dest)
        created = True
        for _rel, parts, src_file in entries:
            dst_file = dest.joinpath(*parts)
            os.makedirs(dst_file.parent, exist_ok=True)
            copy_bytes(src_file, dst_file)
    except BackupError:
        if created:
            shutil.rmtree(dest, ignore_errors=True)
        raise
    except OSError as exc:
        if created:
            shutil.rmtree(dest, ignore_errors=True)
        fail(f"restore failed: {exc}")

    print(str(dest.resolve()))
    print(f"{len(entries)} files")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="backup.py",
        description="Minimal local directory snapshot backup and restore tool.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_backup = subparsers.add_parser("backup", help="back up SOURCE into a new SNAPSHOT")
    p_backup.add_argument("source")
    p_backup.add_argument("snapshot")

    p_restore = subparsers.add_parser("restore", help="restore SNAPSHOT into a new DEST")
    p_restore.add_argument("snapshot")
    p_restore.add_argument("dest")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            backup(args.source, args.snapshot)
        elif args.command == "restore":
            restore(args.snapshot, args.dest)
    except BackupError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_FAILURE
    except OSError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return EXIT_FAILURE
    return 0


if __name__ == "__main__":
    sys.exit(main())
