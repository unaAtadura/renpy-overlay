# 截图翻译与视觉识别

对应源码：`src/renpy_overlay/screenshot/` 功能包 + 宿主侧 `stream/screenshot_flow.py`。测试：`test_screenshot_processing.py`、`test_screenshot_store.py`、`test_screenshot_vision.py`、`test_screen_capture.py`、`test_screenshot_hotkeys.py`、`test_mouse_lock.py`、`test_remote_screenshot.py`、`test_remote_automation.py`、`test_screenshot_window_visual.py`、`test_screenshot_history.py`。

## 1. 截图窗口（window.py / window_visual.py）

- 最多 8 个截图窗口（`MAX_WINDOWS = 8`，定义于 `quick_menu.py`），边框按**红橙黄绿青蓝紫黑**依次分配；可拖动、八向拉伸、双击锁定；
- `window_visual.py` 提供窗口绘制与边缘检测纯函数（离线测试锁定视觉行为：pen 2px、alpha 180）；
- 双击锁定后**单击即截取该区域**发起截图翻译；双击解锁不打断在途截图翻译（区别于流式窗：双击解锁流式窗会中止在途翻译）。

## 2. 屏幕抓取（capture.py）

- Pillow `ImageGrab.grab(all_screens=True)` 截取整个虚拟屏，再按 `bbox - 虚拟屏原点` 裁剪——天然支持多显示器副屏负坐标；虚拟屏原点经 `GetSystemMetrics(SM_XVIRTUALSCREEN/YVIRTUALSCREEN)` 获取（失败回退 (0,0)）；
- bbox 一律为 Windows **物理像素**（进程已启用 Per-Monitor DPI 感知，与 `win32api.window_rect` 同一坐标系）；区域无效（right ≤ left 等）抛 `ValueError` 由调用方提示。

## 3. 图像纯函数处理（processing.py）

与 GUI / 线程完全解耦，只收发 `PIL.Image` / bytes：

| 函数 | 行为 | 关键常量 |
| --- | --- | --- |
| `constrain_aspect_ratio` | 宽高比约束到 0.10~10.00：过宽在底边、过高在侧边**白像素填充** | `MIN/MAX_ASPECT_RATIO = 0.10 / 10.00` |
| `scale_to_percent` | 等比缩放到原像素面积的 percent%（`scale = sqrt(面积比)`，LANCZOS） | 配置 `screenshot_compress_percent`，默认 10 |
| `make_thumbnail` | 像素数超 10w 才等比缩小，不放大 | `THUMBNAIL_MAX_PIXELS = 100_000` |
| `encode_jpeg_bytes / encode_jpeg_base64` | JPEG 编码（非 RGB/L 模式先转 RGB） | 原图/缩略图均 `JPEG_QUALITY = 90` |

设计说明：不做对比度增强与灰度化——识图交给 vision 模型，保留原始色彩信息更利于界面文字识别。

## 4. vision 识图客户端（vision.py）

- 请求体：OpenAI 多模态 `image_url`（`data:image/jpeg;base64,...`）+ `text` 指令，非流式，`temperature 0.2`；思考模式三方方言与文本翻译同一套（见 [translation.md](translation.md) §1）；
- 独立三键配置：`api_base_url_vision` / `model_vision` / `api_key_vision`（空值回退主链路 / 自动发现，见 [config.md](config.md)）；
- `translate_image`：指令 `DEFAULT_SCREENSHOT_PROMPT`（"把图片中的全部文字翻译成简体中文。只输出中文译文本身……"）——刻意**不提"保持原始格式/顺序"**（实测 qwen3.5-vl 会被这句带偏，只输出识别不翻译）；
- **结果校验与强化重试**（`translate_image_verified`）：`looks_untranslated` 纯函数按非空白字符中 CJK 占比 < 0.25 判定"疑似未翻译"；未通过则用 `RETRY_SCREENSHOT_PROMPT` 强化指令重试一次（重试结果无论是否通过都返回并记 WARNING；同一请求最多两次 API 调用；`abort_check` 返回 True 时放弃重试）；
- `recognize_image`：平级独立链路，只识别返回图中文字原文（`DEFAULT_OCR_PROMPT`），供 AI 对话 OCR 等复用。

## 5. 截图翻译流程（stream/screenshot_flow.py 宿主侧）

1. 双击锁定截图窗后单击 → 校验互斥（与单击/自动翻译共用"同时仅一个 API"槽，在途则不发请求不提示）；
2. **隐藏-截屏-恢复时序**：`QuickMenu.hide_all` 隐藏全部截图窗与提示窗（含遥控 A/B）→ 等待 `SCREENSHOT_DWM_SETTLE_SECONDS = 0.18`（约 10 个 60Hz 合成周期，防止抓到刚隐藏窗口的 DWM 旧帧残影）→ `capture_region` 截屏 → finally 恢复显示（快捷键锁定态只恢复提示窗不放出截图窗）；
3. 后台线程：`constrain_aspect_ratio` → `scale_to_percent` → `encode_jpeg_base64` → `translate_image_verified`（vision 三键）；
4. 译文经队列回主线程显示在正文窗；**成功**后原图（JPEG Q90）与 ≤10w 像素缩略图存入 `screenshot.db`（**不写翻译两级缓存**）；**失败**清除内存图像并提示「翻译失败」。

AI 对话 OCR 复用同一套隐藏-截屏-恢复时序（见 [ai-chat.md](ai-chat.md)）。

## 6. 快捷键模式（hotkeys.py + frame_overlay.py）

- 菜单开关（默认关）。开启后：整体隐藏全部截图窗口并锁定布局（`lock_layout`/`unlock_layout`/`locked_windows`），为每个已双击锁定的截图区域显示**等大小全穿透提示窗口**（`frame_overlay.py`：几何与截屏抓取范围精确重合、彩色边框画在内沿零偏移；窗口标志在 `show()` 之前一次固定，运行时只 show/hide/setGeometry/重绘，绝不切换标志——运行时切换会触发 Qt 隐藏窗口与原生样式重算，是穿透失效迭代的根源）；
- 注册全局热键：主开关键 `hotkey_main`（默认 `alt+t`）+ 数字 1-8（`hotkey_window_1..8`，对应八色窗口，`DEFAULT_WINDOW_HOTKEYS`）；
- 主开关**开启**后按数字键：命中"模式开启时已双击锁定的窗口"等同该窗口锁定态左键——按窗口**现取几何坐标**触发截图翻译；主开关**关闭**时按数字键仅提示主开关状态；关闭模式注销全部热键并恢复布局；
- ⚠️ **已知引擎级行为**（A/B 对照实测）：含 Ctrl 的快捷键在 Ren'Py 游戏内触发后即使全部松开，游戏仍持续快进（Ctrl 是 Ren'Py 快进键）——与是否运行本工具无关，属 Ren'Py 输入栈自身行为，程序侧补发 key-up 无法治愈；主开关键含 Ctrl 时开启会提示规避，建议改用不含 Ctrl 的组合（如 `alt+f9`）；锁鼠标逃脱键同理默认 `alt+o`；
- `parse_hotkey` 纯函数解析键位字符串，非法格式在 config 层回退默认值。

## 7. 锁鼠标区域（mouse_lock.py / mouse_lock_window.py / mouse_lock_indicator.py）

- 菜单「锁鼠标区域: 关/开」（默认关）：窗口化运行游戏时把鼠标活动范围限制在框选区域内；
- 开启后出现灰色边框（#808080）范围窗口（可拖动、八向拉伸）；**双击锁定生效**：范围窗口隐藏，由等大小的鼠标穿透提示窗口替换（边框精确显示框选范围不外扩一个像素，与 frame_overlay 同一做法）；`ClipCursor` 限制鼠标，**每 200ms 周期重申**（对抗 Alt+Tab / 其它程序覆盖 ClipCursor）；
- 窗口隐藏后无法双击解锁 → 逃脱热键 `hotkey_mouse_escape`（默认 `alt+o`）为保底退出：功能开启期间全程注册，按下即解除限制、销毁提示窗并恢复范围窗可拖拽状态（可再次双击锁定，解锁无提示）；菜单再点「关」彻底销毁两窗口；
- 与截图布局锁定互不影响（lock_layout 系列不隐藏或捕获本功能窗口）；状态机（开关联动热键注册、双击锁定/逃脱热键解锁、ClipCursor 施加与解除、提示文案）由 `tests/test_mouse_lock.py` 全 stub 离线验证。

## 8. 遥控截图（remote_screenshot.py / remote_screenshot_window.py）

- 配置 `remote_screenshot_enable`（默认开）——**快捷键模式的附属功能**：进入快捷键模式时生成两个可独立拖动的小圆形悬浮窗：
  - 窗口 A（触发器）：无边框纯色半透明粉圆 `#F7A8B8`，直径 `remote_screenshot_diameterA`（默认 100，最小 20）；
  - 窗口 B（瞄准器）：同色边框 + 黑色半透明蒙版的圆形，直径 `remote_screenshot_diameterB`；
- **装填**：把 B 拖到已双击锁定的截图区域上——B 变正方形（边长 = 直径、中心点重合，几何尺寸不变）且边框色变为该区域窗口颜色；拖到未锁定区域不变形并忽略触发；与多个锁定区域相交时**第一个进入的驻留生效**，离开后恢复圆形；
- 单击窗口 A = 对装填区域发起截图翻译（复用 §5 流程：互斥、隐藏时序、提示规则一致）；退出快捷键模式即销毁 A/B；
- 该功能与快捷键模式**主开关相互独立**——主开关关闭时遥控截图照常工作。

## 9. 遥控器自动化（remote_automation.py）

配置 `remote_screenshot_interval`（默认 0.5s，最小 0.1）/ `remote_cache_size_count`（默认 50 条，最小 1）。基于遥控截图的自动化截屏翻译：

- 开启（快捷菜单「遥控器自动化: 开」）后单击 A = 对装填区域启动第一轮截图翻译并进入自动状态：按周期间隔截图，**画面变化检测**通过才进入翻译——感知签名算法为网格灰度感知签名（**32x16 均值网格**，aHash 家族；容忍屏幕噪点与 OLED 防烧屏像素偏移，对「大画面小字幕」的内容变化敏感）；
- 同一时刻只允许一个在途 API；API 繁忙期间保持变化状态、通畅后先把新截图查**内存缓存**（循环队列，仅存感知签名↔译文，命中直接上屏、新内容顶掉最早内容），未命中才提交 API；
- 自动状态中再次单击 A = 重启一轮；B 离开装填区域或切换目标即停止自动并等待再次点击 A；B 装填期间自动隐藏该区域的线框提示；
- 自动化全程**不隐藏任何窗口**（无反复隐藏/显示的爆闪）；「隐藏遥控器」隐藏/恢复 A/B（自动化不中断）；关闭自动化仅停周期截图、保留遥控截图原有单击功能；退出快捷键模式自动化自动复位为关；
- 自动化截图翻译**成功即写 screenshot.db**（与手动同一落库规则；缓存命中不发 API、不重复写库）。

## 10. 截图历史窗口（history_window.py）与原图查看（viewer_window.py）

- 常规窗口浏览 `screenshot.db`：左上年/月/日下拉（年范围取首末记录、日智能平闰年）+ 首末记录日期标签；左中缩略图条带（滚轮上滚看早期/下滚看后期、按住左键拖动浏览；中垂线扫到的缩略图停留 1s 自动选中、左键点击跳过延迟直接选中）；左下选中记录译文；右侧原图；
- 导出：日期行右侧「导出/全部导出」把选中/全部记录的原图原始字节写入与 `screenshot.db` 同级的 `renpy_overlay_cache/screenshot/`（文件名 `<id>.<ext>`，扩展名按 BLOB 魔数判定，不重编码不缩放；重名一律跳过绝不覆盖，前台静默仅记日志）；
- `viewer_window.py`：原图全尺寸查看弹窗（无边框置顶、左键拖拽移动、单击任意位置关闭；位移 ≤ `CLICK_MOVE_TOLERANCE = 4` 判单击防误触）。

## 交叉链接

- screenshot.db 表结构：[history-and-storage.md](history-and-storage.md)
- vision 三键配置：[config.md](config.md)
- 视觉 API 请求体方言：[translation.md](translation.md) §1
