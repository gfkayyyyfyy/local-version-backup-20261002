#!/usr/bin/env python3
"""仅供 test_backup_copy_ioerror.py 的子进程使用的复制故障注入入口。

本身不是测试模块（文件名不以 test 开头，unittest 发现时不会导入），
也不被 backup.py 导入；测试通过

    python -m backup_iofail_inject backup.py backup SOURCE SNAPSHOT [--checksum]

在子进程内先给内建 open 打上受控故障补丁，再用 runpy 原样执行 backup.py
的公开命令行入口。除补丁外不修改任何代码路径：backup.py 仍自行完成参数
解析、源目录校验、快照目录创建、复制与失败后的清理。

普通备份经 shutil.copyfileobj 复制，--checksum 备份经
_copy_bytes_and_hash 自有的读写循环复制；两者都以 open(dst, "wb") 打开
快照内的目标文件，因此补丁包装该目标文件对象即可用同一装置覆盖两种模式。

故障完全由下列仅在备份子进程上设置的环境变量控制；缺省时补丁不安装，
backup.py 逐字节按原始流程执行：

- BACKUP_IOFAIL_SNAPSHOT：本次快照目录的绝对路径；只有写入解析后等于
  其 data/nested/b.bin 的文件才触发（源文件、清单等其他打开不受影响）。
- BACKUP_IOFAIL_EVIDENCE：可选的证据文件路径；触发前把确认结果写入，
  供测试进程核对“完整复制与部分写入确实发生，随后才发生预定错误”。

触发前提（不满足则抛 RuntimeError，使测试失败）：

- 打开 nested/b.bin 的目标文件时，data/a.txt 必须已存在且字节与夹具
  完全一致（证明首个文件已完整写入快照）；
- 目标文件写入首个字节并 flush 后，fstat 必须确认已有非空内容
  （证明部分写入确实发生）。

两项确认通过后抛出 OSError("演示复制错误")。
"""

import builtins
import os
import runpy
import sys
from pathlib import Path

ERROR_TEXT = "演示复制错误"

# 夹具固定形态（与 test_backup_copy_ioerror.py 中的源目录一致）。
PASSED_REL = "a.txt"
PASSED_BYTES = bytes([0x68, 0x69, 0x0A])
FAIL_REL = "nested/b.bin"

EVIDENCE_PASSED = f"完整复制确认: {PASSED_REL}"
EVIDENCE_PARTIAL = f"部分写入确认: {FAIL_REL} 已写入 1 字节"

_real_open = builtins.open


def _resolved(path_value):
    return Path(path_value).resolve(strict=False)


def _append_evidence(evidence_path, line):
    with _real_open(evidence_path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


class _PartialThenFailWriter:
    """包装目标文件对象：首次 write 只落盘首个字节，确认后抛出受控 OSError。"""

    def __init__(self, raw, evidence_path):
        self._raw = raw
        self._evidence_path = evidence_path
        self._triggered = False

    def write(self, data):
        if self._triggered:
            raise OSError(ERROR_TEXT)
        self._triggered = True
        head = bytes(data[:1])
        if not head:
            # 夹具前提是被复制文件非空；读不到字节说明夹具不再成立。
            raise RuntimeError("故障注入前提：被复制文件应为非空文件")
        self._raw.write(head)
        # flush 确保字节已离开 Python 缓冲区进入内核，fstat 才能反映真实大小。
        self._raw.flush()
        if os.fstat(self._raw.fileno()).st_size <= 0:
            raise RuntimeError("故障注入前提：目标文件在故障前应已有非空内容")
        if self._evidence_path:
            _append_evidence(self._evidence_path, EVIDENCE_PARTIAL)
        raise OSError(ERROR_TEXT)

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc_info):
        return self._raw.__exit__(*exc_info)


def _install_fault(snapshot_arg, evidence_path):
    snapshot_resolved = _resolved(snapshot_arg)
    data_dir = snapshot_resolved / "data"
    fail_path = data_dir.joinpath(*FAIL_REL.split("/"))
    passed_path = data_dir.joinpath(*PASSED_REL.split("/"))
    state = {"active": True}

    def patched_open(file, mode="r", *args, **kwargs):
        if (
            state["active"]
            and isinstance(file, (str, os.PathLike))
            and "w" in mode
            and "b" in mode
            and _resolved(file) == fail_path
        ):
            # 备份按排序后的相对路径逐个复制：打开 nested/b.bin 的目标时，
            # a.txt 必须已经完整写入快照，否则说明处理流程或夹具已变化。
            try:
                actual = passed_path.read_bytes()
            except OSError as exc:
                raise RuntimeError(
                    f"故障注入前提：触发前 {PASSED_REL} 应已完整写入快照: {exc}"
                )
            if actual != PASSED_BYTES:
                raise RuntimeError(
                    f"故障注入前提：触发前 {PASSED_REL} 字节应为 "
                    f"{PASSED_BYTES.hex()}，实际为 {actual.hex()}"
                )
            if evidence_path:
                _append_evidence(evidence_path, EVIDENCE_PASSED)
            state["active"] = False
            return _PartialThenFailWriter(
                _real_open(file, mode, *args, **kwargs), evidence_path
            )
        return _real_open(file, mode, *args, **kwargs)

    builtins.open = patched_open


def main():
    snapshot = os.environ.get("BACKUP_IOFAIL_SNAPSHOT")
    if snapshot:
        _install_fault(snapshot, os.environ.get("BACKUP_IOFAIL_EVIDENCE"))

    # 用法：python -m backup_iofail_inject SCRIPT [原样透传参数...]
    if len(sys.argv) < 2:
        raise RuntimeError("故障注入入口缺少要执行的脚本路径")
    script = sys.argv[1]
    sys.argv = [script, *sys.argv[2:]]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
