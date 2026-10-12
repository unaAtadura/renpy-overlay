"""实时语音识别（ASR-RT）：DashScope 实时流式识别客户端与开关控制器。

移植自参考项目 ``.reference_projects/asr_rt``（asr_rt.py +
openai_dashscope_adapter.py），并适配本项目的注入回调与互斥模式：

    扬声器播放的声音 -> soundcard WASAPI loopback 采集（100ms 分片）
                     -> 重采样 16kHz 单声道 PCM16
                     -> DashScope Recognition（经 OpenAI Realtime 事件语义
                        转换层 :class:`OpenAICompatRealtimeASR`）
                     -> 事件仅经 ``submit`` 回调入宿主队列，Qt 主线程消费
                        上屏（增加、修正观感由宿主实现）

线程模型：采集在守护线程（100ms 一片，阻塞式 ``record`` 天然形成实时节奏）；
dashscope 回调线程仅把事件 dict 交 ``on_server_event``（控制器转投队列）；
``toggle``/``shutdown`` 仅在 Qt 主线程调用（与菜单入口同线程约束），
``notify`` 因此只在主线程路径触达。

配置：``api_base_url_asr_rt`` / ``model_asr_rt`` / ``api_key_asr_rt`` 三键
独立、不回退其它链路（需求确认：留空即不可用，由控制器开启前校验并提示）。
dashscope / soundcard / numpy 延迟导入（依赖缺失不影响主程序启动，开启时
降级提示，与听歌识曲缺依赖同策略）。

识别结果不落 asr.db（需求：防止数据库过度膨胀；落库规避在宿主侧实现——
实时链路从不调用 asr store）。
"""

from __future__ import annotations

import base64
import logging
import threading
from collections.abc import Callable
from importlib.util import find_spec
from urllib.parse import urlparse

logger = logging.getLogger("renpy_overlay.video.asr_rt")

__all__ = [
    "ASR_RT_OFF_HINT",
    "ASR_RT_OFF_TEXT",
    "ASR_RT_ON_HINT",
    "ASR_RT_ON_TEXT",
    "DASHSCOPE_AVAILABLE",
    "SOUNDCARD_AVAILABLE",
    "AsrRtController",
    "OpenAICompatRealtimeASR",
    "find_loopback_microphone",
    "openai_base_url_to_ws_url",
    "resolve_asr_rt_options",
]

#: 菜单开关文案（需求：实时语音识别: 关/开）
ASR_RT_ON_TEXT = "实时语音识别: 开"
ASR_RT_OFF_TEXT = "实时语音识别: 关"
#: 标题窗提示（需求原文文案：开启阻塞其余 API 请求 / 关闭恢复）
ASR_RT_ON_HINT = "实时语音识别中，阻塞其余API请求"
ASR_RT_OFF_HINT = "实时语音识别关闭，允许新的API请求"
#: 开启失败提示前缀（需求未覆盖失败场景；确认决议：状态回退为关并提示原因）
ASR_RT_UNAVAILABLE_PREFIX = "实时语音识别不可用："

#: 每片时长约 100ms（参考实现同款：阻塞式 record 天然形成实时节奏）
CHUNK_MS = 100
#: loopback 按设备原生采样率采集（Windows 共享模式混合率），避免内部重采样丢帧
CAPTURE_SR = 48000
#: 识别采样率（16kHz PCM）
TARGET_SR = 16000
#: 采集线程收尾 join 的超时（秒）：record 单片至多 100ms，超时仅见于异常卡死
JOIN_TIMEOUT_SECONDS = 2.0


def _is_available(name: str) -> bool:
    """依赖可用性探测：只查包元数据不真正导入（重库导入放到使用处内部）。"""
    try:
        return find_spec(name) is not None
    except (ImportError, ValueError):  # pragma: no cover - 非法包名等极端场景
        return False


#: 可用性标志（模块导入时确定，运行期不变；缺依赖不影响主程序启动）
DASHSCOPE_AVAILABLE = _is_available("dashscope")
SOUNDCARD_AVAILABLE = _is_available("soundcard")


def resolve_asr_rt_options(app_config) -> tuple[str, str, str]:
    """实时识别三键直读（纯函数，离线测试用）：不回退其它链路。

    返回 ``(base_url, model, api_key)``；与视觉/非实时 ASR 的空值回退主链路
    不同（需求确认），任一键为空即视为未配置，由调用方（控制器）降级提示。
    """
    return (
        app_config.api_base_url_asr_rt,
        app_config.model_asr_rt,
        app_config.api_key_asr_rt,
    )


def openai_base_url_to_ws_url(base_url: str) -> str:
    """https://{host}/compatible-mode/v1 -> wss://{host}/api-ws/v1/inference（纯函数）。

    若 base_url 已是 ws/wss 形态则原样返回，便于直接指定 WS 端点。
    """
    parsed = urlparse(base_url or "")
    if parsed.scheme in ("ws", "wss"):
        return base_url
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}/api-ws/v1/inference"


def _resample_to_16k(mono, sr_in: int):
    """单声道 float32 重采样到 16kHz（纯计算）：整数倍降采样用 box filter
    （等效低通+抽取），其余线性插值。"""
    import numpy as np

    if sr_in == TARGET_SR:
        return mono
    if sr_in % TARGET_SR == 0:
        k = sr_in // TARGET_SR
        n = (len(mono) // k) * k
        return mono[:n].reshape(-1, k).mean(axis=1)
    n_out = int(round(len(mono) * TARGET_SR / sr_in))
    return np.interp(np.linspace(0, len(mono) - 1, n_out), np.arange(len(mono)), mono)


def find_loopback_microphone():
    """获取默认扬声器对应的 loopback 采集设备（即录系统播放的声音）。"""
    import soundcard as sc

    speaker = sc.default_speaker()
    try:
        return sc.get_microphone(id=str(speaker.name), include_loopback=True)
    except Exception as exc:
        mics = [m for m in sc.all_microphones(include_loopback=True) if m.isloopback]
        if not mics:
            raise RuntimeError("未找到可用的 WASAPI loopback 采集设备") from exc
        return mics[0]


def _sentence_text(result) -> str:
    """从 RecognitionResult 中取出文本（get_sentence() 返回 dict，取 text 字段）。"""
    sentence = result.get_sentence()
    if not sentence:
        return ""
    if isinstance(sentence, dict):
        return sentence.get("text") or ""
    return getattr(sentence, "text", None) or ""


class OpenAICompatRealtimeASR:
    """以 OpenAI Realtime API 的接口形态驱动 DashScope Recognition（协议转换层）。

    请求映射：``connect`` -> ``Recognition.start``、``append_input_audio`` ->
    ``send_audio_frame``、``close`` -> ``Recognition.stop``；响应映射：中间
    增量（从句首累积的全量文本）-> ``response.audio_transcript.delta``、句
    结束 -> ``conversation.item.input_audio_transcription.completed``、错误
    -> ``error``。dashscope 延迟导入：依赖缺失时构造即抛 :class:`RuntimeError`。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        on_server_event: Callable[[dict], None] | None = None,
    ) -> None:
        try:
            import dashscope
            from dashscope.audio.asr import Recognition
        except ImportError as exc:  # 依赖缺失：与 find_spec 探测口径一致
            raise RuntimeError("dashscope 依赖未安装") from exc
        self._model = model
        self.on_server_event = on_server_event
        self._Recognition = Recognition
        # OpenAI: wss 建连 + Bearer -> DashScope: 同一专属实例的 WS 推理端点 + token 鉴权
        dashscope.base_websocket_api_url = openai_base_url_to_ws_url(base_url)
        dashscope.api_key = api_key
        self._recognition = None

    # ------------------------------------------------------------------
    # 客户端 -> 服务端（请求方向）
    # ------------------------------------------------------------------

    def session_update(self, input_audio_format: str = "pcm16", sample_rate: int = TARGET_SR):
        """对应 OpenAI ``session.update``：声明音频格式并构造识别实例。"""
        fmt = "pcm" if input_audio_format in ("pcm16", "pcm") else input_audio_format
        self._recognition = self._Recognition(
            model=self._model,
            format=fmt,
            sample_rate=sample_rate,
            callback=self._build_callback(),
        )

    def connect(self) -> None:
        """对应 OpenAI Realtime WebSocket 建连；DashScope 侧开启流式识别任务。"""
        self._ensure_recognition()
        self._recognition.start()

    def append_input_audio(self, audio) -> None:
        """对应 ``input_audio_buffer.append``：原始 PCM16 字节或 base64 字符串。"""
        self._ensure_recognition()
        if isinstance(audio, (bytes, bytearray, memoryview)):
            payload = bytes(audio)
        else:
            payload = base64.b64decode(audio)
        self._recognition.send_audio_frame(payload)

    def close(self) -> None:
        """对应关闭 WebSocket 连接；DashScope 侧 stop（最终结果不再等待）。"""
        self._ensure_recognition()
        self._recognition.stop()

    # ------------------------------------------------------------------
    # 内部：DashScope 回调 -> OpenAI 服务端事件
    # ------------------------------------------------------------------

    def _build_callback(self):
        # 鸭子类型桥（不继承 RecognitionCallback）：保持 dashscope 延迟导入，
        # Recognition 仅按方法名回调，无需继承官方基类
        outer = self

        class _Bridge:
            def on_open(self) -> None:
                outer._emit({"type": "session.created"})

            def on_event(self, result) -> None:
                text = _sentence_text(result)
                if result.is_sentence_end(result.get_sentence()):
                    # 句子结束 -> OpenAI 最终转写事件
                    outer._emit(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": text,
                        }
                    )
                else:
                    # 中间增量（从句首累积的全量文本）-> OpenAI 增量转写事件
                    outer._emit({"type": "response.audio_transcript.delta", "delta": text})

            def on_error(self, result) -> None:
                outer._emit(
                    {
                        "type": "error",
                        "code": getattr(result, "status_code", None),
                        "message": getattr(result, "message", None),
                    }
                )

            def on_complete(self) -> None:
                outer._emit({"type": "response.done"})

            def on_close(self) -> None:
                outer._emit({"type": "session.closed"})

        return _Bridge()

    def _emit(self, event: dict) -> None:
        if self.on_server_event is not None:
            self.on_server_event(event)

    def _ensure_recognition(self) -> None:
        if self._recognition is None:
            raise RuntimeError("请先调用 session_update() 构造识别实例")


class AsrRtController:
    """实时语音识别开关控制器（纯后台无窗口；与 QuickMenu 开关型入口同模式）。

    注入回调：``submit(kind, payload)`` 识别事件入宿主队列（跨线程安全）、
    ``notify`` 标题窗提示（仅在主线程的 toggle/shutdown 路径触达）、
    ``is_busy`` 在途互斥查询（宿主合并翻译/识曲/对话/预构建）、
    ``acquire``/``release`` 互斥槽占用与释放、``options`` 三键提供者、
    ``on_state`` 开关状态变更回调（宿主重置显示状态与锚点）。
    ``client_factory``/``mic_finder`` 仅供离线测试注入替身。

    开启失败（缺依赖/未配置/无环回设备/连接失败）回退为关并提示原因，
    不占用互斥槽（确认决议）；关闭立即停止并保留已上屏文本（确认决议）。
    """

    def __init__(
        self,
        *,
        submit: Callable[[str, dict], None],
        notify: Callable[[str], None],
        is_busy: Callable[[], bool],
        acquire: Callable[[], None],
        release: Callable[[], None],
        options: Callable[[], tuple[str, str, str]],
        on_state: Callable[[bool], None] | None = None,
        client_factory=None,
        mic_finder=None,
    ) -> None:
        self._submit = submit
        self._notify = notify
        self._is_busy = is_busy
        self._acquire = acquire
        self._release = release
        self._options = options
        self._on_state = on_state
        self._client_factory = client_factory or OpenAICompatRealtimeASR
        self._mic_finder = mic_finder or find_loopback_microphone
        self._enabled = False
        self._client = None
        self._running: threading.Event | None = None
        self._thread: threading.Thread | None = None

    @property
    def enabled(self) -> bool:
        """实时识别是否开启（菜单文案随此切换）。"""
        return self._enabled

    # ---- 开关切换（仅主线程调用） ---------------------------------------

    def toggle(self) -> None:
        """开关切换（主线程）：关→开走启动流程，开→关立即停止。"""
        if self._enabled:
            self._stop()
        else:
            self._start()

    def shutdown(self) -> None:
        """静默停止（宿主关闭/识别错误时调用）：释放互斥但不更新提示。"""
        if not self._enabled:
            return
        self._enabled = False
        self._teardown()
        self._emit_state(False)

    # ---- 启动（校验失败回退为关并提示，不占互斥槽） --------------------

    def _start(self) -> None:
        if self._enabled:  # 防御：重复开启
            return
        if self._is_busy():
            logger.info("已有请求在途（翻译/识曲/对话/预构建），忽略本次实时语音识别")
            return
        if not DASHSCOPE_AVAILABLE:
            logger.warning("实时语音识别依赖缺失：dashscope（pip install dashscope）")
            self._notify(ASR_RT_UNAVAILABLE_PREFIX + "缺少 dashscope 依赖")
            return
        base_url, model, api_key = self._options()
        if not base_url or not model or not api_key:
            # 三键不回退（需求确认）：任一为空即未配置
            logger.info("实时语音识别三键未配置齐（base_url/model/api_key 有空值）")
            self._notify(ASR_RT_UNAVAILABLE_PREFIX + "未配置 API")
            return
        try:
            mic = self._mic_finder()
        except Exception as exc:
            logger.warning("实时语音识别无可用环回录制设备：%s", exc)
            self._notify(ASR_RT_UNAVAILABLE_PREFIX + "无可用环回录制设备")
            return
        self._acquire()
        client = self._client_factory(base_url, api_key, model, on_server_event=self._on_event)
        try:
            # OpenAI: session.update 声明 16kHz PCM16 输入，随后建连
            client.session_update(input_audio_format="pcm16", sample_rate=TARGET_SR)
            client.connect()
        except Exception as exc:
            logger.warning("实时语音识别连接失败：%s", exc)
            self._release()
            self._notify(ASR_RT_UNAVAILABLE_PREFIX + "连接失败")
            return
        self._client = client
        self._enabled = True
        self._running = threading.Event()
        self._running.set()
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(mic,),
            name="asr-rt-capture",
            daemon=True,
        )
        self._thread.start()
        self._emit_state(True)
        self._notify(ASR_RT_ON_HINT)
        logger.info("实时语音识别已开启（model=%s），阻塞其余 API 请求", model)

    # ---- 停止（立即结束，不等待尾部 flush；保留已上屏文本） ------------

    def _stop(self) -> None:
        self._enabled = False
        self._teardown()
        self._emit_state(False)
        self._notify(ASR_RT_OFF_HINT)
        logger.info("实时语音识别已关闭，允许新的 API 请求")

    def _teardown(self) -> None:
        """回收采集线程与识别会话并释放互斥槽（enabled 已置 False 后调用）。"""
        running = self._running
        if running is not None:
            running.clear()
        self._running = None
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception:  # pragma: no cover - 会话已死等场景
                logger.debug("关闭实时识别会话失败（忽略）", exc_info=True)
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=JOIN_TIMEOUT_SECONDS)
        self._release()

    # ---- 采集与事件（后台线程 → 宿主队列） -----------------------------

    def _capture_loop(self, mic) -> None:
        """loopback 采集循环：每约 100ms 一片，降采样 16kHz 单声道 PCM16 推流。"""
        import numpy as np

        running = self._running
        frames_per_chunk = CAPTURE_SR * CHUNK_MS // 1000
        try:
            with mic.recorder(samplerate=CAPTURE_SR) as rec:
                while running.is_set():
                    # record 阻塞至凑满一片，天然形成实时节奏（无声时静默等待）
                    block = rec.record(numframes=frames_per_chunk)
                    mono = block.mean(axis=1)
                    down = _resample_to_16k(mono, CAPTURE_SR)
                    pcm16 = np.clip(down, -1.0, 1.0)
                    pcm16 = (pcm16 * 32767.0).astype("<i2")  # 16bit 小端 PCM
                    client = self._client
                    if client is None:
                        break  # 已停止（如正在退出），结束采集
                    try:
                        client.append_input_audio(pcm16.tobytes())
                    except Exception:
                        break  # 识别任务已关闭（如正在退出），结束采集
        except Exception:
            logger.exception("实时语音识别采集线程异常")
            if running is not None and running.is_set() and self._enabled:
                self._submit("asr_rt_error", {"code": None, "message": "系统音频采集失败"})

    def _on_event(self, event: dict) -> None:
        """dashscope 回调线程入口：识别事件仅入宿主队列（不触碰 GUI）。"""
        etype = str(event.get("type") or "")
        if etype == "response.audio_transcript.delta":
            self._submit("asr_rt_delta", {"text": str(event.get("delta") or "")})
        elif etype == "conversation.item.input_audio_transcription.completed":
            self._submit("asr_rt_completed", {"text": str(event.get("transcript") or "")})
        elif etype == "error":
            self._submit(
                "asr_rt_error",
                {"code": event.get("code"), "message": event.get("message")},
            )
        else:  # session.created / response.done / session.closed：无需上屏
            logger.debug("实时语音识别事件：%s", etype)

    def _emit_state(self, enabled: bool) -> None:
        if self._on_state is not None:
            try:
                self._on_state(enabled)
            except Exception:  # pragma: no cover - 宿主回调异常不影响控制器
                logger.exception("实时语音识别状态回调失败")
