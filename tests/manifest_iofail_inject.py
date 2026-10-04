#!/usr/bin/env python3
"""仅供 test_backup_manifest_ioerror.py 的子进程使用的清单故障注入入口。

本身不是测试模块（文件名不以 test 开头，unittest 发现时不会导入），
也不被 backup.py 导入；测试通过

    python -m manifest_iofail_inject backup.py backup SOURCE SNAPSHOT [--checksum]

在子进程内先给清单写入路径打上受控故障补丁，再用 runpy 原样执行
backup.py 的公开命令行入口。backup.py 的清单落盘分两步：先把清单
文本写入 ``SNAPSHOT/manifest.json.tmp``，再用 os.replace 替换为正式
``manifest.json``。本注入器按环境变量选择恰好其中一个咽喉点抛出
``OSError("演示清单错误")``，除该点外不修改任何代码路径：backup.py
仍自行完成参数解析、源目录校验、目录创建、文件复制、摘要计算、清单
生成与失败后的清理。

故障完全由下列仅在该备份子进程上设置的环境变量与命令行参数控制：

- 命令行固定为 ``backup SOURCE SNAPSHOT [--checksum]``：是否带
  --checksum 决定期望清单条目是否含 sha256 字段；
- BACKUP_MANIFEST_IOFAIL_MODE：故障位置，``write`` 表示临时清单内容
  只部分写出即中断（首个字符落盘后抛出故障），``replace`` 表示临时
  清单已完整写出、在 os.replace 替换为 manifest.json 时抛出故障；
- BACKUP_MANIFEST_IOFAIL_PROOF：证据文件路径。注入器在确认下列事实
  后、抛出异常前把核对结果写入该文件，供父进程确认故障确实按预期
  时刻触发：源目录中的全部文件已逐字节完整复制到快照 data（相对
  路径、字节数、SHA-256 逐一核对），且临时清单处于所选故障位置
  要求的状态（部分写出 / 完整写出且内容与期望清单逐字节一致）。

任何前提不成立（复制未完成、临时清单状态不符、故障重复触发，或证据
文件无法写出）都抛 RuntimeError，使子进程以非 2 的方式失败，进而让
测试失败。故障只存在于该子进程内，进程结束即解除，不影响后续用例。
"""

import builtins
import hashlib
import json
import os
import runpy
import sys
from pathlib import Path

ERROR_TEXT = "演示清单错误"
MANIFEST_NAME = "manifest.json"
DATA_DIRNAME = "data"
FORMAT_VERSION = 1

MODE_WRITE = "write"
MODE_REPLACE = "replace"

PROOF_ENV = "BACKUP_MANIFEST_IOFAIL_PROOF"
MODE_ENV = "BACKUP_MANIFEST_IOFAIL_MODE"


def _collect_rel_files(root):
    """递归收集源目录中的文件，返回斜杠分隔相对路径的排序列表。"""
    collected = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        filenames.sort()
        rel_dir = Path(dirpath).relative_to(root)
        for name in filenames:
            collected.append((rel_dir / name).as_posix())
    collected.sort()
    return collected


def _expected_manifest_bytes(source_resolved, rel_paths, checksum):
    """按 backup.py 的清单格式构造期望清单的完整字节。"""
    if checksum:
        files = [
            {
                "path": rel,
                "sha256": hashlib.sha256(
                    (source_resolved / rel).read_bytes()
                ).hexdigest(),
            }
            for rel in rel_paths
        ]
    else:
        files = [{"path": rel} for rel in rel_paths]
    manifest = {"version": FORMAT_VERSION, "files": files}
    text = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    return text.encode("utf-8")


def _verify_copies(ctx):
    """确认全部源文件已逐字节完整复制到 data，返回核对结果列表。"""
    copied = []
    for rel in ctx["rel_paths"]:
        path = ctx["data_dir"] / rel
        if not path.is_file():
            raise RuntimeError(
                f"故障注入前提：触发故障前 {rel} 应已完整复制到 data"
            )
        actual = path.read_bytes()
        expected = ctx["expected_sources"][rel]
        if actual != expected:
            raise RuntimeError(
                f"故障注入前提：触发故障前 {rel} 的字节应与源文件一致"
            )
        copied.append(
            {
                "rel": rel,
                "size": len(actual),
                "sha256": hashlib.sha256(actual).hexdigest(),
            }
        )
    return copied


def _write_proof(ctx, payload):
    """用未打补丁的原始 open 落盘故障证据。"""
    with ctx["original_open"](ctx["proof_path"], "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _proof_base(ctx, copied):
    expected = ctx["expected_manifest"]
    return {
        "copied": copied,
        "expected_manifest_size": len(expected),
        "expected_manifest_sha256": hashlib.sha256(expected).hexdigest(),
    }


class _PartialManifestWriter:
    """临时清单写入对象代理：首个字符落盘后抛出受控故障。

    除 write 外的全部属性（flush/fileno/close 等）都原样委托给真实
    文件对象；with 语句的进入/退出同样委托，保证 backup.py 的清单
    写入及其上下文管理按真实流程执行。
    """

    def __init__(self, raw, ctx):
        self._raw = raw
        self._ctx = ctx

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._raw.__exit__(*exc_info)

    def write(self, data):
        ctx = self._ctx
        if ctx["fired"]:
            raise RuntimeError("故障注入前提：受控故障只能触发一次")
        if not data:
            raise RuntimeError("故障注入前提：清单写入应为非空内容")

        # ---- 只落盘首个字符，制造“清单内容部分写出”的状态 ----
        written = self._raw.write(data[:1])
        if written != 1:
            raise RuntimeError("故障注入前提：首个字符应当实际写入")
        self._raw.flush()
        if os.fstat(self._raw.fileno()).st_size != 1:
            raise RuntimeError("故障注入前提：故障前临时清单应恰好部分写出一个字节")

        # ---- 故障触发前，确认全部源文件已完整复制到 data ----
        copied = _verify_copies(ctx)
        payload = _proof_base(ctx, copied)
        payload["mode"] = MODE_WRITE
        payload["tmp_size"] = 1
        _write_proof(ctx, payload)

        ctx["fired"] = True
        raise OSError(ERROR_TEXT)


def _install_fault(source, snapshot, checksum, mode, proof_path):
    """按模式给清单写入路径打补丁：write 拦 open，replace 拦 os.replace。"""
    source_resolved = source.resolve(strict=True)
    snapshot_resolved = snapshot.resolve(strict=False)
    data_resolved = snapshot_resolved / DATA_DIRNAME
    tmp_path = snapshot_resolved / (MANIFEST_NAME + ".tmp")
    final_path = snapshot_resolved / MANIFEST_NAME

    rel_paths = _collect_rel_files(source_resolved)
    if not rel_paths:
        raise RuntimeError("故障注入前提：源目录中应至少有一个普通文件")
    expected_sources = {
        rel: (source_resolved / rel).read_bytes() for rel in rel_paths
    }
    expected_manifest = _expected_manifest_bytes(
        source_resolved, rel_paths, checksum
    )

    original_open = builtins.open
    ctx = {
        "mode": mode,
        "data_dir": data_resolved,
        "tmp_path": tmp_path,
        "final_path": final_path,
        "rel_paths": rel_paths,
        "expected_sources": expected_sources,
        "expected_manifest": expected_manifest,
        "fired": False,
        "proof_path": proof_path,
        "original_open": original_open,
    }

    if mode == MODE_WRITE:

        def patched_open(file, open_mode="r", *args, **kwargs):
            raw = original_open(file, open_mode, *args, **kwargs)
            # 只代理临时清单的文本写入；源文件读取等其他打开均原样放行。
            if "w" not in open_mode or "b" in open_mode:
                return raw
            try:
                resolved = Path(file).resolve(strict=False)
            except (OSError, ValueError):
                return raw
            if resolved != tmp_path:
                return raw
            return _PartialManifestWriter(raw, ctx)

        builtins.open = patched_open

    elif mode == MODE_REPLACE:
        original_replace = os.replace

        def patched_replace(src, dst, *args, **kwargs):
            try:
                src_resolved = Path(src).resolve(strict=False)
                dst_resolved = Path(dst).resolve(strict=False)
            except (OSError, ValueError):
                return original_replace(src, dst, *args, **kwargs)
            if src_resolved != tmp_path or dst_resolved != final_path:
                return original_replace(src, dst, *args, **kwargs)
            if ctx["fired"]:
                raise RuntimeError("故障注入前提：受控故障只能触发一次")

            # ---- 故障触发前，确认临时清单已完整写出 ----
            if not tmp_path.is_file():
                raise RuntimeError("故障注入前提：替换前临时清单应已存在")
            actual = tmp_path.read_bytes()
            if actual != expected_manifest:
                raise RuntimeError(
                    "故障注入前提：替换前临时清单应已逐字节完整写出"
                )

            # ---- 同时确认全部源文件已完整复制到 data ----
            copied = _verify_copies(ctx)
            payload = _proof_base(ctx, copied)
            payload["mode"] = MODE_REPLACE
            payload["tmp_size"] = len(actual)
            payload["tmp_sha256"] = hashlib.sha256(actual).hexdigest()
            _write_proof(ctx, payload)

            ctx["fired"] = True
            raise OSError(ERROR_TEXT)

        os.replace = patched_replace

    else:
        raise RuntimeError(f"未知的故障模式: {mode!r}")


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
    checksum = "--checksum" in rest[3:]

    proof_path = os.environ.get(PROOF_ENV)
    if not proof_path:
        raise RuntimeError(f"故障注入入口缺少 {PROOF_ENV} 环境变量")
    mode = os.environ.get(MODE_ENV)
    if mode not in (MODE_WRITE, MODE_REPLACE):
        raise RuntimeError(
            f"故障注入入口的 {MODE_ENV} 应为 {MODE_WRITE} 或 {MODE_REPLACE}"
        )

    _install_fault(source, snapshot, checksum, mode, proof_path)

    sys.argv = [script, *rest]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
