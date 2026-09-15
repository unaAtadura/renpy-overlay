"""工具端与注入代理之间的行分隔 JSON（NDJSON）通道。

为什么是"工具端做服务端"：
注入代理的重连逻辑在游戏进程里，让它做客户端最简单 —— 重连失败只影响它自己，
而工具端只要 ``listen`` 一次就能稳定接收，且中途重启工具端也不会留下悬挂的半连接。
绑定到 ``127.0.0.1`` 的随机端口（``port=0`` 由系统分配）避免端口冲突，端口号通过
注入配置传给代理。

安全：仅监听回环地址；首个报文必须是携带正确 ``token`` 的 ``hello``，
之后才进入正常收发。token 由工具端在每次运行开始时随机生成。
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

logger = logging.getLogger("renpy_overlay.ipc")

MAX_LINE = 64 * 1024
ACCEPT_TIMEOUT = 0.5
JOIN_TIMEOUT = 2.0


def encode(message: dict) -> bytes:
    """把消息编码成一行 NDJSON（``ensure_ascii`` 保证任何终端/代码页都能安全传输）。"""
    return json.dumps(message, ensure_ascii=True).encode("ascii", "replace") + b"\n"


def decode(line: bytes | str) -> dict | None:
    """解析一行 NDJSON；非法内容返回 None 而不抛异常（协议层容错）。"""
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8", "replace")
        except Exception:  # pragma: no cover - decode 已带 replace
            return None
    line = line.strip()
    if not line:
        return None
    try:
        message = json.loads(line)
    except ValueError:
        logger.debug("收到非法 JSON 行（前 200 字符）：%.200s", line)
        return None
    return message if isinstance(message, dict) else None


@dataclass
class PeerState:
    """一个已通过 token 校验的注入代理连接。"""

    addr: tuple[str, int]
    pid: int = 0
    py_version: str = ""
    layers: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)
    connected_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    messages: int = 0


class DialogueLink:
    """接收游戏内代理上报的对话消息。"""

    def __init__(
        self,
        token: str,
        host: str = "127.0.0.1",
        port: int = 0,
        on_message: Callable[[dict, PeerState], None] | None = None,
        on_connect: Callable[[PeerState], None] | None = None,
        on_disconnect: Callable[[PeerState], None] | None = None,
    ):
        self._host = host
        self._requested_port = int(port)
        self._token = token
        self._on_message = on_message
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._server: socket.socket | None = None
        self._port = 0
        self._accept_thread: threading.Thread | None = None
        self._reader_threads: list[threading.Thread] = []
        self._clients: list[tuple[socket.socket, PeerState]] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._peers: list[PeerState] = []

    # -- 生命周期 -------------------------------------------------

    @property
    def port(self) -> int:
        return self._port

    @property
    def token(self) -> str:
        """本次会话的握手令牌，需随注入配置一起下发给游戏端。"""
        return self._token

    @property
    def peers(self) -> list[PeerState]:
        with self._lock:
            return list(self._peers)

    def start(self) -> int:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self._host, self._requested_port))
        server.listen(4)
        server.settimeout(ACCEPT_TIMEOUT)
        self._server = server
        self._port = server.getsockname()[1]
        self._stop.clear()
        self._accept_thread = threading.Thread(target=self._accept_loop, name="ipc-accept")
        self._accept_thread.daemon = True
        self._accept_thread.start()
        logger.info("对话通道已监听 %s:%d", self._host, self._port)
        return self._port

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            clients = list(self._clients)
            self._clients = []
        for client, peer in clients:
            self._close_client(client, peer, reason="工具端停止")
        if self._server is not None:
            try:
                self._server.close()
            except OSError:  # pragma: no cover
                pass
            self._server = None
        for thread in [self._accept_thread, *self._reader_threads]:
            if thread is not None and thread.is_alive():
                thread.join(timeout=JOIN_TIMEOUT)
        self._reader_threads = []
        logger.debug("对话通道已关闭")

    # -- 连接处理 -------------------------------------------------

    def _accept_loop(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, addr = self._server.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            client.settimeout(0.5)
            thread = threading.Thread(
                target=self._read_loop, args=(client, addr), name=f"ipc-client-{addr[1]}"
            )
            thread.daemon = True
            self._reader_threads.append(thread)
            thread.start()
            logger.debug("接受连接：%s:%s", addr[0], addr[1])

    def _read_loop(self, client: socket.socket, addr: tuple[str, int]) -> None:
        peer = PeerState(addr=addr)
        buffer = b""
        authenticated = False
        try:
            while not self._stop.is_set():
                try:
                    chunk = client.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                if len(buffer) > MAX_LINE * 8:
                    logger.warning("来自 %s:%s 的缓冲区异常增长，断开连接", addr[0], addr[1])
                    break
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = decode(line)
                    if message is None:
                        continue
                    peer.last_seen = time.time()
                    peer.messages += 1
                    if not authenticated:
                        if not self._authenticate(message, peer, client):
                            return
                        authenticated = True
                        continue
                    self._dispatch(message, peer)
        finally:
            self._close_client(client, peer, reason="对端关闭")

    def _authenticate(self, message: dict, peer: PeerState, client: socket.socket) -> bool:
        if message.get("t") != "hello":
            logger.warning("首个报文不是 hello（收到 %r），断开连接", message.get("t"))
            return False
        if str(message.get("token", "")) != self._token:
            logger.error("token 校验失败，拒绝该连接（可能是其它程序误连）")
            return False
        peer.pid = int(message.get("pid", 0) or 0)
        peer.py_version = str(message.get("py", ""))
        layers = message.get("hooks") or []
        peer.layers = [str(item) for item in layers] if isinstance(layers, list) else []
        with self._lock:
            self._clients.append((client, peer))
            self._peers.append(peer)
        logger.info(
            "游戏端已接入：pid=%s python=%s Hook层=%s",
            peer.pid,
            peer.py_version,
            ",".join(peer.layers) or "<延迟注册>",
        )
        if self._on_connect is not None:
            self._safe_call(self._on_connect, peer)
        return True

    def _dispatch(self, message: dict, peer: PeerState) -> None:
        kind = message.get("t")
        if kind == "hb":
            peer.stats = message.get("stats") or peer.stats
            logger.debug("心跳：pid=%s %s", peer.pid, peer.stats)
            return
        if self._on_message is not None:
            self._safe_call(self._on_message, message, peer)
        else:
            logger.debug("未处理的消息类型：%s", kind)

    def _safe_call(self, callback: Callable, *args) -> None:
        try:
            callback(*args)
        except Exception:  # pragma: no cover - 回调异常不应终止读线程
            logger.exception("消息回调执行失败")

    def _close_client(self, client: socket.socket, peer: PeerState, reason: str = "") -> None:
        try:
            client.close()
        except OSError:  # pragma: no cover
            pass
        with self._lock:
            self._clients = [item for item in self._clients if item[0] is not client]
            if peer in self._peers:
                self._peers.remove(peer)
                logger.info("游戏端连接断开：pid=%s（%s）", peer.pid, reason)
                if self._on_disconnect is not None:
                    self._safe_call(self._on_disconnect, peer)

    # -- 状态查询 -------------------------------------------------

    @property
    def connected(self) -> bool:
        with self._lock:
            return bool(self._clients)

    def seconds_since_last_message(self) -> float:
        with self._lock:
            peers = list(self._peers)
        if not peers:
            return float("inf")
        return time.time() - max(peer.last_seen for peer in peers)
