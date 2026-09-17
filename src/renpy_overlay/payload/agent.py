# -*- coding: utf-8 -*-
"""renpy-overlay 的注入代理：在 Ren'Py 进程内捕获对话并推送到外部悬浮窗。

设计约束（决定了下面所有写法）：

1. **不能影响游戏**。任何 Hook 都可能在主循环里被调用，因此每个回调都整体包在
   ``try/except BaseException`` 里，绝不把异常抛回游戏；网络 IO 全部放到独立的
   守护线程，主循环里只做"读值 + 入队"。
2. **必须兼容 Python 2.7 / 3.x**。Ren'Py 7.x 内置 2.7，8.x 内置 3.9+，同一份
   源码要能跑在两边：不使用新语法，用 ``_to_text`` 统一处理 bytes/str/unicode。
3. **多层捕获 + 去重**。Ren'Py 各版本对话实现差异较大（ADV / NVL / 气泡 /
   多角色），因此同时挂官方回调与轮询兜底，谁先捕到都算，靠 ``(who, what)``
   去重避免重复输出。
4. **可完全卸载**。安装时记录"反注册函数"，``shutdown()`` 时逆序执行，
   把 config 列表、被包装的原有回调、socket、线程全部还原。

被捕获的层（任一可用即可，全部可用则互为补充）：

- ``config.all_character_callbacks``：官方角色回调，``event == "begin"`` 时
  kwargs 里带完整的 ``what``，是最可靠的来源。
- ``config.say_arguments_callback``：链式包装以取得当前发言角色对象。
- ``renpy.exports.menu``：链式包装，所有 menu 语句（分支选项）的唯一入口 ——
  进入时捕获选项原文（玩家当前语言下所见文本）与触发对话上下文，返回时
  捕获玩家所选；游戏自定义 choice screen 也经过它。
- ``say`` 界面作用域：``renpy.get_screen("say").scope["who" / "what"]``。
- 对话历史：``renpy.store._history_list[-1].who / .what``。
- 当前语句：``renpy.game.context().current`` 上的 ``who / what``。
"""

import json
import os
import re
import socket
import sys
import threading
import time

AGENT_ID = "renpy_overlay_agent"
PROTO_VERSION = 1

DEFAULT_CONFIG = {
    "host": "127.0.0.1",
    "port": 0,
    "token": "",
    "poll_hz": 7.0,
    "max_queue": 512,
    "heartbeat": 2.0,
    "reconnect_max": 5.0,
    "notify": True,
    "log_level": "info",
}

# 用 dict 承载全局状态：避免 global 语句，同时保证跨线程读写是"单个字典键"级别的原子操作
_STATE = {
    "config": dict(DEFAULT_CONFIG),
    "queue": None,
    "sock": None,
    "sock_lock": None,
    "stop": True,
    "install_done": False,
    "install_deadline": 0.0,
    "remove_hooks": [],
    "restore": [],
    "layers": [],
    "last_who": "",
    "last_what": "",
    "last_publish": 0.0,
    "who_hint": "",
    "captured": 0,
    "sent": 0,
    "dropped": 0,
    "errors": [],
    "started_at": 0.0,
    "last_heartbeat": 0.0,
    "say_arguments_prev": None,
    "say_arguments_had": False,
    "exports_menu_prev": None,
    "last_menu": None,
    "last_choice_captions": None,
}

_TAG_RE = re.compile(r"\{[^{}]*\}")


# ------------------------------------------------------------------ 基础工具


def _to_text(value):
    """把任意值转换为可安全 JSON 化的文本（py2/py3 通用）。"""
    if value is None:
        return ""
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8", "replace")
        except BaseException:
            return ""
    if isinstance(value, type("")):
        return value
    name = getattr(value, "name", None)
    if isinstance(name, (bytes, type(""))) and name:
        return _to_text(name)
    try:
        return "%s" % (value,)
    except BaseException:
        return ""


def _clean_text(text):
    """去掉 Ren'Py 文本标签与转义，便于在悬浮窗里按纯文本展示。"""
    text = text.replace("{{", "\x01").replace("[[", "\x02")
    text = _TAG_RE.sub("", text)
    text = text.replace("\x01", "{").replace("\x02", "[")
    return text.strip()


def _encode_json(obj):
    """JSON 行协议：``ensure_ascii`` 后再转 bytes，彻底避开终端与目标进程的编码差异。"""
    text = json.dumps(obj, ensure_ascii=True)
    if isinstance(text, bytes):
        return text + b"\n"
    return text.encode("ascii", "replace") + b"\n"


def _log(level, message):
    """把游戏端日志通过 IPC 回灌到工具端 logger 的同一条时间线上。"""
    try:
        _emit({"t": "log", "level": level, "msg": message})
    except BaseException:
        pass


def _note_error(where, exc):
    try:
        detail = "%s: %s: %s" % (where, type(exc).__name__, exc)
        errors = _STATE["errors"]
        if len(errors) < 20:
            errors.append(detail)
        _log("warning", "[agent] %s" % detail)
    except BaseException:
        pass


def _emit(obj, priority=False):
    """入队一条协议消息。队列满时丢弃最旧的非关键消息，避免游戏端内存无上限增长。"""
    queue = _STATE["queue"]
    if queue is None:
        return False
    obj.setdefault("pid", os.getpid())
    limit = int(_STATE["config"].get("max_queue", 512))
    try:
        if priority:
            queue.appendleft(obj)
        else:
            queue.append(obj)
        while len(queue) > limit:
            try:
                queue.popleft()
            except BaseException:
                break
            _STATE["dropped"] = _STATE["dropped"] + 1
        return True
    except BaseException as exc:
        _note_error("emit", exc)
        return False


def _stats():
    return {
        "captured": _STATE["captured"],
        "sent": _STATE["sent"],
        "dropped": _STATE["dropped"],
        "queued": len(_STATE["queue"]) if _STATE["queue"] is not None else 0,
    }


# ------------------------------------------------------------------ 捕获逻辑


def _say_screen_scope():
    try:
        import renpy
    except BaseException:
        return None
    getter = getattr(renpy, "get_screen", None)
    if not callable(getter):
        return None
    try:
        screen = getter("say")
    except BaseException:
        return None
    return getattr(screen, "scope", None) if screen is not None else None


def _scan_screen():
    scope = _say_screen_scope()
    if not scope:
        return None
    try:
        what = _to_text(scope.get("what"))
        who = _to_text(scope.get("who"))
    except BaseException:
        return None
    if not what:
        return None
    return (who, what)


def _scan_history():
    try:
        import renpy
    except BaseException:
        return None
    # 双层 getattr：store 属性缺失（如被 mock 的不完整模块）时也不能抛
    history = getattr(getattr(renpy, "store", None), "_history_list", None)
    if not history:
        return None
    try:
        entry = history[-1]
    except BaseException:
        return None
    what = _to_text(getattr(entry, "what", ""))
    if not what:
        return None
    return (_to_text(getattr(entry, "who", "")), what)


def _scan_statement():
    """兜底：直接读当前语句节点（renpy.game.context().current）。"""
    try:
        import renpy

        node = renpy.game.context().current
        what = getattr(node, "what", None)
        if what is None:
            return None
        return (_to_text(getattr(node, "who", None)), _to_text(what))
    except BaseException:
        return None


def _resolve_who(explicit):
    """说话人名字的来源优先级：回调显式给出 > say 界面 > 历史 > 当前语句 > 缓存。"""
    who = _to_text(explicit)
    if who:
        _STATE["who_hint"] = who
        return who
    who = _STATE["who_hint"]
    if who:
        return who
    for scanner in (_scan_screen, _scan_history, _scan_statement):
        try:
            found = scanner()
        except BaseException:
            continue
        if found and found[0]:
            _STATE["who_hint"] = found[0]
            return found[0]
    return ""


def _publish(who, what, source, force=False):
    """统一的出口：清洗文本 + 去重 + 入队。"""
    try:
        what = _clean_text(_to_text(what))
        if not what:
            return False
        who = _clean_text(_to_text(who))
        if not force and what == _STATE["last_what"] and who == _STATE["last_who"]:
            return False
        _STATE["last_what"] = what
        _STATE["last_who"] = who
        _STATE["last_publish"] = time.time()
        _STATE["captured"] = _STATE["captured"] + 1
        return _emit(
            {
                "t": "say",
                "who": who,
                "what": what,
                "src": source,
                "ts": time.time(),
            }
        )
    except BaseException as exc:
        _note_error("publish", exc)
        return False


def _character_callback(event, **kwargs):
    """config.all_character_callbacks 的接收端。"""
    try:
        if event == "begin":
            # begin 事件携带完整台词；即使与上一句完全相同也应输出，所以 force=True
            _publish(_resolve_who(kwargs.get("who")), kwargs.get("what"), "callback", force=True)
            return
        if event == "show":
            # {w}/{p} 会把台词切成多段，这里显示当前正在播放的片段
            start = kwargs.get("start")
            end = kwargs.get("end")
            full = _to_text(kwargs.get("what"))
            if isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(full):
                _publish(_resolve_who(kwargs.get("who")), full[start:end], "callback-segment")
    except BaseException as exc:
        _note_error("character_callback", exc)


def _say_arguments_callback(character, *args, **kwargs):
    """链式包装原回调：只借用第一个入参（当前发言角色）取名字，不改变其行为。"""
    try:
        _STATE["who_hint"] = _clean_text(_to_text(getattr(character, "name", None) or character))
    except BaseException:
        pass
    prev = _STATE.get("say_arguments_prev")
    if callable(prev):
        return prev(character, *args, **kwargs)
    return (args, kwargs)


def _periodic_callback():
    """config.periodic_callbacks：约 20Hz 被调用，内部再按 poll_hz 限流。"""
    try:
        config = _STATE["config"]
        now = time.time()
        interval = 1.0 / max(1.0, float(config.get("poll_hz", 7.0)))
        if now - _STATE["last_publish"] < interval * 0.5:
            return
        for scanner in (_scan_screen, _scan_history, _scan_statement):
            found = scanner()
            if not found:
                continue
            _publish(found[0], found[1], "poll")
            break
        _scan_choice_screen()
        _ensure_installed()
    except BaseException as exc:
        _note_error("periodic", exc)


# ------------------------------------------------------------------ 分支选项捕获


def _menu_entry_caption(item):
    """取单个选项条目的显示文本。

    兼容两种形态：display_menu 原始 items 的 ``(caption, label, chosen)``
    三元组（第二项语义在版本间有差异，不用；长度不足 2 的畸形项跳过，
    与 _publish_choice_pick 的防御一致），以及 choice screen 传入的
    带 ``caption`` 属性的条目对象。
    """
    try:
        if isinstance(item, (list, tuple)):
            return _clean_text(_to_text(item[0])) if len(item) >= 2 else ""
        caption = getattr(item, "caption", None)
        if caption is None:
            return ""
        return _clean_text(_to_text(caption))
    except BaseException:
        return ""


def _menu_captions(items):
    """解析 display_menu 的 items，返回玩家可见的选项文本列表（清洗后）。"""
    captions = []
    if not isinstance(items, (list, tuple)):
        return captions
    for item in items:
        caption = _menu_entry_caption(item)
        if caption:
            captions.append(caption)
    return captions


def _publish_choice(items, source="exports_menu", force=False):
    """菜单出现：上报选项原文列表与触发该菜单的对话上下文。

    以选项文本组合作指纹去重：choice screen 轮询兜底 20Hz 重复扫同一屏时
    只报一次；exports_menu 主 hook 以 ``force=True`` 调用 —— 回退重放、从
    选项节点存档载入等场景会重新执行 menu 语句，必须视为新的选项事件重新
    上报（否则工具端停留在重放的旧句上、选项被忽略）。菜单关闭后由轮询
    清空指纹。实测验证的引擎版本与测试游戏见 _install_now 的 exports_menu 注释。
    """
    if not isinstance(items, (list, tuple)):
        return False
    captions = _menu_captions(items)
    fingerprint = tuple(captions)
    if not captions:
        return False
    if not force and fingerprint == _STATE.get("last_choice_captions"):
        return False
    _STATE["last_menu"] = items  # 原始 items 留给结果匹配（见 _publish_choice_pick）
    _STATE["last_choice_captions"] = fingerprint
    context = _scan_history() or ("", "")
    _STATE["captured"] = _STATE["captured"] + 1
    return _emit(
        {
            "t": "choice",
            "items": captions,
            "who": _clean_text(context[0]),
            "what": _clean_text(context[1]),
            "src": source,
            "ts": time.time(),
        }
    )


def _publish_choice_pick(result, source="exports_menu"):
    """菜单关闭：把玩家所选（exports.menu 的返回值）映射回选项序号与文本并上报。

    返回值是选项的 value：8.x menu_actions 形态在三元组第三位、旧二组形态在
    第二位，两处都尝试匹配；都匹配不上时降级为仅上报返回值文本。
    """
    if result is None:
        return False
    items = _STATE.get("last_menu")
    index = -1
    caption = ""
    if isinstance(items, (list, tuple)):
        text = _clean_text(_to_text(result))
        for position, item in enumerate(items):
            if not isinstance(item, (list, tuple)):
                continue
            entry_caption = _menu_entry_caption(item)
            entry_values = [item[1], item[-1]] if len(item) >= 3 else [item[1]] if len(item) >= 2 else []
            if result in entry_values or (entry_caption and entry_caption == text):
                index = position
                caption = entry_caption
                break
    if index < 0:
        caption = _clean_text(_to_text(result))
    return _emit(
        {
            "t": "choice_pick",
            "index": index,
            "caption": caption,
            "src": source,
            "ts": time.time(),
        }
    )


def _exports_menu_wrapper(*args, **kwargs):
    """链式包装 renpy.exports.menu：进入时捕获选项，返回时捕获玩家所选。

    捕获逻辑全部包在 try/except 里，绝不干扰原调用；原函数的异常必须
    原样抛回给游戏（那是游戏自己的流程，不能吞）。实测验证的引擎版本与
    测试游戏见 _install_now 的 exports_menu 注释。
    """
    try:
        items = args[0] if args else kwargs.get("items")
        # force=True：主 hook 意味着 menu 语句真的执行了（首次/回退重放/存档载入），
        # 每次都视为新的选项事件重新上报；轮询兑底的指纹去重不受影响
        _publish_choice(items, force=True)
    except BaseException as exc:
        _note_error("choice_capture", exc)
    prev = _STATE.get("exports_menu_prev")
    if not callable(prev):
        return None  # 未注册（不应发生）：不调用原函数，也不产生 pick
    result = prev(*args, **kwargs)
    try:
        _publish_choice_pick(result)
    except BaseException as exc:
        _note_error("choice_pick", exc)
    return result


def _restore_exports_menu():
    try:
        import renpy

        renpy.exports.menu = _STATE["exports_menu_prev"]
    except BaseException as exc:
        _note_error("restore_exports_menu", exc)


def _scan_choice_screen():
    """轮询兜底：从 choice screen 的 scope 读取选项并上报。

    覆盖两类场景：exports_menu 替换未生效（如实测的 Ren'Py 8.2.1 中 renpy
    命名空间不含 display_menu/menu 的 re-export，或被调用方绕过），以及直接从
    停在选项节点的存档载入。游戏自定义 choice screen 名时本兜底失效，依赖
    exports_menu 主 hook。

    返回 True 表示菜单当前在屏；不在屏时清空指纹，允许同一菜单下次出现时
    重新上报。同一菜单在屏期间靠 _publish_choice 的指纹去重不重复上报。
    """
    try:
        import renpy
    except BaseException:
        return False
    getter = getattr(renpy, "get_screen", None)
    if not callable(getter):
        return False
    try:
        screen = getter("choice")
    except BaseException:
        screen = None
    if screen is None:
        _STATE["last_choice_captions"] = None  # 菜单已关闭：清指纹
        return False
    scope = getattr(screen, "scope", None)
    try:
        items = scope.get("items") if scope is not None and hasattr(scope, "get") else None
    except BaseException:
        items = None
    if not items:
        return False
    _publish_choice(items, source="choice_screen")
    return True


# ------------------------------------------------------------------ Hook 安装


def _install_now():
    """在主线程（或任意线程，GIL 下 list.append 是原子的）真正执行注册。"""
    if _STATE["install_done"]:
        return _STATE["layers"]
    _STATE["install_done"] = True
    layers = []
    try:
        import renpy

        config = renpy.config

        callbacks = getattr(config, "all_character_callbacks", None)
        if isinstance(callbacks, list):
            callbacks.append(_character_callback)
            _STATE["remove_hooks"].append(
                lambda: (
                    callbacks.remove(_character_callback)
                    if _character_callback in callbacks
                    else None
                )
            )
            layers.append("all_character_callbacks")

        periodic = getattr(config, "periodic_callbacks", None)
        if isinstance(periodic, list):
            periodic.append(_periodic_callback)
            _STATE["remove_hooks"].append(
                lambda: (
                    periodic.remove(_periodic_callback) if _periodic_callback in periodic else None
                )
            )
            layers.append("periodic_callbacks")

        # say_arguments_callback 是"单个函数"而不是列表，必须链式包装并记录原值
        _STATE["say_arguments_had"] = hasattr(config, "say_arguments_callback")
        _STATE["say_arguments_prev"] = getattr(config, "say_arguments_callback", None)
        try:
            config.say_arguments_callback = _say_arguments_callback
            _STATE["remove_hooks"].append(_restore_say_arguments)
            layers.append("say_arguments_callback")
        except BaseException as exc:
            _note_error("say_arguments_callback", exc)

        # 分支选项：已实测验证读取 —— Ren'Py 8.2.1.24030407（脚本版本 (8, 2, 1)，
        # 构建于 2025-07-06，Python 3.9.10 / x64），测试游戏 Sicae-Ep.7。该版本中
        # menu 语句经 Menu.execute 调用 renpy.exports.menu(choices, ...)（唯一入口，
        # 返回值即所选 value）；renpy 命名空间无 display_menu/menu 的 re-export，
        # renpy/display/menu.py 也不存在 —— 因此直接替换 renpy.exports 模块上的
        # menu；替换未生效或被绕过的场景（如从选项节点存档载入）由轮询兜底
        # （_scan_choice_screen）覆盖。更早/更晚版本未实测，以 Hook 层日志中的
        # exports_menu 是否存在为准。
        hooked = False
        exports_menu = getattr(renpy.exports, "menu", None)
        if callable(exports_menu):
            try:
                _STATE["exports_menu_prev"] = exports_menu
                renpy.exports.menu = _exports_menu_wrapper
                _STATE["remove_hooks"].append(_restore_exports_menu)
                hooked = True
            except BaseException as exc:
                _note_error("exports_menu", exc)
        if hooked:
            layers.append("exports_menu")

        exit_callbacks = getattr(config, "python_exit_callbacks", None)
        if isinstance(exit_callbacks, list):
            # 游戏退出时自动清理，避免把我们的线程/socket 留在退出流程里
            exit_callbacks.append(shutdown)
            _STATE["remove_hooks"].append(
                lambda: exit_callbacks.remove(shutdown) if shutdown in exit_callbacks else None
            )
            layers.append("python_exit_callbacks")
    except BaseException as exc:
        _note_error("install", exc)

    _STATE["layers"] = layers
    _log("info", "[agent] Hook 安装完成：%s" % ", ".join(layers))
    _emit({"t": "hooks", "layers": layers, "stats": _stats()}, priority=True)
    _notify("renpy-overlay 已接入，对话将同步到悬浮窗")
    return layers


def _restore_say_arguments():
    try:
        import renpy

        if _STATE["say_arguments_had"]:
            renpy.config.say_arguments_callback = _STATE["say_arguments_prev"]
        else:
            try:
                del renpy.config.say_arguments_callback
            except BaseException:
                renpy.config.say_arguments_callback = None
    except BaseException as exc:
        _note_error("restore_say_arguments", exc)


def _notify(message):
    if not _STATE["config"].get("notify", True):
        return
    try:
        import renpy

        renpy.notify(message)
    except BaseException:
        pass


def _ensure_installed():
    """看门狗：若 renpy.invoke_in_main_thread 是异步的且迟迟没执行，这里补一次。"""
    if _STATE["install_done"]:
        return
    if _STATE["install_deadline"] and time.time() > _STATE["install_deadline"]:
        _log("warning", "[agent] 主线程注册超时，改为直接在当前线程注册")
        _install_now()


def _install_hooks():
    """优先请求在主线程注册（更保守），失败或超时则退回当前线程直接注册。"""
    try:
        import renpy
    except BaseException as exc:
        _log("error", "[agent] 目标进程内无法 import renpy：%r" % (exc,))
        return []

    invoker = getattr(renpy, "invoke_in_main_thread", None)
    if callable(invoker):
        try:
            invoker(_install_now)
            _STATE["install_deadline"] = time.time() + 2.0
            return ["deferred:main_thread"]
        except BaseException as exc:
            _log("warning", "[agent] invoke_in_main_thread 不可用(%r)，改为直接注册" % (exc,))

    return _install_now()


# ------------------------------------------------------------------ 网络线程


def _drop_socket():
    sock = _STATE.get("sock")
    _STATE["sock"] = None
    if sock is not None:
        try:
            sock.close()
        except BaseException:
            pass


def _ensure_socket():
    sock = _STATE.get("sock")
    if sock is not None:
        return sock
    config = _STATE["config"]
    try:
        new_sock = socket.create_connection(
            (str(config.get("host", "127.0.0.1")), int(config.get("port", 0))), 2.0
        )
        new_sock.settimeout(2.0)
        _STATE["sock"] = new_sock
        # hello 直接同步发出，不经过队列：服务端要求它必须是首个报文
        _send_now(
            {
                "t": "hello",
                "token": str(config.get("token", "")),
                "proto": PROTO_VERSION,
                "pid": os.getpid(),  # hello 绕过 _emit，需自己带 pid
                "py": sys.version.split()[0],
                "hooks": list(_STATE["layers"]),
                "cwd": os.getcwd(),
            }
        )
        _log("info", "[agent] 已连接上报通道 %s:%s" % (config.get("host"), config.get("port")))
        return new_sock
    except BaseException as exc:
        _note_error("connect", exc)
        _drop_socket()
        return None


def _send_now(message):
    """直接发送（仅由发送线程调用），用于必须抢先到达的报文。"""
    sock = _STATE.get("sock")
    if sock is None:
        return False
    lock = _STATE["sock_lock"]
    lock.acquire()
    try:
        sock.sendall(_encode_json(message))
        _STATE["sent"] = _STATE["sent"] + 1
        return True
    finally:
        lock.release()


def _flush():
    sock = _STATE["sock"]
    queue = _STATE["queue"]
    if sock is None or queue is None:
        return
    lock = _STATE["sock_lock"]
    while queue:
        try:
            message = queue.popleft()
        except BaseException:
            return
        lock.acquire()
        try:
            sock.sendall(_encode_json(message))
            _STATE["sent"] = _STATE["sent"] + 1
        finally:
            lock.release()


def _sender_loop():
    backoff = 0.5
    while not _STATE["stop"]:
        try:
            _ensure_installed()
            if _ensure_socket() is None:
                time.sleep(backoff)
                backoff = min(backoff * 2, float(_STATE["config"].get("reconnect_max", 5.0)))
                continue
            backoff = 0.5
            _flush()
            now = time.time()
            if now - _STATE["last_heartbeat"] >= float(_STATE["config"].get("heartbeat", 2.0)):
                _STATE["last_heartbeat"] = now
                _emit(
                    {"t": "hb", "uptime": round(now - _STATE["started_at"], 2), "stats": _stats()}
                )
            time.sleep(0.2)
        except BaseException as exc:
            _note_error("sender", exc)
            _drop_socket()
            time.sleep(backoff)
            backoff = min(backoff * 2, float(_STATE["config"].get("reconnect_max", 5.0)))
    # 退出前尽力把队列里剩下的内容（含 bye）送出去
    try:
        if _STATE.get("sock") is not None:
            _flush()
    except BaseException:
        pass
    _drop_socket()


# ------------------------------------------------------------------ 对外接口


def start(config_json=None):
    """由注入引导代码调用。立即返回，不阻塞调用线程。"""
    import collections

    config = dict(DEFAULT_CONFIG)
    if config_json:
        try:
            user_config = json.loads(config_json)
            if isinstance(user_config, dict):
                config.update(user_config)
        except BaseException as exc:
            _note_error("parse_config", exc)
    _STATE["config"] = config
    _STATE["queue"] = collections.deque()
    _STATE["sock_lock"] = threading.Lock()
    _STATE["stop"] = False
    _STATE["install_done"] = False
    _STATE["install_deadline"] = 0.0
    _STATE["layers"] = []
    _STATE["remove_hooks"] = []
    _STATE["last_what"] = ""
    _STATE["last_who"] = ""
    _STATE["who_hint"] = ""
    _STATE["exports_menu_prev"] = None
    _STATE["last_menu"] = None
    _STATE["last_choice_captions"] = None
    _STATE["started_at"] = time.time()

    thread = threading.Thread(target=_sender_loop, name="renpy-overlay-sender")
    thread.daemon = True
    thread.start()
    _STATE["thread"] = thread

    try:
        _notify("renpy-overlay 注入中…")
    except BaseException:
        pass

    layers = _install_hooks()
    info = {
        "pid": os.getpid(),
        "py": sys.version.split()[0],
        "layers": layers,
        "port": config.get("port"),
    }
    _log("info", "[agent] 代理已启动：%s" % json.dumps(info, ensure_ascii=True))
    return info


def shutdown():
    """卸载：反注册回调 → 还原被包装的函数 → 关 socket → 停线程 → 移出 sys.modules。"""
    already = _STATE["stop"]
    _STATE["stop"] = True
    removed = []
    for name in ("remove_hooks", "restore"):
        for undo in reversed(list(_STATE.get(name) or [])):
            try:
                undo()
                removed.append(getattr(undo, "__name__", "??"))
            except BaseException as exc:
                _note_error("uninstall", exc)
    _STATE["remove_hooks"] = []
    _STATE["restore"] = []

    try:
        if _STATE.get("sock") is not None:
            _emit({"t": "bye", "stats": _stats()}, priority=True)
            _flush()
    except BaseException:
        pass
    _drop_socket()

    if not already:
        _log("info", "[agent] 代理已卸载，捕获 %d 条对话" % _STATE["captured"])
        _notify("renpy-overlay 已卸载")
    try:
        sys.modules.pop(AGENT_ID, None)
    except BaseException:
        pass
    return {
        "unloaded": True,
        "captured": _STATE["captured"],
        "sent": _STATE["sent"],
        "dropped": _STATE["dropped"],
        "errors": list(_STATE["errors"]),
    }


def status():
    """自检接口：外部可要求回传当前状态。"""
    return {
        "layers": list(_STATE["layers"]),
        "install_done": _STATE["install_done"],
        "py": sys.version.split()[0],
        "stats": _stats(),
        "errors": list(_STATE["errors"]),
    }
