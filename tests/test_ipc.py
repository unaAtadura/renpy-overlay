"""NDJSON 通道的协议往返与握手校验。

这里用真实 socket 起一个 ``DialogueLink`` 服务端，再扮演"游戏端代理"连上去，
覆盖协议线上行为：token 握手、非法报文容错、心跳被协议层消化、断开回调等。
所有等待都带超时，避免竞态导致测试挂死。
"""

from __future__ import annotations

import math
import socket
import time

import pytest

from renpy_overlay import ipc

TOKEN = "tok-for-tests"
WAIT_TIMEOUT = 5.0


def _wait_for(predicate, what: str, timeout: float = WAIT_TIMEOUT):
    """轮询等待条件成立；超时直接让测试失败并说明在等什么。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.01)
    pytest.fail(f"等待超时（{timeout}s）：{what}")


class Harness:
    """一个跑在随机端口上的接收端，外加若干扮演代理的客户端 socket。"""

    def __init__(self, on_message=None):
        self.messages: list[tuple[dict, ipc.PeerState]] = []
        self.connected: list[ipc.PeerState] = []
        self.disconnected: list[ipc.PeerState] = []
        self._extra_on_message = on_message
        self.link = ipc.DialogueLink(
            token=TOKEN,
            port=0,
            on_message=self._on_message,
            on_connect=self.connected.append,
            on_disconnect=self.disconnected.append,
        )
        self.port = self.link.start()
        self._socks: list[socket.socket] = []

    def _on_message(self, message: dict, peer: ipc.PeerState) -> None:
        self.messages.append((message, peer))
        if self._extra_on_message is not None:
            self._extra_on_message(message, peer)

    # -- 扮演游戏端 ------------------------------------------------

    @staticmethod
    def hello(token: str = TOKEN, **extra) -> dict:
        message = {"t": "hello", "token": token, "pid": 4321, "py": "3.9.7"}
        message.update(extra)
        return message

    def connect(self, first_message: dict | None = None) -> socket.socket:
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=WAIT_TIMEOUT)
        sock.settimeout(WAIT_TIMEOUT)
        self._socks.append(sock)
        if first_message is not None:
            self.send(sock, first_message)
        return sock

    @staticmethod
    def send(sock: socket.socket, message: dict) -> None:
        sock.sendall(ipc.encode(message))

    def close(self) -> None:
        for sock in self._socks:
            try:
                sock.close()
            except OSError:  # pragma: no cover
                pass
        self.link.stop()


@pytest.fixture
def harness():
    instance = Harness()
    yield instance
    instance.close()


# ------------------------------------------------------------------ 编解码


def test_encode_is_single_ascii_line():
    payload = {"t": "say", "who": "爱丽丝", "what": "你好\n第二行"}
    blob = ipc.encode(payload)
    assert blob.endswith(b"\n")
    assert blob.count(b"\n") == 1, "内层换行必须被 JSON 转义，保证一行一报文"
    blob.decode("ascii")  # 不抛异常即证明纯 ASCII（任何代码页都能安全传输）
    assert b"\\u4f60" in blob, "非 ASCII 字符应被转义"
    assert ipc.decode(blob) == payload


def test_decode_tolerates_junk():
    assert ipc.decode(b"") is None
    assert ipc.decode(b"   \r\n") is None
    assert ipc.decode(b"not-json") is None
    assert ipc.decode(b"[1, 2]") is None, "顶层不是对象的 JSON 一律丢弃"
    assert ipc.decode(b'{"t": "say"}') == {"t": "say"}
    assert ipc.decode('{"t": "say"}') == {"t": "say"}, "也应接受 str"
    assert ipc.decode(b'{"t": "say"}   ') == {"t": "say"}


# ------------------------------------------------------------------ 握手


def test_handshake_populates_peer(harness):
    sock = harness.connect(harness.hello(hooks=["all_character_callbacks", "periodic"]))
    peer = _wait_for(lambda: harness.connected[0] if harness.connected else None, "on_connect 回调")
    assert peer.pid == 4321
    assert peer.py_version == "3.9.7"
    assert peer.layers == ["all_character_callbacks", "periodic"]
    assert harness.link.connected
    assert harness.link.peers == [peer]
    assert sock.fileno() != -1, "握手成功后连接应保持"


def test_say_is_dispatched_after_handshake(harness):
    sock = harness.connect(harness.hello())
    _wait_for(lambda: harness.connected, "on_connect 回调")
    harness.send(sock, {"t": "say", "who": "爱丽丝", "what": "欢迎来到梦境"})
    message, peer = _wait_for(lambda: harness.messages[0] if harness.messages else None, "say 报文")
    assert message["what"] == "欢迎来到梦境"
    assert peer is harness.connected[0]


def test_wrong_token_is_rejected(harness):
    sock = harness.connect(harness.hello(token="wrong-token"))
    assert sock.recv(1024) == b"", "token 不匹配时服务端应直接断开"
    assert not harness.connected
    assert not harness.messages
    assert not harness.link.connected


def test_message_before_hello_is_rejected(harness):
    sock = harness.connect({"t": "say", "what": "太早了"})
    assert sock.recv(1024) == b"", "首个报文不是 hello 时必须断开"
    assert not harness.connected
    assert not harness.messages


def test_multiple_lines_in_single_chunk(harness):
    sock = harness.connect()
    blob = (
        ipc.encode(harness.hello())
        + ipc.encode({"t": "say", "who": "A", "what": "第一条"})
        + ipc.encode({"t": "say", "who": "B", "what": "第二条"})
    )
    sock.sendall(blob)  # 一个 TCP 分段里塞三行，考验分包处理
    _wait_for(lambda: len(harness.messages) == 2, "两条 say 报文")
    assert [item[0]["what"] for item in harness.messages] == ["第一条", "第二条"]


def test_illegal_line_does_not_break_stream(harness):
    sock = harness.connect(harness.hello())
    _wait_for(lambda: harness.connected, "on_connect 回调")
    sock.sendall(b"{ this is not json }\n")
    harness.send(sock, {"t": "say", "what": "仍然可用"})
    message, _peer = _wait_for(
        lambda: harness.messages[0] if harness.messages else None, "say 报文"
    )
    assert message["what"] == "仍然可用"


# ------------------------------------------------------------------ 心跳与统计


def test_heartbeat_updates_stats_without_dispatch(harness):
    sock = harness.connect(harness.hello())
    _wait_for(lambda: harness.connected, "on_connect 回调")
    harness.send(sock, {"t": "hb", "stats": {"sent": 3, "captured": 5}})
    stats = _wait_for(lambda: harness.link.peers[0].stats, "心跳统计进入 PeerState")
    assert stats == {"sent": 3, "captured": 5}
    assert harness.messages == [], "心跳由协议层消化，不应派发给业务回调"


def test_seconds_since_last_message(harness):
    assert math.isinf(harness.link.seconds_since_last_message()), "无连接时返回 inf"
    harness.connect(harness.hello())
    _wait_for(lambda: harness.connected, "on_connect 回调")
    assert harness.link.seconds_since_last_message() < WAIT_TIMEOUT


# ------------------------------------------------------------------ 断开与容错


def test_disconnect_callback(harness):
    sock = harness.connect(harness.hello())
    peer = _wait_for(lambda: harness.connected[0] if harness.connected else None, "on_connect 回调")
    sock.close()
    seen = _wait_for(
        lambda: harness.disconnected[0] if harness.disconnected else None, "on_disconnect 回调"
    )
    assert seen is peer
    assert not harness.link.connected
    assert harness.link.peers == []


def test_message_callback_exception_does_not_kill_reader():
    calls: list[dict] = []

    def flaky(message: dict, _peer: ipc.PeerState) -> None:
        calls.append(message)
        if len(calls) == 1:
            raise RuntimeError("模拟界面回调抛异常")

    instance = Harness(on_message=flaky)
    try:
        sock = instance.connect(instance.hello())
        _wait_for(lambda: instance.connected, "on_connect 回调")
        instance.send(sock, {"t": "say", "what": "第一条"})
        instance.send(sock, {"t": "say", "what": "第二条"})
        _wait_for(lambda: len(calls) == 2, "第二条报文仍被派发")
        assert calls[1]["what"] == "第二条"
    finally:
        instance.close()


def test_stop_is_idempotent_and_closes_clients(harness):
    harness.connect(harness.hello())
    _wait_for(lambda: harness.connected, "on_connect 回调")
    harness.link.stop()
    assert not harness.link.connected
    harness.link.stop()  # 第二次调用不应抛异常
    assert not harness.link.connected
