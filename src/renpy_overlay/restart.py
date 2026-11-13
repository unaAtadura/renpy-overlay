"""分离进程重启工具：以相同形态重新拉起自身后交由调用方退出当前实例。

职责边界：本模块只负责「命令行重建」与「分离进程启动」两件纯事件，
不 import 项目内其他模块（cli 经 stream_window 依赖 stream 包，反向
引用会成环），宿主与 cli 层均可安全依赖。

重启约定（需求 future_自动重启.txt，方案 A）：

- frozen 态（PyInstaller onedir）：``[sys.executable, *sys.argv[1:], *extra]``；
- 源码态（uv run / python -m）：``[sys.executable, "-m", "renpy_overlay",
  *sys.argv[1:], *extra]``；
- ``extra`` 恒追加在末尾：argparse 对重复参数取后者，``--pid`` /
  ``--data-dir`` 由此显式覆盖原 argv 中的同键值；
- 启动用 ``DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP``：新实例脱离本
  进程生命周期（旧实例退出不影响新实例），GUI 形态无控制台需求；
- ``Popen`` 抛任何异常都吞掉并记日志返回 False——调用方（设置窗口）以
  返回值决定「提示保留当前会话」还是「触发退出链」。
"""

from __future__ import annotations

import logging
import subprocess
import sys

logger = logging.getLogger(__name__)

#: Windows 分离启动标志：新进程组 + 无控制台继承（非 Windows 平台传 0）
_DETACHED_FLAGS = (
    (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
    if sys.platform == "win32"
    else 0
)


def build_spawn_args(extra: list[str]) -> list[str]:
    """重建等价启动命令行（纯函数，离线单测钉住两种形态）。

    ``extra`` 追加在末尾：argparse 重复参数后者生效，``--pid`` /
    ``--data-dir`` 由此显式覆盖原 argv 中的同键值（如 GUI 选择窗启动时
    原 argv 无 --pid，重启时在此补上目标进程）。
    """
    previous = list(sys.argv[1:])
    if getattr(sys, "frozen", False):  # PyInstaller 打包态：exe 即入口
        return [sys.executable, *previous, *extra]
    return [sys.executable, "-m", "renpy_overlay", *previous, *extra]


def spawn_self(extra: list[str]) -> bool:
    """分离进程拉起自身。返回 True=已启动（本进程随后可安全退出）。

    失败只记日志返回 False，不抛出——重启是可选动作，失败时调用方
    保留当前会话并提示用户即可，绝不能因重启失败把程序搞崩。
    """
    command = build_spawn_args(extra)
    try:
        subprocess.Popen(
            command,
            creationflags=_DETACHED_FLAGS,
            close_fds=True,
        )
    except Exception as exc:  # exe 缺失 / 权限不足等，保留当前会话
        logger.warning("重启拉起新实例失败：%s（命令行：%r）", exc, command)
        return False
    logger.info("已拉起新实例：%r", command)
    return True
