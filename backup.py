#!/usr/bin/env python3
"""本地目录备份与恢复最小工具（快照版本 1）。

用法:
    python backup.py backup SOURCE SNAPSHOT [--checksum] [--exclude PATH]...
                            [--exclude-dir PATH]...
    python backup.py restore SNAPSHOT DEST [--file PATH]... [--dry-run]
    python backup.py verify SNAPSHOT [--details]

--file 可重复指定，只从快照恢复清单中逐字匹配的相对路径；
未提供 --file 时恢复清单中的全部文件。

--dry-run 只完成恢复前的全部校验并在标准输出打印一行 JSON 预览
（snapshot、destination、files、paths），不创建目标目录、不复制文件；
paths 为本次选择的相对路径，按 Unicode 码点升序排列。

--exclude 可重复指定，只影响本次备份：以源目录为基准、使用 / 分隔的
相对路径，与文件相对路径逐字匹配，匹配的普通文件不写入快照；
未提供 --exclude 时备份源目录中的全部普通文件。

--exclude-dir 可重复指定，只影响本次备份：以源目录为基准、使用 / 分隔的
相对路径，与目录相对路径逐字匹配，匹配的目录及其全部后代文件不写入
快照；重复指定或父子目录重叠时取并集，未提供时不排除任何目录。

快照目录结构:
    SNAPSHOT/data/...        保留相对目录结构的文件原始字节
    SNAPSHOT/manifest.json   UTF-8 JSON 清单

任何可预先判定的错误都在创建目标目录之前检出；所有失败向标准错误
输出原因并以退出码 2 结束，成功时退出码为 0。
"""

import argparse
import hashlib
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
    if "\x00" in rel:
        # 以 JSON 字符串形式展示原始路径，使空字符显示为字面转义序列
        # \u0000，而不向标准错误写入实际空字符或触发底层路径调用。
        fail(f"清单路径包含空字符: {json.dumps(rel, ensure_ascii=False)}")
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
    """递归遍历源目录，返回 (普通文件相对路径列表, 普通目录相对路径列表)。

    两类路径都使用斜杠分隔；目录路径不含末尾斜杠，空目录同样收集。
    名称直接取自 os.scandir 的逐字结果，因此在大小写不敏感的文件系统上
    仍保留磁盘上的真实大小写，不被路径解析时的大小写折叠影响。
    """
    collected = []
    collected_dirs = []

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
                collected_dirs.append(rel)
                scan(entry.path, rel + "/")
            elif stat.S_ISREG(mode):
                collected.append(rel)
            else:
                fail(f"源目录中包含非普通文件: {rel}")

    scan(str(source), "")
    collected.sort()
    collected_dirs.sort()
    return collected, collected_dirs


def _validate_exclusions(exclusions):
    """校验 --exclude 排除路径的形态，返回按输入顺序去重后的列表。

    排除路径以源目录为基准、使用 / 分隔，与文件相对路径逐字精确匹配：
    仅拒绝空字符串、绝对路径、带盘符路径、反斜杠以及空/./.. 分量，
    不做大小写转换、分隔符转换、目录展开或通配符匹配。
    """
    seen = set()
    selected = []
    for raw in exclusions:
        if _exclude_form_invalid(raw):
            fail(f"排除路径无效: {raw}")
        if raw not in seen:
            seen.add(raw)
            selected.append(raw)
    return selected


def _exclude_form_invalid(raw):
    """判断 --exclude / --exclude-dir 参数形态是否非法（非法返回 True）。"""
    if raw == "":
        return True
    if raw.startswith("/"):
        return True
    # Windows 盘符路径（如 C:/a 或 C:a），盘符仅限 ASCII 字母。
    if len(raw) >= 2 and raw[1] == ":" and (
        "a" <= raw[0] <= "z" or "A" <= raw[0] <= "Z"
    ):
        return True
    if "\\" in raw:
        return True
    for part in raw.split("/"):
        if part in ("", ".", ".."):
            return True
    return False


def _validate_exclude_dirs(exclude_dirs):
    """校验 --exclude-dir 排除目录路径的形态，返回按输入顺序去重后的列表。

    形态规则与 --exclude 完全一致：仅拒绝空字符串、绝对路径、带盘符路径、
    反斜杠以及空/./.. 分量；是否匹配真实目录在遍历源目录后另行判定。
    """
    seen = set()
    selected = []
    for raw in exclude_dirs:
        if _exclude_form_invalid(raw):
            fail(f"排除目录路径无效: {raw}")
        if raw not in seen:
            seen.add(raw)
            selected.append(raw)
    return selected


def _is_under_excluded_dir(rel, excluded_dir_set):
    """判断文件相对路径的任一祖先目录是否在被排除目录集合中。

    按路径分量逐级比对，因此排除 cache 不会影响 cache-old 或 cache.txt。
    """
    parts = rel.split("/")
    for end in range(1, len(parts)):
        if "/".join(parts[:end]) in excluded_dir_set:
            return True
    return False


def _copy_bytes(src_path, dst_path):
    """逐字节复制文件内容，不保留或依赖源文件之外的任何状态。"""
    with open(src_path, "rb") as src_f:
        with open(dst_path, "wb") as dst_f:
            shutil.copyfileobj(src_f, dst_f, length=COPY_BUFFER_SIZE)


def _copy_bytes_and_hash(src_path, dst_path):
    """复制文件内容并返回内容的 SHA-256 摘要（64 位小写十六进制）。"""
    digest = hashlib.sha256()
    with open(src_path, "rb") as src_f:
        with open(dst_path, "wb") as dst_f:
            while True:
                chunk = src_f.read(COPY_BUFFER_SIZE)
                if not chunk:
                    break
                dst_f.write(chunk)
                digest.update(chunk)
    return digest.hexdigest()


def _remove_tree(path):
    shutil.rmtree(path, ignore_errors=True)


def _validate_checksum_value(value, rel):
    """校验清单条目里的 sha256 字段形态，合法则原样返回。"""
    # 仅字段缺省表示无摘要；显式 null 同样属于格式错误。
    if not isinstance(value, str) or len(value) != 64:
        fail(f"摘要格式错误: {rel}")
    for ch in value:
        if ch not in "0123456789abcdef":
            fail(f"摘要格式错误: {rel}")
    return value


def _sha256_of_file(path):
    """计算普通文件字节的 SHA-256，返回 64 位小写十六进制字符串。"""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(COPY_BUFFER_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def cmd_backup(source_arg, snapshot_arg, checksum=False, excludes=None,
               exclude_dirs=None):
    source = Path(source_arg)
    snapshot = Path(snapshot_arg)

    # ---- 校验 --exclude / --exclude-dir 排除路径的形态（不创建任何目标）----
    excluded = _validate_exclusions(excludes or [])
    excluded_dirs = _validate_exclude_dirs(exclude_dirs or [])

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
    # 排除参数不绕过此处的完整安全检查：符号链接与非普通文件仍整体拒绝。
    rel_paths, rel_dirs = _collect_source_files(source_resolved)

    # ---- 排除路径必须与收集到的普通文件逐字匹配（目录等一律不匹配）----
    # 单文件排除始终按完整源目录判断：即使该文件同时位于被排除目录内，
    # 只要它是源目录中的普通文件即算匹配，不因目录排除而报错。
    if excluded:
        collected = set(rel_paths)
        for raw in excluded:
            if raw not in collected:
                fail(f"排除项未匹配普通文件: {raw}")
        excluded_set = set(excluded)
        rel_paths = [rel for rel in rel_paths if rel not in excluded_set]

    # ---- 排除目录必须与源目录中真实目录的完整相对路径逐字匹配 ----
    # 名称取自遍历 scandir 的逐字结果而非路径解析：在大小写不敏感的文件
    # 系统上 Cache 可能打开 cache，但逐字集合中没有 Cache，仍按未匹配拒绝，
    # 使大小写敏感与不敏感文件系统遵守同一规则。源目录已经过完整安全检查
    # （无符号链接、无非普通文件）；不存在的路径、指向普通文件的路径以及
    # 仅大小写不同的路径一律拒绝，存在的空目录同样在集合中、是合法匹配。
    # 每个值独立校验：即使另一值是已覆盖该路径的合法父目录，拼错的子目录
    # 值仍按未匹配拒绝；重复指定与父子目录重叠在合法值之间自然取并集。
    if excluded_dirs:
        real_dir_set = set(rel_dirs)
        excluded_dir_set = set()
        for raw in excluded_dirs:
            if raw not in real_dir_set:
                fail(f"排除目录未匹配普通目录: {raw}")
            excluded_dir_set.add(raw)
        rel_paths = [
            rel for rel in rel_paths
            if not _is_under_excluded_dir(rel, excluded_dir_set)
        ]

    # ---- 所有预先可判定的检查通过后，才创建快照目录 ----
    created = False
    try:
        os.mkdir(snapshot)
        created = True
        data_dir = snapshot / DATA_DIRNAME
        os.mkdir(data_dir)

        checksums = {} if checksum else None
        for rel in rel_paths:
            parts = rel.split("/")
            src_file = source_resolved.joinpath(*parts)
            dst_file = data_dir.joinpath(*parts)
            os.makedirs(dst_file.parent, exist_ok=True)
            try:
                if checksum:
                    checksums[rel] = _copy_bytes_and_hash(src_file, dst_file)
                else:
                    _copy_bytes(src_file, dst_file)
            except OSError as exc:
                fail(f"复制文件失败 {rel}: {exc}")

        if checksum:
            file_entries = [
                {"path": rel, "sha256": checksums[rel]} for rel in rel_paths
            ]
        else:
            file_entries = [{"path": rel} for rel in rel_paths]
        manifest = {
            "version": FORMAT_VERSION,
            "files": file_entries,
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

        # 仅字段缺省表示无摘要；显式 null、类型/长度/字符不符均为格式错误。
        has_checksum = "sha256" in item
        if has_checksum:
            expected = _validate_checksum_value(item["sha256"], normalized)
            try:
                actual = _sha256_of_file(data_file)
            except OSError as exc:
                fail(f"无法读取文件以校验摘要: {normalized}: {exc}")
            if actual != expected:
                fail(f"摘要校验不一致: {normalized}")

        entries.append((normalized, parts, data_file, has_checksum))

    return snapshot_resolved, data_dir, entries


def _validate_file_selection(selections):
    """校验 --file 选择值，返回按输入顺序去重后的列表；非法则失败。

    选择值与清单 path 逐字精确匹配：仅拒绝绝对路径与空、.、.. 分量，
    不做大小写转换、路径归一化、通配符匹配或目录展开。
    """
    seen = set()
    selected = []
    for sel in selections:
        if sel == "":
            fail("--file 选择不能为空字符串")
        if sel.startswith("/"):
            fail(f"--file 选择不能是绝对路径: {sel}")
        for part in sel.split("/"):
            if part in ("", ".", ".."):
                fail(f"--file 选择包含无效路径分量: {sel}")
        if sel not in seen:
            seen.add(sel)
            selected.append(sel)
    return selected


def cmd_restore(snapshot_arg, dest_arg, file_selections=None, dry_run=False):
    snapshot = Path(snapshot_arg)
    dest = Path(dest_arg)

    # ---- 校验 --file 选择值的形态（不创建任何目标）----
    selected = _validate_file_selection(file_selections or [])

    # ---- 读取并完整校验清单及其引用的数据（不创建任何目标）----
    # 清单校验始终针对整个快照，与是否选择子集无关。
    snapshot_resolved, _, entries = _load_manifest(snapshot)
    # 恢复逻辑不需要摘要标记，还原为三元组以保持原有处理不变。
    entries = [(normalized, parts, data_file)
               for normalized, parts, data_file, _ in entries]

    # ---- 校验选择的路径确实列入清单（逐字精确匹配）----
    if selected:
        by_path = {normalized: (parts, data_file)
                   for normalized, parts, data_file in entries}
        chosen = []
        for sel in selected:
            entry = by_path.get(sel)
            if entry is None:
                fail(f"选择的路径未在快照清单中: {sel}")
            chosen.append((sel, entry[0], entry[1]))
        entries = chosen

    # ---- 校验恢复目标路径 ----
    if os.path.lexists(dest):
        fail(f"恢复目标已存在，拒绝覆盖: {dest_arg}")
    dest_resolved = dest.resolve(strict=False)
    if _is_within(dest_resolved, snapshot_resolved):
        fail("恢复目录不得位于快照目录内")

    # 解析后不得越出恢复目录。
    for normalized, parts, _ in entries:
        _validate_within(dest_resolved, parts, "恢复目录", normalized)

    # ---- 预览：只输出本次计划，不创建任何目录或文件 ----
    # 此时恢复前的全部校验（含未选中条目）均已通过；目标写入权限不在
    # 预检范围内，预览成功不承诺随后的实际复制一定成功。
    if dry_run:
        paths = sorted(normalized for normalized, _, _ in entries)
        preview = {
            "snapshot": str(snapshot_resolved),
            "destination": str(dest_resolved),
            "files": len(paths),
            "paths": paths,
        }
        print(json.dumps(preview, ensure_ascii=False))
        return EXIT_OK

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


def cmd_verify(snapshot_arg, details=False):
    """只读校验快照：完整检查清单及其引用的数据，不创建或修改任何内容。"""
    snapshot = Path(snapshot_arg)

    # 与恢复前完全相同的校验规则；仅统计结果，不复制文件。
    snapshot_resolved, _, entries = _load_manifest(snapshot)

    verified = sum(1 for *_, has_checksum in entries if has_checksum)
    result = {
        "snapshot": str(snapshot_resolved),
        "files": len(entries),
        "verified": verified,
        "unchecked": len(entries) - verified,
    }
    if details:
        # 逐文件明细：path 保留清单中的相对路径，按 Unicode 码点升序
        # （Python 字符串默认比较即码点序），与清单顺序无关。
        result["entries"] = sorted(
            (
                {
                    "path": normalized,
                    "status": "verified" if has_checksum else "unchecked",
                }
                for normalized, _, _, has_checksum in entries
            ),
            key=lambda item: item["path"],
        )
    print(json.dumps(result, ensure_ascii=False))
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
    p_backup.add_argument(
        "--checksum",
        action="store_true",
        help="为每个文件记录内容的 SHA-256 摘要；恢复时带摘要的文件"
             "须与摘要一致才会恢复",
    )
    p_backup.add_argument(
        "--exclude",
        dest="excludes",
        action="append",
        metavar="PATH",
        help="本次备份排除该相对路径对应的普通文件，可重复指定；"
             "与源目录中的文件相对路径逐字匹配，缺省时备份全部文件",
    )
    p_backup.add_argument(
        "--exclude-dir",
        dest="exclude_dirs",
        action="append",
        metavar="PATH",
        help="本次备份排除该相对路径对应的目录及其全部后代文件，"
             "可重复指定；与源目录中的目录相对路径逐字匹配，"
             "重复指定或父子目录重叠时取并集，缺省时不排除任何目录",
    )

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
        help="只恢复清单中该相对路径对应的文件，可重复指定；"
             "缺省时恢复全部文件",
    )
    p_restore.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        help="只执行恢复前的全部校验并在标准输出打印一行 JSON 预览"
             "（snapshot、destination、files、paths），不创建目标目录、"
             "不复制或修改任何文件；paths 按 Unicode 码点升序排列",
    )

    p_verify = subparsers.add_parser(
        "verify", help="只读校验快照清单及其引用的数据，不恢复任何文件"
    )
    p_verify.add_argument("snapshot")
    p_verify.add_argument(
        "--details",
        action="store_true",
        help="校验成功时在结果 JSON 中附加 entries 逐文件明细"
             "（path 与 status），按路径的 Unicode 码点升序排列",
    )

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "backup":
        action = lambda: cmd_backup(
            args.source, args.snapshot, args.checksum, args.excludes,
            args.exclude_dirs,
        )
    elif args.command == "restore":
        action = lambda: cmd_restore(
            args.snapshot, args.dest, args.files, args.dry_run
        )
    elif args.command == "verify":
        action = lambda: cmd_verify(args.snapshot, args.details)
    else:
        parser.print_usage(sys.stderr)
        print("错误: 必须指定 backup、restore 或 verify 命令", file=sys.stderr)
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
