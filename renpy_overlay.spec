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
   win32gui、win32process) / tkinter / sqlite3 / PyQt6（流式悬浮窗）/
   requests+websocket（实时识别 dashscope 链路，见 asr_rt），其余大体积库
   一律排除；
4. Analysis 后按名单过滤 datas / binaries，剔除非必需的静态资源与二进制
   （Qt 翻译文件、Tcl 时区库、软件渲染 OpenGL、Qt PDF、罕见图像格式插件），
   这些在运行链上均无引用；
5. 单文件（onefile）模式：EXE 直接收全部 binaries 与 datas。若想要启动更快的
   目录版，把 EXE 的 ``a.binaries`` / ``a.datas`` 换成 ``exclude_binaries=True``，
   并追加 ``COLLECT(exe, a.binaries, a.datas, name="renpy-overlay")`` 即可。

构建命令（产物在 dist/renpy-overlay.exe）：::

    uv run pyinstaller renpy_overlay.spec --noconfirm
"""

import os

ROOT = SPECPATH  # noqa: F821 - PyInstaller 注入：spec 文件所在目录
SRC = os.path.join(ROOT, "src")

# 保险清单：本工具运行链不依赖这些库，防止将来被间接引入而撑大体积
# （注：numpy / aiohttp 是听歌识曲 shazamio 链路（song_recognition 包）的
# 真实运行依赖，PIL 是截图翻译链路（screenshot 包）的真实运行依赖，
# requests / websocket 是实时识别 dashscope 链路（video/asr_rt）的真实
# 运行依赖，均不在此列；scipy / pandas 等仍未使用）
EXCLUDES = [
    # 科学计算 / 图像（PIL / numpy 已被真实运行链引入，不能排除）
    "scipy", "pandas", "matplotlib", "sympy", "cv2", "torch",
    # 其它 GUI 框架（悬浮窗用 PyQt6，内置 hook 收集；选择窗用 tkinter）
    "PyQt5", "PySide2", "PySide6", "wx",
    # 交互式环境与网络库（翻译走标准库 urllib；aiohttp 已被 shazamio 引入；
    # requests / urllib3 已被 dashscope 引入，不能排除）
    "IPython", "jupyter", "notebook", "httpx",
    "lxml", "bs4",
    # 测试 / 构建工具与标准库测试包
    "pytest", "_pytest", "setuptools", "pkg_resources", "pip", "wheel",
    "test", "tkinter.test",
    # 无引用链的标准库（被第三方库或钩子连带收集，运行链不使用）
    "unittest", "pydoc", "pydoc_data", "doctest", "tarfile", "xmlrpc",
    "tty", "socketserver",
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
        # 绑定窗口入口图标（SVG）：QIcon 按相对包路径加载
        (
            os.path.join(SRC, "renpy_overlay", "assets"),
            os.path.join("renpy_overlay", "assets"),
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

# ---- 非必需 datas / binaries 过滤：只保留运行链真正会加载的资源 ----

def _norm(name: str) -> str:
    """统一正斜杠，便于按相对路径前缀/后缀匹配。"""
    return name.replace("\\", "/")

# Qt 翻译文件（~180 个 .qm）：界面文案硬编码中文，不加载 Qt 翻译系统
a.datas = [entry for entry in a.datas if not _norm(entry[0]).endswith(".qm")]

# Tcl 时区数据（tzdata ~600 个文件）：仅 Tcl clock 命令的时区库，
# tkinter 目录/文件选择对话框用不到
a.datas = [
    entry for entry in a.datas
    if "/_tcl_data/tzdata/" not in _norm(entry[0])
]

# 二进制剔除名单（basename）：
# - opengl32sw：软件渲染 OpenGL 光栅器（~20MB），悬浮窗全为 QWidget+QPainter
#   绘制，走系统 OpenGL/RHI，不经过软件光栅器；
# - Qt6Pdf + qpdf 插件：程序不加载 PDF 图像；
# - qtiff/qtga/qwbmp/qicns：罕见图像格式插件（png/jpg/webp/svg/ico 为内建
#   或保留插件，assets 的 SVG 图标依赖 qsvgicon + Qt6Svg，均保留）。
_DROP_BINARIES = {
    "opengl32sw.dll", "qt6pdf.dll", "qpdf.dll",
    "qtiff.dll", "qtga.dll", "qwbmp.dll", "qicns.dll",
}
a.binaries = [
    entry for entry in a.binaries
    if _norm(entry[0]).rsplit("/", 1)[-1].lower() not in _DROP_BINARIES
]

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
