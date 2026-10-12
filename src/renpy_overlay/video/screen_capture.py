"""全屏录制编码线程：PyAV gdigrab 抓屏 + libx264 编码到 mkv 中间容器。

替代原 QProcess 驱动外置 ffmpeg.exe 的 gdigrab 录制（需求确认：不再依赖
用户自备的 ffmpeg.exe）。av 19 系官方 wheels 实测已包含 libavdevice，
gdigrab 输入可直接打开，参数与原命令行逐项对应：

- ``-framerate/-offset_x/-offset_y/-video_size`` → gdigrab options 字典
  （物理像素几何，调用方经 recorder.physical_geometry 换算，与原口径一致）；
- ``-c:v libx264 -preset ultrafast -pix_fmt yuv420p`` → 编码器同名配置。

线程与停止约束：

- 本模块在 daemon 线程中运行（recorder 拉起）：gdigrab 实时产出帧，
  解码/编码循环逐帧检查 stop_event，置位后在 ≤1 帧间隔内退出并关闭容器
  正常 finalize（原 QProcess 三层退出策略随之消失——无进程可杀）；
- mkv 中间容器语义保留：簇式容错封装，即使异常未 finalize，已写数据仍可
  被 :func:`.av_media.merge_streams` remux 为最终 mp4；
- ``open_fn`` 仅供离线测试注入（stub 容器；真实 gdigrab 不进测试）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from .av_media import DEFAULT_RECORD_FPS, av_available

logger = logging.getLogger("renpy_overlay.video.screen_capture")

#: 录屏可用性（PyAV 可导入即视为可用；gdigrab 随 av wheels 的 libavdevice 提供）
CAPTURE_AVAILABLE = av_available


def record_screen(
    out_path: str | Path,
    stop_event,
    *,
    offset_x: int,
    offset_y: int,
    width: int,
    height: int,
    fps: int = DEFAULT_RECORD_FPS,
    open_fn=None,
) -> bool:
    """gdigrab 抓主屏区域编码为 mkv；正常收尾返回 True，初始化/编码失败 False。

    ``(offset_x, offset_y, width, height)`` 为 gdigrab 物理像素几何（多屏
    布局下主屏原点可能非零，由调用方经 QGuiApplication 屏幕几何计算）。
    ``stop_event`` 置位后当前帧编码完即退出（协作式停止，无需强杀）。
    """
    import av

    if open_fn is None:
        open_fn = av.open
    options = {
        "framerate": str(fps),
        "offset_x": str(offset_x),
        "offset_y": str(offset_y),
        "video_size": f"{width}x{height}",
    }
    logger.info("开始录屏编码：%s（%dx%d @%dfps）", out_path, width, height, fps)
    try:
        with open_fn("desktop", format="gdigrab", options=options) as src:
            with open_fn(str(out_path), "w") as dst:
                v_in = src.streams.video[0]
                encoder = dst.add_stream("libx264", rate=fps)
                # 尺寸必须显式设置：PyAV 19 的 add_stream 会给编码器预置
                # 640x480 默认值，且不随输入帧自动更新（实测产物被写死）
                encoder.width = width
                encoder.height = height
                encoder.pix_fmt = "yuv420p"
                encoder.options = {"preset": "ultrafast"}
                for frame in src.decode(v_in):
                    if stop_event.is_set():
                        break  # 协作式停止：当前帧收尾后退出（≤1 帧间隔）
                    for packet in encoder.encode(frame):
                        dst.mux(packet)
                for packet in encoder.encode(None):  # flush 尾包，正常 finalize
                    dst.mux(packet)
        logger.info("录屏编码结束：%s", out_path)
        return True
    except Exception as exc:  # 设备打开/编码/IO 异常统一收敛为失败
        logger.error("录屏编码失败（%s）：%s", out_path, exc)
        return False
