#!/usr/bin/env python3
"""仅供 test_backup_manifest_ioerror.py 的子进程使用的清单故障注入入口。

本身不是测试模块（文件名不以 test 开头，unittest 发现时不会导入），
也不被 backup.py 导入；测试通过

    python -m manifest_iofail_inject backup.py backup SOURCE SNAPSHOT [--checksum]

在子进程内先给内建 open 或 os.replace 打上受控故障补丁，再用 runpy
原样执行 backup.py 的公开命令行入口。backup.py 仍自行完成参数解析、
源目录校验、目录创建、文件复制、摘要计算、清单序列化与失败后的清理；
补丁只作用于本次快照的清单写入咽喉点：

- partial 模式：open(snapshot/manifest.json.tmp, "w") 返回的文本写入
  对象在第一次 write 原样落盘（并 flush 确认部分清单内容已写出）后，
  于第二次 write 抛出 OSError("演示清单错误")，模拟清单内容部分写出
  后中断；
- replace 模式：清单临时文件完整写出并关闭后，把最终落位的
  os.replace(manifest.json.tmp, manifest.json) 替换为抛出
  OSError("演示清单错误")，模拟清单完整写出但替换失败。

两种模式在抛出异常前都会先核对两个源文件已逐字节完整复制到
SNAPSHOT/data（磁盘字节与源文件一致），再把核对结果写入证据文件，
避免父进程把参数拒绝或复制失败误判为清单回滚成功。

故障完全由下列仅在该备份子进程上设置的环境变量控制：

- MANIFEST_IOFAIL_MODE：``partial`` 或 ``replace``；
- MANIFEST_IOFAIL_PROOF：证据文件路径。注入器在确认复制完成后、抛出
  异常前把核对结果（模式、各文件相对路径、字节数、SHA-256，以及各
  模式特有的现场信息）写入该文件，供父进程确认故障确实按预期时刻
  触发。

任何前提不成立（顺序、路径、字节数不符，临时清单未完整写出，或证据
文件无法写出）都抛 RuntimeError，使子进程以非 2 的方式失败，进而让
测试失败。
"""

import builtins
import hashlib
import json
import os
import runpy
import sys
from pathlib import Path

ERROR_TEXT = "演示清单错误"

MODE_PARTIAL = "partial"
MODE_REPLACE = "replace"

MANIFEST_NAME = "manifest.json"
MANIFEST_TMP_NAME = MANIFEST_NAME + ".tmp"
DATA_DIRNAME = "data"

# 夹具源文件（相对源目录 / data 目录的 POSIX 路径）。
SOURCE_RELS = ("nested/data.bin", "note.txt")


def _verify_copies(ctx):
    """确认两个源文件均已逐字节完整复制到 data，返回证据字典。"""
    copied = {}
    for rel in SOURCE_RELS:
        data_file = ctx["data_dir"].joinpath(*rel.split("/"))
        if not data_file.is_file():
            raise RuntimeError(
                f"故障注入前提：触发清单故障前 {rel} 应已存在于快照 data 中"
            )
        actual = data_file.read_bytes()
        expected = ctx["expected"][rel]
        if actual != expected:
            raise RuntimeError(
                f"故障注入前提：触发清单故障前 {rel} 应已逐字节完整复制"
            )
        copied[rel] = {
            "size": len(actual),
            "sha256": hashlib.sha256(actual).hexdigest(),
        }
    return copied


def _write_proof(ctx, extra):
    """核对复制完成后写出证据文件，随后调用方才抛出预定的 OSError。"""
    payload = {"mode": ctx["mode"], "copied": _verify_copies(ctx)}
    payload.update(extra)
    with ctx["original_open"](ctx["proof_path"], "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


class _PartialManifestWriter:
    """清单临时文件的文本写入对象代理：首次 write 放行，第二次即故障。

    除 write 外的全部属性（flush/fileno/close 等）都原样委托给真实
    文件对象；with 语句的进入/退出同样委托，保证 backup.py 的清单
    写入流程及其上下文管理按真实路径执行。
    """

    def __init__(self, raw, ctx):
        self._raw = raw
        self._ctx = ctx
        self._writes = 0

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._raw.__exit__(*exc_info)

    def write(self, data):
        ctx = self._ctx
        self._writes += 1

        if self._writes == 1:
            # 第一部分清单内容原样写出并强制落盘，留下真实的部分清单。
            written = self._raw.write(data)
            self._raw.flush()
            size = os.fstat(self._raw.fileno()).st_size
            if size <= 0:
                raise RuntimeError("故障注入前提：部分清单内容应当实际落盘")
            ctx["partial_chars"] = len(data)
            ctx["partial_size"] = size
            return written

        if ctx["fired"]:
            raise RuntimeError("故障注入前提：受控故障只能触发一次")

        # ---- 故障触发前，先确认两个源文件均已完整复制到 data ----
        _write_proof(ctx, {
            "partial_write_chars": ctx["partial_chars"],
            "partial_write_size": ctx["partial_size"],
        })
        ctx["fired"] = True
        raise OSError(ERROR_TEXT)


def _install_partial_fault(ctx):
    """给内建 open 打补丁：仅代理清单临时文件的文本写入。"""
    original_open = builtins.open
    ctx["original_open"] = original_open
    manifest_tmp = ctx["manifest_tmp"]

    def patched_open(file, mode="r", *args, **kwargs):
        raw = original_open(file, mode, *args, **kwargs)
        # 只代理清单临时文件的文本写模式；源文件读取、data 复制等一律放行。
        if "b" in mode or not any(mark in mode for mark in ("w", "a")):
            return raw
        try:
            resolved = Path(file).resolve(strict=False)
        except (OSError, ValueError):
            return raw
        if resolved != manifest_tmp:
            return raw
        if ctx["opened"]:
            raise RuntimeError("故障注入前提：清单临时文件只应打开一次")
        ctx["opened"] = True
        return _PartialManifestWriter(raw, ctx)

    builtins.open = patched_open


def _install_replace_fault(ctx):
    """给 os.replace 打补丁：仅拦截清单临时文件到正式清单的替换。"""
    original_open = builtins.open
    ctx["original_open"] = original_open
    original_replace = os.replace
    manifest_tmp = ctx["manifest_tmp"]
    manifest_final = ctx["manifest_final"]

    def patched_replace(src, dst):
        try:
            src_resolved = Path(src).resolve(strict=False)
            dst_resolved = Path(dst).resolve(strict=False)
        except (OSError, ValueError):
            return original_replace(src, dst)
        if src_resolved != manifest_tmp or dst_resolved != manifest_final:
            return original_replace(src, dst)
        if ctx["fired"]:
            raise RuntimeError("故障注入前提：受控故障只能触发一次")

        # ---- 故障触发前，确认清单临时文件已完整写出 ----
        if not manifest_tmp.is_file():
            raise RuntimeError("故障注入前提：替换前清单临时文件应已存在")
        try:
            doc = json.loads(manifest_tmp.read_bytes().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"故障注入前提：替换前清单临时文件应是完整 JSON: {exc}"
            )
        paths = [entry.get("path") for entry in doc.get("files", [])]
        if doc.get("version") != 1 or sorted(paths) != sorted(SOURCE_RELS):
            raise RuntimeError(
                "故障注入前提：替换前清单临时文件应完整记录两个文件"
            )

        # ---- 确认两个源文件均已完整复制到 data，然后才抛出故障 ----
        _write_proof(ctx, {
            "tmp_manifest_complete": True,
            "tmp_manifest_paths": paths,
        })
        ctx["fired"] = True
        raise OSError(ERROR_TEXT)

    os.replace = patched_replace


def _install_fault(mode, source, snapshot, proof_path):
    """按模式安装清单写入故障，并预读夹具源文件字节作为核对基准。"""
    source_resolved = source.resolve(strict=True)
    snapshot_resolved = snapshot.resolve(strict=False)

    # 期望字节直接取自夹具源文件本身，避免在注入器里复制一份常量。
    expected = {}
    for rel in SOURCE_RELS:
        data = (source_resolved / rel).read_bytes()
        if not data:
            raise RuntimeError("故障注入前提：夹具源文件必须非空")
        expected[rel] = data

    ctx = {
        "mode": mode,
        "data_dir": snapshot_resolved / DATA_DIRNAME,
        "manifest_tmp": snapshot_resolved / MANIFEST_TMP_NAME,
        "manifest_final": snapshot_resolved / MANIFEST_NAME,
        "expected": expected,
        "fired": False,
        "opened": False,
        "proof_path": proof_path,
    }

    if mode == MODE_PARTIAL:
        _install_partial_fault(ctx)
    elif mode == MODE_REPLACE:
        _install_replace_fault(ctx)
    else:
        raise RuntimeError(f"未知的清单故障模式: {mode!r}")


def main():
    # 用法：python -m manifest_iofail_inject SCRIPT backup SOURCE SNAPSHOT [...]
    if len(sys.argv) < 5:
        raise RuntimeError("故障注入入口缺少 backup SOURCE SNAPSHOT 参数")
    script = sys.argv[1]
    rest = sys.argv[2:]
    if rest[0] != "backup":
        raise RuntimeError(f"故障注入入口仅用于 backup 命令，实际为 {rest[0]!r}")
    source = Path(rest[1])
    snapshot = Path(rest[2])

    mode = os.environ.get("MANIFEST_IOFAIL_MODE")
    if not mode:
        raise RuntimeError("故障注入入口缺少 MANIFEST_IOFAIL_MODE 环境变量")
    proof_path = os.environ.get("MANIFEST_IOFAIL_PROOF")
    if not proof_path:
        raise RuntimeError("故障注入入口缺少 MANIFEST_IOFAIL_PROOF 环境变量")

    _install_fault(mode, source, snapshot, proof_path)

    sys.argv = [script, *rest]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
