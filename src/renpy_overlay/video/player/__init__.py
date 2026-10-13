"""SDL2 播放引擎子包（移植自 .reference_projects/videoplayer 已验证方案）。

模块组成：
- :mod:`.sync_clock`：同步时钟与音视频节拍判定（纯函数）
- :mod:`.queues`：视频帧有界队列 + 音频 PCM 环形缓冲（双缓冲基础设施）
- :mod:`.keyframe_index`：关键帧索引预解析 + sidecar 文件缓存
- :mod:`.source_filter`：源滤镜分页 LRU 读透缓存
- :mod:`.audio_output`：SDL2 音频输出（callback + 环形缓冲 + 主时钟）
- :mod:`.decoder`：解码 worker（异步 seek latch + 单遍 demux 分流 + 预填充）
- :mod:`.renderer_sdl2`：SDL2 视频渲染器（IYUV 纹理直传 + 信箱/节拍上屏）
- :mod:`.stats`：实验指标聚合
- :mod:`.engine`：门面（组装 worker/双缓冲/SDL 音视频；QMediaPlayer 风格 API）
"""
