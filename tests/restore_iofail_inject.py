#!/usr/bin/env python3
"""仅供 test_restore_copy_ioerror.py 的子进程使用的复制故障注入入口。

本身不是测试模块（文件名不以 test 开头，unittest 发现时不会导入），
也不被 backup.py 导入；测试通过

    python -m restore_iofail_inject backup.py restore SNAPSHOT DEST

在子进程内先给 shutil.copyfileobj 打上受控故障补丁，再用 runpy 原样
执行 backup.py 的公开命令行入口。除补丁外不修改任何代码路径：backup.py
仍自行完成参数解析、清单校验、目录创建、复制与失败后的清理。

故障完全由下列仅在恢复子进程上设置的环境变量控制；缺省时补丁不安装，
backup.py 逐字节按原始流程执行：

- RESTORE_IOFAIL_TARGET：允许注入的恢复目标绝对路径；只有写入解析后
  位于该目录之内的文件才可能触发（快照数据等其他写入不受影响）。
- RESTORE_IOFAIL_STAGE：
  - first：恢复顺序中的首个文件（a.txt）写入部分字节后失败；
  - second：a.txt 完整复制，nested/b.bin 写入部分字节后失败。

故障一律为 OSError("演示复制错误")，且仅在确认目标文件确实存在、
已有非空内容（write+flush 后 fstat 核对）之后抛出。
"""

import os
import runpy
import shutil
import sys
from pathlib import Path

ERROR_TEXT = "演示复制错误"

FIRST_FAIL_REL = "a.txt"
SECOND_FAIL_REL = "nested/b.bin"
SECOND_PASSED_REL = "a.txt"


def _resolved(path_value):
    return Path(path_value).resolve(strict=False)


def _rel_inside(dst_file, target_resolved):
    """返回 dst_file 相对目标目录的 POSIX 路径；不属于目标目录则 None。"""
    try:
        name = getattr(dst_file, "name", None)
        if not isinstance(name, str):
            return None
        return _resolved(name).relative_to(target_resolved).as_posix()
    except (TypeError, ValueError, OSError):
        return None


def _write_partial_then_fail(fsrc, fdst):
    """只复制源的首个字节，确认目标已有非空内容后抛出受控 OSError。"""
    chunk = fsrc.read(1)
    if not chunk:
        # 夹具前提是两个非空文件；读不到字节说明夹具不再成立。
        raise RuntimeError("故障注入前提：被复制文件应为非空文件")
    fdst.write(chunk)
    # flush 确保字节已离开 Python 缓冲区进入内核，fstat 才能反映真实大小。
    fdst.flush()
    st = os.fstat(fdst.fileno())
    if st.st_size <= 0:
        # 触发前必须确认目标文件确实存在且已有非空内容。
        raise RuntimeError("故障注入前提：目标文件在故障前应已有非空内容")
    raise OSError(ERROR_TEXT)


def _install_fault(target_arg, stage):
    target_resolved = _resolved(target_arg)
    original_copyfileobj = shutil.copyfileobj
    state = {"active": True}

    def patched_copyfileobj(fsrc, fdst, length=0):
        rel = _rel_inside(fdst, target_resolved)
        if not state["active"] or rel is None:
            return original_copyfileobj(fsrc, fdst, length)

        if stage == "first":
            # 恢复按清单顺序进行，首个文件必须是 a.txt。
            if rel != FIRST_FAIL_REL:
                raise RuntimeError(
                    f"故障注入前提：首个复制的文件应为 {FIRST_FAIL_REL}，"
                    f"实际为 {rel}"
                )
            state["active"] = False
            _write_partial_then_fail(fsrc, fdst)
        elif stage == "second":
            # a.txt 必须完整恢复；其后的嵌套二进制文件写入部分字节时失败。
            if rel == SECOND_PASSED_REL:
                return original_copyfileobj(fsrc, fdst, length)
            if rel != SECOND_FAIL_REL:
                raise RuntimeError(
                    f"故障注入前提：第二个复制的文件应为 {SECOND_FAIL_REL}，"
                    f"实际为 {rel}"
                )
            state["active"] = False
            _write_partial_then_fail(fsrc, fdst)
        else:
            raise RuntimeError(f"未知的故障注入阶段: {stage!r}")

        # 上述分支要么返回要么抛出；到此说明补丁逻辑本身有误。
        raise RuntimeError("故障注入逻辑不应到达此处")

    shutil.copyfileobj = patched_copyfileobj


def main():
    target = os.environ.get("RESTORE_IOFAIL_TARGET")
    stage = os.environ.get("RESTORE_IOFAIL_STAGE")
    if target and stage:
        _install_fault(target, stage)

    # 用法：python -m restore_iofail_inject SCRIPT [原样透传参数...]
    if len(sys.argv) < 2:
        raise RuntimeError("故障注入入口缺少要执行的脚本路径")
    script = sys.argv[1]
    sys.argv = [script, *sys.argv[2:]]
    runpy.run_path(script, run_name="__main__")


if __name__ == "__main__":
    main()
