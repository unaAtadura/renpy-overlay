"""视频剪辑功能子包（自独立实验项目 videocut 迁入，embed 进宿主）。

分层（与 :mod:`renpy_overlay.video` 同约定：纯后台能力与窗口分离，
各文件原子级功能）：

- 纯后台：:mod:`.player`（SDL2 播放引擎：双缓冲/关键帧索引/异步 seek/
  音频主时钟）、:mod:`.editing`（时间轴模型/切片标记/命令流/导出/字幕）、
  :mod:`.waveform`（音频波形提取 + sidecar 缓存）、:mod:`.probe`
  （媒体元数据轻量探测，不启引擎）；
- 窗口层：:mod:`.editor_window`（剪辑主窗口：三区布局 + 快捷键路由 +
  命令流 + 字幕/导出流程）与其面板（播放器/资源/日志/时间轴/声波图/
  全屏/视图状态）。

宿主集成约定（与其它功能一致）：
- 配置读取走 ``config.json`` 单一来源（:mod:`renpy_overlay.config`），
  窗口构造注入宿主 Config，不自带配置文件；
- 制作字幕的 ASR/翻译请求复用 :mod:`renpy_overlay.video.asr` 与
  :mod:`renpy_overlay.translator`，制作期间经宿主注入的
  ``api_acquire``/``api_release`` 占用「同时仅一个 API 请求」互斥槽
  （与实时语音识别同模式）；
- PyAV/SDL2 缺失时由宿主 try-import 降级提示，不影响主程序。
"""
