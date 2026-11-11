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
- ``config.say_menu_text_filter``：链式包装，在 Say.execute 进正文显示之前拿到
  当句原文（旧版本引擎上 ``_history_list`` 要等交互结束才入账，靠它保证
  「显示即达」；预测路径的调用已判别排除，不会提前上报未来台词）。静态
  next 是 Menu 的提问句（紧邻分支选项）被挂起不单独上报，由 choice 事件
  合并呈现。
- ``renpy.store._last_say_what``：轮询直读，Say.execute 写入的当前句原文显示
  期间常驻（finally 只清 _last_raw_what），可覆盖「注入时该句已在屏」的盲区
  （该句执行早于注入，过滤器不会再触发；偏移 0，不前后取句）。
- ``renpy.exports.menu``：链式包装，所有 menu 语句（分支选项）的唯一入口 ——
  进入时捕获选项原文（玩家当前语言下所见文本）与该菜单的紧邻提问对白
  （前置 say 挂起、menu 自带 caption 行或 AST 静态前驱，无则为空），返回时
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
    "say_text_filter_prev": None,
    "exports_menu_prev": None,
    "last_menu": None,
    "last_choice_captions": None,
    "pending_prompt": None,  # 挂起的 menu 提问对白 (who, what)，choice 事件消费
    "menu_prompt_cache": {},  # id(Menu 节点) -> (who, what)，AST 反查按菜单缓存
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


def _scan_last_say_store():
    """store._last_say_what 直读：偏移 0 精确命中正在显示的台词。

    引擎在 Say.execute 进正文显示之前写入该值，显示期间常驻（finally 只清
    _last_raw_what）——「注入时该句已经在屏上」的场景（该句执行早于注入，
    text_filter 不会再触发）也能取到当前句，补上最后的盲区。who 取配套的
    _last_say_who，经 store 解析为展示名，与 history/过滤器源去重键一致。
    """
    try:
        import renpy

        what = getattr(renpy.store, "_last_say_what", None)
        who = getattr(renpy.store, "_last_say_who", None)
    except BaseException:
        return None
    what = _to_text(what)
    if not what:
        return None
    return (_who_from_raw(who), what)


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
    """统一的出口：清洗文本 + 去重 + 入队。

    入队失败（发送队列未就绪等）时回退去重标记与计数：允许后续轮询重新发布
    同一句，避免这一句因一次入队失败就被永久吞掉。
    """
    try:
        what = _clean_text(_to_text(what))
        if not what:
            return False
        pending = _STATE.get("pending_prompt")
        if pending is not None and pending[1] == what:
            # 该句已挂起为 menu 提问：其余捕获源（含 begin 事件的 force=True）不重复上报
            return False
        who = _clean_text(_to_text(who))
        if not force and what == _STATE["last_what"] and who == _STATE["last_who"]:
            return False
        previous_what = _STATE["last_what"]
        previous_who = _STATE["last_who"]
        _STATE["last_what"] = what
        _STATE["last_who"] = who
        _STATE["last_publish"] = time.time()
        _STATE["captured"] = _STATE["captured"] + 1
        if not _emit(
            {
                "t": "say",
                "who": who,
                "what": what,
                "src": source,
                "ts": time.time(),
            }
        ):
            _STATE["last_what"] = previous_what
            _STATE["last_who"] = previous_who
            _STATE["captured"] = _STATE["captured"] - 1
            return False
        return True
    except BaseException as exc:
        _note_error("publish", exc)
        return False


def _character_callback(event, **kwargs):
    """config.all_character_callbacks 的接收端。"""
    try:
        if event == "begin":
            # begin 事件携带完整台词；即使与上一句完全相同也应输出，所以 force=True。
            # Menu caption 的旁白（narration）在此挂起，由随后的 choice 事件合并呈现
            _maybe_suspend_menu_narration(kwargs.get("what"))
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


def _who_from_raw(raw):
    """把 who 原始表达式解析为展示名：简单变量名经 store 取角色名。

    与 history/过滤器源的口径一致（同为展示名），保证各路去重键相同；
    解析失败（旁白/复杂表达式）返回空串，不误报旧角色。
    """
    try:
        import renpy

        if not raw:
            return ""
        character = getattr(renpy.store, raw, None)
        if character is None:
            return ""
        return _clean_text(_to_text(getattr(character, "name", None) or ""))
    except BaseException:
        return ""


def _who_from_statement(node):
    """从 Say 节点解析展示用说话人（``node.who`` 为角色变量名时经 store 取角色名）。"""
    return _who_from_raw(getattr(node, "who", None))


def _consume_stale_prompt(current_what):
    """挂起的提问没等到 menu（如全部选项被条件排除、menu 块被跳过）：补报后清空。

    若挂起句与当前句是同一句（回退重放同一提问），静默清空交由重新挂起，
    不产生重复上报。
    """
    pending = _STATE.get("pending_prompt")
    if not pending:
        return
    _STATE["pending_prompt"] = None
    if pending[1] and pending[1] != current_what:
        _publish(pending[0], pending[1], "pending_prompt", force=True)


def _say_text_filter(text):
    """链式包装 say_menu_text_filter：在台词显示瞬间捕获文本（「落后一句」根治）。

    Ren'Py 在 ``Say.execute`` 进正文显示之前调用本过滤器，而 ``_history_list``
    的当前句要等交互结束（do_done）才入账——在 7.3/8.0/8.1 等旧引擎上，这是
    唯一能拿到显示瞬间当前句的公开钩子。双重判别：

    - 当前执行节点必须是 Say（排除 Menu 选项文本等其他过滤调用）；
    - 传入文本与当前节点原文必须一致（预测路径会用未来节点调用本过滤器，
      不一致即预测调用，跳过以免提前上报未显示的台词）。

    特例：当前 Say 的静态 next 是 Menu（提问紧邻分支选项）时挂起该句
    （``pending_prompt``）不单独上报，由随后的 choice 事件合并呈现——
    避免「提问先上屏、又被选项整段替换清掉」的两拍观感。

    文本按原始原文上报（与既有扫描源一致，由 _clean_text 去标签）。
    """
    try:
        import renpy

        node = renpy.game.context().current
        if type(node).__name__ == "Say" and getattr(node, "what", None) == text:
            who = _who_from_statement(node)
            what = _to_text(text)
            _consume_stale_prompt(_clean_text(what))
            nxt = getattr(node, "next", None)
            if nxt is not None and type(nxt).__name__ == "Menu":
                _STATE["pending_prompt"] = (who, _clean_text(what))
            else:
                _publish(who, what, "text_filter")
    except BaseException:
        pass
    prev = _STATE.get("say_text_filter_prev")
    if callable(prev):
        return prev(text)
    return text


def _restore_say_text_filter():
    try:
        import renpy

        renpy.config.say_menu_text_filter = _STATE.get("say_text_filter_prev")
    except BaseException as exc:
        _note_error("restore_say_text_filter", exc)


def _periodic_callback():
    """config.periodic_callbacks：约 20Hz 被调用，内部再按 poll_hz 限流。"""
    try:
        config = _STATE["config"]
        now = time.time()
        interval = 1.0 / max(1.0, float(config.get("poll_hz", 7.0)))
        if now - _STATE["last_publish"] < interval * 0.5:
            return
        # 两层扫描：当前句源（say 界面 / store 直读 / 语句节点）优先——任一有值
        # 即停（是否新值交给去重判定）；仅当它们都无值时才轮到历史兜底源。
        # 这样旧值（典型为 history 的上一句）既不会占据优先位饿死当前句源，
        # 也不会在当前句已发布后翻旧账造成乒乓重复上报
        for scanner in (_scan_screen, _scan_last_say_store, _scan_statement):
            found = scanner()
            if not found:
                continue
            _publish(found[0], found[1], "poll")
            break
        else:
            found = _scan_history()
            if found:
                _publish(found[0], found[1], "poll")
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


def _menu_node_at_menu():
    """解析当前执行点上的 Menu 节点对象，兼容两种引擎形态。

    旧引擎 ``context().current`` 是节点对象；8.3+ 是节点名字符串
    （execution.py：``self.current = node.name``），经 script.namemap
    （lookup 的底层数据）回查。menu 语句执行期间（exports_menu 主 hook 与
    choice screen 轮询兜底、以及 Menu.execute 内 narration 的旁白上报点）
    current 均停留在 Menu 节点上，不随 next_node 提前移动。
    """
    try:
        import renpy

        cur = renpy.game.context().current
    except BaseException:
        return None
    if cur is not None and type(cur).__name__ == "Menu":
        return cur
    if cur:
        try:
            found = renpy.game.script.lookup(cur)
        except BaseException:
            return None
        if found is not None and type(found).__name__ == "Menu":
            return found
    return None


def _menu_narration(menu_node):
    """提取 Menu 节点自带的 caption/narration 条目，按引擎口径合并成单条文本。

    引擎 Menu.execute 把 ``block is None`` 且 ``config.narrator_menu`` 开启
    的条目收集为 narration，在选项出现前以 ``renpy.exports.say(None, join,
    interact=False)`` 旁白形式说出（menu 块的提问对白本体）。这里复刻同一
    收集条件；narration 不属于任何分支 block，只取 menu 显示前必然说出的
    内容，绝不预读分支体。
    """
    try:
        import renpy

        if not getattr(renpy.config, "narrator_menu", False):
            return ""
        items = getattr(menu_node, "items", None)
        if not isinstance(items, (list, tuple)):
            return ""
        narration = []
        for entry in items:
            if not isinstance(entry, (list, tuple)) or len(entry) < 3:
                continue
            if entry[2] is not None:
                continue  # 有 block 的条目是真选项，block 体内内容绝不预读
            label = _clean_text(_to_text(entry[0]))
            if label:
                narration.append(label)
        if not narration:
            return ""
        return "\n".join(narration)
    except BaseException:
        return ""


def _maybe_suspend_menu_narration(text):
    """挂起 Menu caption：narration 经 exports.say 直调发出，与选项几乎同时
    发布，应随 choice 事件合并呈现而非单独上屏。

    begin 事件触发时当前执行点仍在 Menu 上（current 不变），文本与该菜单
    的 narration 汇总一致即挂起；随后 _publish 的挂起拦截会抑制这条上报。
    """
    try:
        menu_node = _menu_node_at_menu()
        if menu_node is None:
            return
        narration = _menu_narration(menu_node)
        if narration and narration == _clean_text(_to_text(text)):
            _STATE["pending_prompt"] = ("", narration)
    except BaseException:
        pass


def _menu_prompt_from_ast(menu_node):
    """反查 AST：静态 next 指向该 Menu 的前驱语句是 Say 才算提问（前置 say 形态）。

    只向前看（已显示的过去）：不读 Menu 分支体、不沿 next 向后遍历；jump 的
    next 指向静态下一节点而非跳转目标，jump 干扰会自然判否。语句表优先取
    script.namemap（节点名 → 节点对象的全量映射，8.3+ 移除 all_stmts 方法后
    仍可用；新引擎上 all_stmts 是列表属性，callable 判别排除），兼容旧引擎
    的 script.all_stmts()；结果由调用方按菜单节点缓存。
    """
    try:
        import renpy

        namemap = getattr(renpy.game.script, "namemap", None)
        if isinstance(namemap, dict):
            stmts = list(namemap.values())
        else:
            all_stmts = getattr(renpy.game.script, "all_stmts", None)
            stmts = all_stmts() if callable(all_stmts) else None
        if not stmts:
            return ("", "")
        for stmt in stmts:
            if getattr(stmt, "next", None) is menu_node:
                if type(stmt).__name__ != "Say":
                    return ("", "")
                what = _clean_text(_to_text(getattr(stmt, "what", "")))
                if not what:
                    return ("", "")
                return (_who_from_statement(stmt), what)
    except BaseException:
        pass
    return ("", "")


def _resolve_menu_prompt():
    """解析该 menu 块的紧邻提问对白 ``(who, what)``，供 choice 事件合并呈现。

    优先级：挂起句（前置 say 紧邻 menu 由 _say_text_filter 挂起；caption 由
    _maybe_suspend_menu_narration 挂起）→ Menu 自带 caption（menu 内无 block
    字符串行，引擎在选项出现前以旁白说出）→ AST 静态前驱 Say（新引擎上
    current 是节点名字符串、text_filter 挂起不生效，由 lookup 回查节点后
    反查兜底）。都取不到返回空串——工具端维持仅显示选项列表的现状，绝不拿
    对话历史旧台词冒充提问。
    """
    pending = _STATE.get("pending_prompt")
    _STATE["pending_prompt"] = None
    menu_node = _menu_node_at_menu()
    if menu_node is not None:
        narration = _menu_narration(menu_node)
        if narration:
            # caption 是 menu 自带提问，优先合并；此前挂起的普通对话照常补报，不丢句
            if pending and pending[1] and pending[1] != narration:
                _publish(pending[0], pending[1], "pending_prompt", force=True)
            _log("info", "[agent] menu 提问合并（caption）：%s" % narration[:40])
            return ("", narration)
    if pending and pending[1]:
        _log("info", "[agent] menu 提问合并（挂起）：%s" % pending[1][:40])
        return (_clean_text(_to_text(pending[0])), pending[1])
    if menu_node is None:
        return ("", "")
    cache = _STATE.get("menu_prompt_cache")
    if not isinstance(cache, dict):
        cache = {}
        _STATE["menu_prompt_cache"] = cache
    if id(menu_node) in cache:
        return cache[id(menu_node)]
    found = _menu_prompt_from_ast(menu_node)
    cache[id(menu_node)] = found
    if found[1]:
        _log("info", "[agent] menu 提问合并（前驱）：%s：%s" % (found[0] or "-", found[1][:40]))
    return found


def _publish_choice(items, source="exports_menu", force=False):
    """菜单出现：上报选项原文列表与该菜单的紧邻提问对白。

    以选项文本组合作指纹去重：choice screen 轮询兜底 20Hz 重复扫同一屏时
    只报一次；exports_menu 主 hook 以 ``force=True`` 调用 —— 回退重放、从
    选项节点存档载入等场景会重新执行 menu 语句，必须视为新的选项事件重新
    上报（否则工具端停留在重放的旧句上、选项被忽略）。菜单关闭后由轮询
    清空指纹。``who``/``what`` 经 _resolve_menu_prompt 解析（挂起句 + AST
    静态前驱，仅认紧邻提问；无则为空串，绝不取对话历史旧台词填充）。
    实测验证的引擎版本与测试游戏见 _install_now 的 exports_menu 注释。
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
    prompt_who, prompt_what = _resolve_menu_prompt()
    # 副作用采集（事前全量）：从 Menu AST 节点提取各选项 $ 语句原文；失败为
    # None，工具端条目仅记录选项文本（静默降级，绝不抛回游戏）
    effects = _collect_effects(_menu_node_at_menu(), captions)
    _STATE["captured"] = _STATE["captured"] + 1
    return _emit(
        {
            "t": "choice",
            "items": captions,
            "effects": effects,
            "who": prompt_who,
            "what": prompt_what,
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
        # 每次都视为新的选项事件重新上报；轮询兜底的指纹去重不受影响
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


def _source_lines(source):
    """把 PyCode.source 拆成非空行列表（strip 后保留原文；非文本返回空）。"""
    if not isinstance(source, (bytes, type(""))):
        return []
    text = _to_text(source).replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _extract_effect_lines(stmt, out, stats):
    """从单个语句节点提取副作用语句行（按 PyCode.source 语义判别）。

    规则：``code.source`` 逐行 strip 后以 ``$`` 开头的行各记一条
    （``$ xxx = True`` / ``$ xxx += 1`` 必命中，保留 ``$`` 前缀——摘要
    解析已兼容两种来源）；无 ``$`` 行但类名为 ``Python``（无前缀的版本
    形态 / ``python:`` 块）整条折叠收入。``Say``（无 code）、
    ``UserStatement``（play/queue 等，source 不以 $ 开头）、``Show``/
    ``Hide``/``With`` 等混排语句按此语义天然排除——``$`` 与混排语句
    同块并存不再互相干扰。只读文本、绝不求值（求值会真实触发游戏
    副作用，红线）。
    """
    lines = _source_lines(getattr(getattr(stmt, "code", None), "source", None))
    dollar = [line for line in lines if line.startswith("$")]
    if dollar:
        out.extend(dollar)
        stats["py"] = stats.get("py", 0) + len(dollar)
        return
    name = type(stmt).__name__
    if name == "Python":
        if lines:
            out.append(" ; ".join(lines))
        else:
            stats["missing"] = stats.get("missing", 0) + 1
        stats["py"] = stats.get("py", 0) + 1
    elif lines:
        stats["mixed"] = stats.get("mixed", 0) + 1


def _if_entry_blocks(stmt):
    """If 节点的分支块列表：entries=[(cond, block)…] 为主，旧 block 属性兼容。"""
    blocks = []
    entries = getattr(stmt, "entries", None)
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                blocks.append(entry[1])
    if not blocks:
        block = getattr(stmt, "block", None)
        if block is not None:
            blocks.append(block)
    return blocks


def _safe_child_nodes(stmt):
    """引擎自带子节点迭代（Node.get_children）的防御包装：异常/空安全。"""
    getter = getattr(stmt, "get_children", None)
    if not callable(getter):
        return []
    try:
        children = getter()
    except BaseException:
        return []
    if children is None:
        return []
    try:
        return list(children)
    except BaseException:
        return []


def _walk_effect_block(block, out, stats, depth):
    """递归遍历选项 block：逐节点按语义提取副作用行。

    ``$`` 与 ``mc`` / ``play`` / ``show`` / ``hide`` / ``with`` 混排时各
    语句是独立的兄弟节点，非副作用语句由 :func:`_extract_effect_lines`
    按语义排除，不再影响采集。``If`` 按分支块向下递归（entries 为主、
    旧 block 属性兼容）；嵌套 ``Menu`` 的选项 block 归属内层菜单，不并入
    外层选项；其余带子块的语句（While 等）经 ``get_children`` 取一层子
    节点提取。递归限深，只读结构、绝不求值。
    """
    if not isinstance(block, (list, tuple)) or depth > 2:
        return
    for stmt in block:
        _extract_effect_lines(stmt, out, stats)
        name = type(stmt).__name__
        if name == "If":
            for sub in _if_entry_blocks(stmt):
                _walk_effect_block(sub, out, stats, depth + 1)
            continue
        if name == "Menu":
            continue  # 嵌套菜单：归属内层菜单，不并入外层选项
        if depth < 2:
            for child in _safe_child_nodes(stmt):
                _extract_effect_lines(child, out, stats)


def _find_statement_block(elements, fallback_index):
    """在 items 每项的元素序列里定位语句块；失败回退旧口径 [1]。

    判据：元素本身是序列且首元素像语句节点（带 ``location`` 或 ``next``
    属性——renpy.ast.Node 的公共特征）。对 ``(caption, block, label)``
    与任何位序/结构变体稳健；全部探测失败时回退 ``elements[1]``（旧行
    为），并在 ``stats`` 记 fallback 供观测摘要携带。
    """
    fallback = elements[fallback_index] if fallback_index < len(elements) else None
    for position, element in enumerate(elements):
        if position == 0 or not isinstance(element, (list, tuple)) or not element:
            continue  # [0] 是 caption 文本；空序列不像语句块
        first = element[0]
        if hasattr(first, "location") or hasattr(first, "next"):
            return element, False
    return fallback, True


def _match_blocks_by_caption(visible, captions):
    """按 caption 精确匹配：全部一一命中才返回元素序列列表，否则 None。"""
    pool = list(visible)
    matched = []
    for caption in captions:
        hit = None
        for j, pair in enumerate(pool):
            if pair[0] == caption:
                hit = j
                break
        if hit is None:
            return None
        matched.append(pool[hit][1])
        pool.pop(hit)
    return matched


def _collect_effects(node, captions):
    """从当前 Menu AST 节点提取各选项对应的 $ 语句源码列表（事前全量）。

    数据来源：:func:`_menu_node_at_menu`（menu 交互期间 current 即 Menu 节点），
    节点 ``items`` 每项含 caption 与**选项 block**（真实位序按
    :func:`_find_statement_block` 探测：序列且首元素为语句节点的元素；
    探测失败回退 ``[1]`` 旧口径），caption 为纯字符串或 ``(cond, text)``
    元组（带 ``if`` 条件的隐藏项，已被引擎过滤不在运行时 items 中）。

    语句判别：按 ``PyCode.source`` 逐行匹配 ``$`` 前缀（:func:
    `_extract_effect_lines`），类名 ``Python`` 的节点整条兜底、``If``
    分支递归、其余带子块语句取一层子节点——``$`` 与 show / play / with
    等混排不影响采集。

    对齐策略（AST items 含隐藏项、caption 可能与玩家所见语言不一致——
    translate 块下 AST 是脚本原文语言）：可见项数与运行时选项数相等时按
    顺序贪心对齐；不等时仅接受 caption 完全一一命中的形态；仍不中返回
    None——宁可整条降级也不输出错位的副作用。

    覆盖条件：副作用写在选项 block 内 + ``PyCode.source`` 保留 + 引擎未魔
    改；任何一环不满足即返回 None（静默降级，工具端条目仅记录选项文本）。

    观测日志：每个降级分支回灌一条 warning（带具体原因），成功采集回灌
    一条 info 摘要（选项数 / 每项语句数 / 对齐与 block 定位方式 / 计数）
    ——仅在菜单真正执行时各输出一次（主 hook 每菜单一次；轮询兜底被指纹
    去重挡住不会重复），不读取任何游戏状态、绝不影响游戏。
    """
    try:
        node_name = type(node).__name__ if node is not None else "None"
        if node is None or node_name != "Menu":
            _log(
                "warning",
                "[agent] effects 采集降级：当前执行点非 Menu 节点（type=%s）" % node_name,
            )
            return None
        ast_items = getattr(node, "items", None)
        if not isinstance(ast_items, (list, tuple)) or not ast_items:
            _log(
                "warning",
                "[agent] effects 采集降级：Menu 节点 items 形态异常（type=%s）"
                % type(ast_items).__name__,
            )
            return None
        visible = []
        for item in ast_items:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            caption = item[0]
            if isinstance(caption, (list, tuple)):
                if len(caption) < 2:
                    continue
                caption = caption[1]
            visible.append((_to_text(caption), list(item)))
        if not visible:
            _log("warning", "[agent] effects 采集降级：AST items 全部形态异常")
            return None
        matched = None
        if len(visible) == len(captions):
            matched = [visible[i][1] for i in range(len(captions))]
            align_mode = "顺序贪心（可见 %d 项 = 选项 %d 项）" % (len(visible), len(captions))
        else:
            matched = _match_blocks_by_caption(visible, captions)
            align_mode = "caption 精确匹配（可见 %d 项 / 选项 %d 项）" % (
                len(visible),
                len(captions),
            )
        if matched is None:
            _log(
                "warning",
                "[agent] effects 采集降级：对齐失败（%s；宁整条降级不错位）" % align_mode,
            )
            return None
        effects = []
        stats = {"py": 0, "missing": 0, "mixed": 0, "fallback": 0}
        for elements in matched:
            block, is_fallback = _find_statement_block(elements, 1)
            if is_fallback:
                stats["fallback"] = stats.get("fallback", 0) + 1
            lines = []
            _walk_effect_block(block, lines, stats, 0)
            effects.append(lines)
        _log(
            "info",
            "[agent] effects 上报摘要：%d 选项，语句数 %s（对齐=%s；block=探测 %d/回退 %d；"
            "$ 节点 %d 个，source 缺失 %d 个，混排排除 %d 个）"
            % (
                len(captions),
                [len(lines) for lines in effects],
                align_mode,
                len(captions) - stats.get("fallback", 0),
                stats.get("fallback", 0),
                stats.get("py", 0),
                stats.get("missing", 0),
                stats.get("mixed", 0),
            ),
        )
        return effects
    except BaseException as exc:
        _log(
            "warning",
            "[agent] effects 采集异常降级：%s: %s" % (type(exc).__name__, exc),
        )
        return None


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

        # 显示瞬间捕获（旧引擎「落后一句」的根治）：say_menu_text_filter 在
        # Say.execute 进正文显示前被调用，比 _history_list 入账（do_done）
        # 早整整一个交互周期；链式包装并记录原值，注销时还原
        _STATE["say_text_filter_prev"] = getattr(config, "say_menu_text_filter", None)
        try:
            config.say_menu_text_filter = _say_text_filter
            _STATE["remove_hooks"].append(_restore_say_text_filter)
            layers.append("say_text_filter")
        except BaseException as exc:
            _note_error("say_text_filter", exc)

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
    _STATE["say_text_filter_prev"] = None
    _STATE["exports_menu_prev"] = None
    _STATE["last_menu"] = None
    _STATE["last_choice_captions"] = None
    _STATE["pending_prompt"] = None
    _STATE["menu_prompt_cache"] = {}
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
