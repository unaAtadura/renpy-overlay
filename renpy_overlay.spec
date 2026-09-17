# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：生成单文件 exe，且只打包运行必需的模块。

要点说明：

1. 入口用根目录的 ``pyinstaller_entry.py``（绝对导入）—— PyInstaller 把入口
   脚本当作顶层 ``__main__`` 执行，包内的 ``__main__.py`` 因相对导入不可用；
2. ``payload/agent.py`` 是注入到游戏进程内执行的源码，经
   ``importlib.resources`` 读成字节，静态分析发现不了它，必须显式放进
   ``datas``；同时 ``renpy_overlay.payload`` 要进 ``hiddenimports``
   （``files("renpy_overlay.payload")`` 会先 import 该包再定位资源）；
3. ``excludes`` 只做保险：运行链上真实依赖仅 psutil / pywin32(win32con、
   win32gui、win32process) / tkinter / sqlite3 / PyQt6（流式悬浮窗），
   其余大体积库一律排除；
4. 单文件（onefile）模式：EXE 直接收全部 binaries 与 datas。若想要启动更快的
   目录版，把 EXE 的 ``a.binaries`` / ``a.datas`` 换成 ``exclude_binaries=True``，
   并追加 ``COLLECT(exe, a.binaries, a.datas, name="renpy-overlay")`` 即可。

构建命令（产物在 dist/renpy-overlay.exe）：::

    uv run pyinstaller renpy_overlay.spec --noconfirm
"""

import os

ROOT = SPECPATH  # noqa: F821 - PyInstaller 注入：spec 文件所在目录
SRC = os.path.join(ROOT, "src")

# 保险清单：本工具运行链不依赖这些库，防止将来被间接引入而撑大体积
EXCLUDES = [
    # 科学计算 / 图像
    "numpy", "scipy", "pandas", "matplotlib", "sympy", "PIL", "cv2", "torch",
    # 其它 GUI 框架（悬浮窗用 PyQt6，内置 hook 收集；选择窗用 tkinter）
    "PyQt5", "PySide2", "PySide6", "wx",
    # 交互式环境与网络库（翻译走标准库 urllib）
    "IPython", "jupyter", "notebook", "requests", "urllib3", "aiohttp", "httpx",
    "lxml", "bs4",
    # 测试 / 构建工具与标准库测试包
    "pytest", "_pytest", "setuptools", "pkg_resources", "pip", "wheel",
    "test", "tkinter.test",
]

a = Analysis(  # noqa: F821 - PyInstaller 注入
    [os.path.join(ROOT, "pyinstaller_entry.py")],
    pathex=[SRC],
    binaries=[],
    datas=[
        # 注入负载源码：运行时按 renpy_overlay/payload/agent.py 的路径读取
        (
            os.path.join(SRC, "renpy_overlay", "payload", "agent.py"),
            os.path.join("renpy_overlay", "payload"),
        ),
    ],
    hiddenimports=[
        "renpy_overlay.payload",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821 - PyInstaller 注入

exe = EXE(  # noqa: F821 - PyInstaller 注入
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="renpy-overlay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 不启用 UPX：压缩会加剧杀软对远程注入工具的误报
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,  # 保留控制台：命令行参数、日志输出与控制台命令都依赖它
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
