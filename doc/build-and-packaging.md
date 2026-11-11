# 构建与打包

对应源码/配置：`pyproject.toml`、`uv.lock`、`renpy_overlay.spec`、`pyinstaller_entry.py`、`__main__.py`。测试：无（构建配置；`test_cli.py` 验证入口行为）。

## 1. 开发环境与依赖（pyproject.toml）

- 工具端 Python ≥ 3.10（`.python-version` 指定）；包管理用 [uv](https://docs.astral.sh/uv/)：`uv sync` 创建虚拟环境并安装依赖；
- 默认走清华 TUNA 镜像（`[[tool.uv.index]]`，官方源在本机下载大体积 wheel 时不稳定）；换源可改该配置或设环境变量 `UV_DEFAULT_INDEX`；
- 入口脚本：`renpy-overlay = "renpy_overlay.cli:main"`（`uv run renpy-overlay`）；另支持 `python -m renpy_overlay`（`__main__.py`）；
- 运行依赖与选型说明：

| 依赖 | 版本下限 | 用途 / 备注 |
| --- | --- | --- |
| psutil | ≥6.1 | 进程枚举 / 目标存活检测 |
| pillow | ≥10.0 | 截屏与图像处理（仅 screenshot 模块） |
| pyqt6 | ≥6.7 | 悬浮窗唯一 GUI 方案（硬依赖；逐像素透明窗口） |
| pywin32 | ≥306（仅 Windows） | 窗口枚举 / 样式 / 定位 |
| shazamio | ≥0.5.1 | 听歌识曲（安装可能需要 Rust 工具链；未装时若镜像有预编译 wheel 也可） |
| audioop-lts | ≥0.2（仅 Python ≥3.13） | 3.13 移除标准库 audioop，shazamio 的 pydub 依赖它 |
| soundcard / soundfile / pyaudiowpatch / pyaudio | 见 pyproject | 系统音频录制三方案（仅 Windows） |
| dashscope | ≥1.23 | 实时语音识别（内部用 requests/websocket） |
| av | ≥19（仅 Python ≥3.12） | PyAV wheels 自带 FFmpeg 动态库；下限 19 起官方 wheels 实测含 libavdevice（gdigrab）；更早解释器不安装 → 运行时降级禁用视频功能 |
| pysdl2 + pysdl2-dll | ≥0.9.14 / ≥2.30.0 | SDL2 播放引擎（剪辑） |

- 开发依赖（`dependency-groups.dev`）：pyinstaller ≥6.22.3、pytest ≥8.0、ruff ≥0.6；
- ruff 配置：line-length 100、target py310、规则集 `E/F/W/I/UP/B`（E501 忽略）；`payload/*.py` 单独豁免全部规则（注入代码必须兼容 Python 2.7，不能套 py3.10+ 现代化规则）；
- pytest 配置：`testpaths = ["tests"]`、`pythonpath = ["src"]`、`addopts = "-q"`。

## 2. 常用命令

```bash
uv sync                                  # 安装依赖
uv run renpy-overlay --list              # 运行
uv run pytest                            # 单元测试
uv run ruff check .                      # 静态检查
uv run pyinstaller renpy_overlay.spec --noconfirm   # 打包
```

## 3. PyInstaller 打包（renpy_overlay.spec）

**默认目录版（onedir）**：免每次启动解压，冷启动显著快于单文件版（实测 `--list` 启动中位数 962ms vs 单文件 3005ms）。产物 `dist/renpy-overlay/`（`renpy-overlay.exe` + `_internal/` 资源目录），**整个目录一起分发**（只拷 exe 不拷 `_internal/` 无法运行）。单文件版：把 spec 顶部 `ONEFILE = False` 改 `True`（产物单个 exe，每次启动解压到 `%TEMP%/_MEIxxxx`，自解压行为更易被杀软启发式关注）。

要点：

1. **入口**用根目录 `pyinstaller_entry.py`（绝对导入）——PyInstaller 把入口当顶层 `__main__` 执行，包内 `__main__.py` 因相对导入不可用；
2. **`payload/agent.py` 是注入到游戏进程内执行的源码**，静态分析发现不了它：必须显式放进 `datas`（目标路径 `renpy_overlay/payload/`），且 `renpy_overlay.payload` 进 `hiddenimports`（运行时 `files("renpy_overlay.payload")` 定位资源前需先 import 该包）；`assets/`（入口图标 SVG + 预构建外部工具 unrpyc.exe / rpaextract.exe）同样随包分发，运行时经 `importlib.resources` / 相对包路径定位；
3. **SDL2 手动收集**：pysdl2-dll 无 PyInstaller hook，其 `sdl2dll/dll/SDL2.dll` 必须手动加入 binaries；只收 SDL2 本体（render/audio/events），SDL2_image/mixer/ttf/gfx 扩展及第三方依赖链不打包；
4. **excludes 只做保险**（运行链真实依赖：psutil / pywin32 / sqlite3 / PyQt6 / requests+websocket / PIL / numpy / aiohttp 等）：剔除 scipy、pandas、matplotlib、cv2、torch、PyQt5/PySide、tkinter（进程选择窗已改用 PyQt6，不再依赖）、IPython、lxml、pytest、unittest 等；
5. **Analysis 后按名单过滤**：剔 Qt 翻译文件（~180 个 .qm，界面硬编码中文）、Tcl 时区库 tzdata（~600 文件；tkinter 已移出运行链，此过滤仅为防连带收集的保险）、二进制黑名单（`opengl32sw.dll` 软件渲染 OpenGL ~20MB、`qt6pdf.dll`/`qpdf.dll`、罕见图像格式插件 qtiff/qtga/qwbmp/qicns；qsvgicon + Qt6Svg 保留——assets SVG 图标依赖）；
6. **EXE 选项**：`console=True`（命令行参数、日志与控制台命令都依赖控制台）、**`upx=False`**（压缩会加剧杀软对远程注入工具的误报）。

## 4. 打包后的运行时行为

- 首次运行在 **exe 同目录**自动生成 `config.json`（`config.default_path` 的 frozen 分支；从旧单文件版迁移时把旧 `config.json` 拷入新目录即可）；
- 日志默认写当前工作目录 `logs/`（双击运行时即 exe 同目录）；
- 注入、悬浮窗、翻译等行为与 `uv run renpy-overlay` 完全一致，命令行参数相同；
- `payload/` 模块在打包形态下经 `sys._MEIPASS`（onefile 的 _MEI 临时目录 / onedir 的 `_internal`）定位，两种模式资源定位同构。

## 交叉链接

- 配置文件位置规则：[config.md](config.md) §1
- payload 源码的 py2/py3 兼容约束：[dialogue-capture.md](dialogue-capture.md) §1
- 外部工具（unrpyc / rpaextract）来源：[pretranslate.md](pretranslate.md) §7
