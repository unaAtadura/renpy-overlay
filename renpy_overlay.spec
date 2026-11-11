# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：默认目录版（onedir，启动更快），只打包运行必需的模块。

要点说明：

1. 入口用根目录的 ``pyinstaller_entry.py``（绝对导入）—— PyInstaller 把入口
   脚本当作顶层 ``__main__`` 执行，包内的 ``__main__.py`` 因相对导入不可用；
2. ``payload/agent.py`` 是注入到游戏进程内执行的源码，经
   ``importlib.resources`` 读成字节，静态分析发现不了它，必须显式放进
   ``datas``；同时 ``renpy_overlay.payload`` 要进 ``hiddenimports``
   （``files("renpy_overlay.payload")`` 会先 import 该包再定位资源）；
3. ``excludes`` 只做保险：运行链上真实依赖仅 psutil / pywin32(win32con、
   win32gui、win32process) / sqlite3 / PyQt6（悬浮窗与进程选择窗）/
   requests+websocket（实时识别 dashscope 链路，见 asr_rt），其余大体积库
   一律排除；
4. Analysis 后按名单过滤 datas / binaries，剔除非必需的静态资源与二进制
   （Qt 翻译文件、Tcl 时区库、软件渲染 OpenGL、Qt PDF、罕见图像格式插件），
   这些在运行链上均无引用；
5. 目录版（onedir，默认）：EXE 用 ``exclude_binaries=True`` 只保留引导与
   元信息，binaries / datas 经 ``COLLECT`` 收集到 ``dist/renpy-overlay/``
   （免每次启动解压，冷启动显著快于单文件版）；单文件版（onefile）由
   顶部 ``ONEFILE = True`` 切换保留为备选——EXE 直接收全部 binaries 与
   datas（每次启动解压到临时目录，分发为单个 exe）。两种模式共用同一套
   Analysis / 过滤 / datas 配置，资源定位同构（frozen 运行时
   ``sys._MEIPASS`` 分别指向 _MEI 临时目录与 _internal 目录）。

构建命令（目录版产物：dist/renpy-overlay/renpy-overlay.exe + _internal/；
单文件版产物：dist/renpy-overlay.exe）：::

    uv run pyinstaller renpy_overlay.spec --noconfirm
"""

import os
import sysconfig

ROOT = SPECPATH  # noqa: F821 - PyInstaller 注入：spec 文件所在目录
SRC = os.path.join(ROOT, "src")

# SDL2 本体（video/clip/player 播放引擎）：pysdl2-dll 无 PyInstaller hook，其
# sdl2dll/dll 目录下的 DLL 不会自动收集，必须手动加入 binaries；
# 运行链仅用 SDL2 本体（render/audio/events），SDL2_image/mixer/ttf/gfx
# 扩展及其第三方依赖链（libwebp、libogg 等）不被使用，不打包
SDL2_BINARIES = [
    (
        os.path.join(sysconfig.get_paths()["purelib"], "sdl2dll", "dll", "SDL2.dll"),
        ".",
    ),
]

# 保险清单：本工具运行链不依赖这些库，防止将来被间接引入而撑大体积
# （注：numpy / aiohttp 是听歌识曲 shazamio 链路（song_recognition 包）的
# 真实运行依赖，PIL 是截图翻译链路（screenshot 包）的真实运行依赖，
# requests / websocket 是实时识别 dashscope 链路（video/asr_rt）的真实
# 运行依赖，均不在此列；scipy / pandas 等仍未使用）
EXCLUDES = [
    # 科学计算 / 图像（PIL / numpy 已被真实运行链引入，不能排除）
    "scipy", "pandas", "matplotlib", "sympy", "cv2", "torch",
    # 其它 GUI 框架（悬浮窗与进程选择窗统一用 PyQt6，内置 hook 收集；
    # tkinter 已移出运行链，排除以防将来被连带引入）
    "PyQt5", "PySide2", "PySide6", "wx", "tkinter",
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
    # dashscope 主链软依赖：导入实验验证（屏蔽后
    # ``from dashscope.audio.asr import Recognition`` 导入链完好），
    # 仅 streaming / agent / cli 等可选子模块使用；
    # cryptography / aiohttp / requests 则是主链硬依赖，不可排除
    "rich", "pygments",
]

a = Analysis(  # noqa: F821 - PyInstaller 注入
    [os.path.join(ROOT, "pyinstaller_entry.py")],
    pathex=[SRC],
    binaries=SDL2_BINARIES,
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

# Tcl 时区数据（tzdata ~600 个文件）：tkinter 已移出运行链（进程选择窗改用
# PyQt6），此过滤仅为防连带收集的保险。（前导加 / 统一为绝对形态，
# 避免清单首段无斜杠时前缀匹配漏网）
a.datas = [
    entry for entry in a.datas
    if "/_tcl_data/tzdata/" not in "/" + _norm(entry[0])
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

#: 打包形态开关：False = 目录版 onedir（默认，启动更快）；
#: True = 单文件 onefile（备选：分发为单个 exe，每次启动解压到临时目录）
ONEFILE = False

# 两种模式一致的 EXE 选项（console / upx / name 等既有取值保持不变）
_EXE_OPTIONS = dict(
    name="renpy-overlay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # 不启用 UPX：压缩会加剧杀软对远程注入工具的误报
    upx_exclude=[],
    console=True,  # 保留控制台：命令行参数、日志输出与控制台命令都依赖它
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)

if ONEFILE:
    # 单文件版（备选）：EXE 直接收全部 binaries 与 datas，
    # 每次启动解压到 %TEMP%/_MEIxxxx 临时目录
    exe = EXE(  # noqa: F821 - PyInstaller 注入
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        runtime_tmpdir=None,
        **_EXE_OPTIONS,
    )
else:
    # 目录版（默认）：EXE 只保留引导与元信息，binaries / datas 由
    # COLLECT 统一收集到 dist/renpy-overlay/（exe 与 _internal/ 同级）
    exe = EXE(  # noqa: F821 - PyInstaller 注入
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        **_EXE_OPTIONS,
    )
    coll = COLLECT(  # noqa: F821 - PyInstaller 注入
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name="renpy-overlay",
    )
