#!/usr/bin/env python3
"""仅供 test_backup_copy_ioerror.py 的子进程使用的复制故障注入入口。

本身不是测试模块（文件名不以 test 开头，unittest 发现时不会导入），
也不被 backup.py 导入；测试通过

    python -m backup_iofail_inject backup.py backup SOURCE SNAPSHOT [--checksum]

在子进程内先给内建 open 打上受控故障补丁，再用 runpy 原样执行
backup.py 的公开命令行入口。普通备份的 _copy_bytes 经由
shutil.copyfileobj 调用 fdst.write，--checksum 备份的
_copy_bytes_and_hash 自行调用 dst_f.write；两条复制路径共同的写入
咽喉点就是 open(..., "wb") 返回的文件对象，因此补丁在这一层拦截，
除写入外不修改任何代码路径：backup.py 仍自行完成参数解析、源目录
校验、清单生成、目录创建、复制与失败后的清理。

故障完全由下列仅在该备份子进程上设置的环境变量与命令行参数控制：

- 命令行固定为 ``backup SOURCE SNAPSHOT ...``：只对写入解析后位于
  ``SNAPSHOT/data`` 之内的普通文件生效，清单等其他写入不受影响；
- BACKUP_IOFAIL_PROOF：证据文件路径。注入器在确认下列事实后、抛出
  异常前把核对结果（相对路径、字节数、已完成文件的 SHA-256）写入该
  文件，供父进程确认故障确实按预期时刻触发。

故障场景固定：a.txt 必须已完整写入快照（磁盘字节与源文件逐字节
一致），随后 nested/b.bin 的目标文件只写入源文件的首个字节，在
flush + fstat 确认该字节落盘后抛出 OSError("演示复制错误")。
任何前提不成立（顺序、路径、字节数不符，或证据文件无法写出）都抛
RuntimeError，使子进程以非 2 的方式失败，进而让测试失败。
"""

import builtins
import hashlib
import json
import os
import runpy
import sys
from pathlib import Path

ERROR_TEXT = "演示复制错误"

# 先完整复制的文件与随后部分写入即失败的文件（相对 data 目录）。
PASSED_REL = "a.txt"
FAIL_REL = "nested/b.bin"


def _rel_inside(resolved, base):
    """返回 resolved 相对 base 的 POSIX 路径；不属于 base 则 None。"""
    try:
        return resolved.relative_to(base).as_posix()
    except ValueError:
        return None


class _FaultWriter:
    """对 data 目录内目标文件的写入对象代理：拦截 write 安装受控故障。

    除 write 外的全部属性（flush/fileno/close 等）都原样委托给真实
    文件对象；with 语句的进入/退出同样委托，保证 backup.py 的复制
    函数及其上下文管理按真实流程执行。
    """

    def __init__(self, raw, rel, ctx):
        self._raw = raw
        self._rel = rel
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

        if self._rel == PASSED_REL:
            # a.txt 原样放行，只累计写入字节供故障时刻核对。
            written = self._raw.write(data)
            ctx["passed_bytes"] += data
            return written

        if self._rel != FAIL_REL:
            # 夹具中 data 下只会有这两个普通文件。
            raise RuntimeError(f"故障注入前提：出现未预期的复制目标 {self._rel}")

        if ctx["fired"]:
            raise RuntimeError("故障注入前提：受控故障只能触发一次")

        # ---- 故障触发前，先确认 a.txt 已完整复制 ----
        expected_passed = ctx["expected_passed"]
        if ctx["passed_bytes"] != expected_passed:
            raise RuntimeError(
                "故障注入前提：触发故障时 a.txt 的写入字节与源文件不一致"
            )
        a_path = ctx["data_dir"] / PASSED_REL
        if not a_path.is_file():
            raise RuntimeError("故障注入前提：故障前 a.txt 应当已存在于快照中")
        actual_passed = a_path.read_bytes()
        if actual_passed != expected_passed:
            raise RuntimeError(
                "故障注入前提：故障前 a.txt 应当已逐字节完整写入"
            )

        # ---- 让 nested/b.bin 只落盘源文件的首个字节 ----
        if not data:
            raise RuntimeError("故障注入前提：被复制文件应为非空文件")
        first = data[:1]
        if first != ctx["expected_first"]:
            raise RuntimeError(
                "故障注入前提：nested/b.bin 写入的首个字节与源文件不一致"
            )
        written = self._raw.write(first)
        if written != 1:
            raise RuntimeError("故障注入前提：首个字节应当实际写入")
        self._raw.flush()
        if os.fstat(self._raw.fileno()).st_size != 1:
            raise RuntimeError("故障注入前提：故障前目标文件应恰好有一个字节")

        # ---- 核对完成，留下证据后才抛出预定的 OSError ----
        payload = {
            "completed": {
                "rel": PASSED_REL,
                "size": len(actual_passed),
                "sha256": hashlib.sha256(actual_passed).hexdigest(),
            },
            "partial": {
                "rel": FAIL_REL,
                "size": 1,
                "first_byte_hex": first.hex(),
            },
        }
        with ctx["original_open"](ctx["proof_path"], "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)

        ctx["fired"] = True
        raise OSError(ERROR_TEXT)


def _install_fault(source, snapshot, proof_path):
    """给内建 open 打补丁：仅代理 data 目录内的二进制写入文件。"""
    source_resolved = source.resolve(strict=True)
    data_resolved = (snapshot.resolve(strict=False) / "data")

    # 期望字节直接取自夹具源文件本身，避免在注入器里复制一份常量。
    expected_passed = (source_resolved / PASSED_REL).read_bytes()
    expected_first = (source_resolved / FAIL_REL).read_bytes()[:1]
    if not expected_passed or not expected_first:
        raise RuntimeError("故障注入前提：两个夹具源文件都必须非空")

    original_open = builtins.open
    ctx = {
        "data_dir": data_resolved,
        "expected_passed": expected_passed,
        "expected_first": expected_first,
        "passed_bytes": b"",
        "fired": False,
        "proof_path": proof_path,
        "original_open": original_open,
    }

    def patched_open(file, mode="r", *args, **kwargs):
        raw = original_open(file, mode, *args, **kwargs)
        # 只代理二进制写/追加模式；源文件读取、脚本读取、清单写入均原样放行。
        if "b" not in mode or not any(mark in mode for mark in ("w", "a")):
            return raw
        try:
            rel = _rel_inside(Path(file).resolve(strict=False), data_resolved)
        except (OSError, ValueError):
            rel = None
        if rel is None:
            return raw
        return _FaultWriter(raw, rel, ctx)

    builtins.open = patched_open


def main():
    # 用法：python -m backup_iofail_inject SCRIPT backup SOURCE SNAPSHOT [...]
    if len(sys.argv) < 5:
        raise RuntimeError("故障注入入口缺少 backup SOURCE SNAPSHOT 参数")
    script = sys.argv[1]
    rest = sys.argv[2:]
    if rest[0] != "backup":
        raise RuntimeError(f"故障注入入口仅用于 backup 命令，实际为 {rest[0]!r}")
    source = Path(rest[1])
    snapshot = Path(rest[2])

    proof_path = os.environ.get("BACKUP_IOFAIL_PROOF")
    if not proof_path:
        raise RuntimeError("故障注入入口缺少 BACKUP_IOFAIL_PROOF 环境变量")

    _install_fault(source, snapshot, proof_path)

    sys.argv = [script, *rest]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
