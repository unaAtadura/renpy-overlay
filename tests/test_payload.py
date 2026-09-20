"""注入源码的静态校验。

agent.py 会被送进游戏进程由**目标自带的解释器**执行（可能是 Python 2.7），
本机测试环境跑不起来它 —— 所以这里做"静态契约"检查：

1. 不含 Python 2.7 解析不了的语法（f-string、注解、海象、nonlocal、关键字限定参数……）；
2. 顶层 import 只允许标准库白名单，且绝不包含工具端专有模块；
3. 引导模板与 agent 之间的接口（``start`` / ``shutdown`` / ``status``）保持对齐；
4. base64 内嵌的 agent 源码与配置能精确往返。
"""

from __future__ import annotations

import ast
import base64
import json
import re

import pytest

from renpy_overlay import injector

ALLOWED_TOP_LEVEL_IMPORTS = {"json", "os", "re", "socket", "sys", "threading", "time"}


def _tree(source: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as exc:  # pragma: no cover - 失败信息更直观
        pytest.fail(f"注入源码无法解析：{exc}")


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    mapping: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            mapping[child] = node
    return mapping


def _assert_py27_safe(source: str, label: str) -> None:
    """逐节点断言源码不含 Python 2.7 不支持的语法。"""
    tree = _tree(source)
    parent = _parents(tree)
    for node in ast.walk(tree):
        name = type(node).__name__
        assert name != "JoinedStr", f"{label}: f-string 不被 Python 2.7 支持"
        assert name != "FormattedValue", f"{label}: f-string 片段"
        assert name != "NamedExpr", f"{label}: 海象运算符（:=）不被 Python 2.7 支持"
        assert name != "Nonlocal", f"{label}: nonlocal 是 Python 3 语法"
        assert name != "YieldFrom", f"{label}: yield from 是 Python 3 语法"
        assert name != "AsyncFunctionDef", f"{label}: async 是 Python 3 语法"
        assert name != "AnnAssign", f"{label}: 变量注解是 Python 3 语法"
        assert name != "Match", f"{label}: match 语句是 Python 3.10 语法"
        if isinstance(node, (ast.FunctionDef, ast.Lambda)):
            args = node.args
            assert not args.kwonlyargs, f"{label}: 关键字限定参数（*, x）是 Python 3 语法"
            assert not getattr(args, "posonlyargs", []), (
                f"{label}: 仅位置参数（/）是 Python 3.8 语法"
            )
            every_arg = list(args.args) + list(args.kwonlyargs)
            if args.vararg is not None:
                every_arg.append(args.vararg)
            if args.kwarg is not None:
                every_arg.append(args.kwarg)
            assert all(arg.annotation is None for arg in every_arg), (
                f"{label}: 参数注解是 Python 3 语法"
            )
            assert getattr(node, "returns", None) is None, f"{label}: 返回注解是 Python 3 语法"
        if isinstance(node, ast.Raise):
            assert node.cause is None, f"{label}: raise ... from ... 是 Python 3 语法"
        if isinstance(node, ast.Starred):
            # py2.7 只在函数调用里支持 *args 展开；赋值/列表里的星号解包是 py3.5+
            assert isinstance(parent.get(node), ast.Call), (
                f"{label}: 星号解包只允许出现在函数调用中"
            )
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            pytest.fail(f"{label}: 不应使用 print（py2 里会按元组打印），日志请走 IPC")


# ------------------------------------------------------------------ agent 源码


@pytest.fixture(scope="module")
def agent_text() -> str:
    raw = injector.agent_source()
    assert raw, "payload/agent.py 不应为空"
    return raw.decode("utf-8")


def test_agent_declares_utf8_coding(agent_text):
    # 游戏端拿到的是 UTF-8 字节，没有这行声明 py2 会因为中文注释直接 SyntaxError
    assert agent_text.startswith("# -*- coding: utf-8 -*-")


def test_agent_public_entrypoints_exist(agent_text):
    names = {node.name for node in _tree(agent_text).body if isinstance(node, ast.FunctionDef)}
    assert {"start", "shutdown", "status"} <= names, "引导模板依赖这三个入口"


def test_agent_is_python27_safe(agent_text):
    _assert_py27_safe(agent_text, "agent.py")


def test_agent_top_level_imports_only_stdlib(agent_text):
    for node in _tree(agent_text).body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] in ALLOWED_TOP_LEVEL_IMPORTS, (
                    f"顶层 import 了意外模块：{alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in ALLOWED_TOP_LEVEL_IMPORTS, (
                f"顶层 import 了意外模块：{node.module}"
            )


def test_agent_never_imports_tool_side_modules(agent_text):
    forbidden = {"psutil", "win32api", "win32gui", "tkinter", "typer", "requests"}
    for node in ast.walk(_tree(agent_text)):
        if isinstance(node, ast.Import):
            assert not ({alias.name.split(".")[0] for alias in node.names} & forbidden)
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in forbidden


# ------------------------------------------------- 分支选项捕获（离线 exec）


def _load_agent_module(agent_text: str):
    """把 agent 源码 exec 到独立命名空间：顶层只依赖标准库，可离线执行纯逻辑。"""
    import types

    module = types.ModuleType("agent_under_test")
    module.__dict__["__name__"] = "agent_under_test"
    exec(compile(agent_text, "agent.py", "exec"), module.__dict__)
    return module


def _fresh_queue_module(agent_text: str):
    import collections

    module = _load_agent_module(agent_text)
    module._STATE["queue"] = collections.deque()
    return module


def test_menu_choice_capture_roundtrip(agent_text):
    """选项出现上报 items 与上下文；玩家所选（8.x 形态：value 在三元组第三位）
    映射回序号与文本。"""
    module = _fresh_queue_module(agent_text)
    # Ren'Py 8.x menu_actions=True：Menu.execute 传入 (label, condition, value)
    items = [("Accept", "True", "label_a"), ("Reject", "True", "label_b")]
    assert module._publish_choice(items) is True
    shown = module._STATE["queue"][-1]
    assert shown["t"] == "choice"
    assert shown["items"] == ["Accept", "Reject"]
    # 本机无 renpy：_scan_history 降级为空上下文
    assert shown["who"] == "" and shown["what"] == ""
    assert module._publish_choice_pick("label_b") is True
    pick = module._STATE["queue"][-1]
    assert pick["t"] == "choice_pick"
    assert pick["index"] == 1
    assert pick["caption"] == "Reject"


def test_menu_choice_pick_matches_caption_and_legacy_two_tuple(agent_text):
    """匹配兼容：caption 匹配、旧二组形态（value 在第二位）也支持。"""
    module = _fresh_queue_module(agent_text)
    module._publish_choice([("接受", "yes")])  # 旧二组形态 (label, value)
    assert module._publish_choice_pick("yes") is True
    pick = module._STATE["queue"][-1]
    assert pick["index"] == 0 and pick["caption"] == "接受"


def test_menu_choice_pick_unmatched_falls_back_to_caption(agent_text):
    """返回值与 items 不匹配（版本差异/自定义返回）：降级为仅上报文本。"""
    module = _fresh_queue_module(agent_text)
    module._publish_choice([("接受", "True", "a")])
    assert module._publish_choice_pick("weird-value") is True
    pick = module._STATE["queue"][-1]
    assert pick["index"] == -1
    assert pick["caption"] == "weird-value"


def test_menu_choice_invalid_inputs_ignored(agent_text):
    """非法 items / 菜单被跳过（返回 None）时不上报，不产生噪音消息。"""
    module = _fresh_queue_module(agent_text)
    assert module._publish_choice(None) is False
    assert module._publish_choice("not-a-list") is False
    assert module._publish_choice_pick(None) is False
    assert len(module._STATE["queue"]) == 0


def test_menu_choice_replay_reports_again_with_force(agent_text):
    """回退重放/存档载入同一菜单：exports_menu 主 hook（force=True）每次重新
    上报；choice screen 轮询兜底保持指纹去重。"""
    module = _fresh_queue_module(agent_text)
    items = [("Yes", "True", "y"), ("No", "True", "n")]
    assert module._publish_choice(items, force=True) is True
    assert module._publish_choice(items, force=True) is True  # 重放：不被指纹拦截
    assert len(module._STATE["queue"]) == 2
    # 轮询兜底路径：同一菜单仍在屏时仍按指纹去重
    assert module._publish_choice(items, source="choice_screen") is False
    assert len(module._STATE["queue"]) == 2


def test_menu_captions_skips_malformed_items(agent_text):
    """items 中的非三元组元素被跳过，不影响其余选项解析。"""
    module = _load_agent_module(agent_text)
    captions = module._menu_captions(
        [("选项一", "a", False), "junk", (42,), ("选项二", "b", True)]
    )
    assert captions == ["选项一", "选项二"]


def test_menu_captions_supports_entry_objects(agent_text):
    """choice screen 传入的带 caption 属性的条目对象同样可解析。"""
    import types

    module = _load_agent_module(agent_text)
    entries = [
        types.SimpleNamespace(caption="Yes", action="a", chosen=False),
        types.SimpleNamespace(caption="No", action="b", chosen=True),
    ]
    assert module._menu_captions(entries) == ["Yes", "No"]


def test_exports_menu_wrapper_captures_and_forwards(agent_text):
    """包装器：先上报 choice，再透传原调用，返回后上报 choice_pick。"""
    module = _fresh_queue_module(agent_text)
    calls = []

    def fake_menu(items, set_expr=None, **kwargs):
        calls.append((items, set_expr, kwargs))
        return "label_b"

    module._STATE["exports_menu_prev"] = fake_menu
    items = [("Yes", "True", "label_a"), ("No", "True", "label_b")]
    result = module._exports_menu_wrapper(items, None)
    assert result == "label_b"
    assert calls == [(items, None, {})]
    kinds = [message["t"] for message in module._STATE["queue"]]
    assert kinds == ["choice", "choice_pick"]
    pick = module._STATE["queue"][-1]
    assert pick["index"] == 1 and pick["caption"] == "No"


def test_exports_menu_wrapper_propagates_game_exception(agent_text):
    """原函数异常必须原样抛回游戏；异常路径不产生 choice_pick。"""
    module = _fresh_queue_module(agent_text)

    def boom(items):
        raise RuntimeError("game error")

    module._STATE["exports_menu_prev"] = boom
    with pytest.raises(RuntimeError, match="game error"):
        module._exports_menu_wrapper([("A", "True", "a")])
    assert [message["t"] for message in module._STATE["queue"]] == ["choice"]


def test_menu_choice_screen_fallback_poll(agent_text, monkeypatch):
    """轮询兜底：choice screen 在屏时从 scope 读 items 上报；同一菜单不重复报；
    菜单关闭后清指纹，同一菜单再次出现可重新上报。"""
    import sys
    import types

    module = _fresh_queue_module(agent_text)
    screen = types.SimpleNamespace(
        scope={"items": [("Yes", "y", False), ("No", "n", False)]}
    )
    fake = types.SimpleNamespace(get_screen=lambda name: screen)
    monkeypatch.setitem(sys.modules, "renpy", fake)

    assert module._scan_choice_screen() is True
    shown = module._STATE["queue"][-1]
    assert shown["t"] == "choice" and shown["items"] == ["Yes", "No"]
    assert shown["src"] == "choice_screen"

    # 同一菜单仍在屏：返回值仍为 True（表示在屏），但指纹去重不重复上报
    assert module._scan_choice_screen() is True
    assert len(module._STATE["queue"]) == 1

    # 菜单关闭：清指纹
    fake.get_screen = lambda name: None
    assert module._scan_choice_screen() is False

    # 同一菜单再次出现：可重新上报
    module._STATE["queue"].clear()
    fake.get_screen = lambda name: screen
    assert module._scan_choice_screen() is True
    assert module._STATE["queue"][-1]["t"] == "choice"


# ------------------------------------------------------------------ 引导模板


def test_bootstrap_embeds_agent_and_config_intact():
    config = {"host": "127.0.0.1", "port": 43210, "token": "t0ken", "poll_hz": 7.0}
    source = injector.build_agent_bootstrap(config).decode("utf-8")

    assert "__RESULT_ADDR__" in source, "结果区地址留给 run_bootstrap 在分配后替换"
    for placeholder in ("__AGENT_B64__", "__CONFIG_B64__", "__AGENT_NAME__"):
        assert placeholder not in source, f"占位符 {placeholder} 未被替换"
    assert injector.AGENT_MODULE_NAME in source

    # 嵌入内容必须与 build_agent_bootstrap 的输入精确往返
    expected_agent = repr(base64.b64encode(injector.agent_source()))
    expected_config = repr(base64.b64encode(json.dumps(config, ensure_ascii=True).encode("utf-8")))
    assert expected_agent in source
    assert expected_config in source
    compile(source, "<bootstrap>", "exec")
    _assert_py27_safe(source, "bootstrap")


def test_bootstrap_reentrant_shutdown_guard():
    """重复注入时必须先卸掉旧代理（幂等），模板里要有 sys.modules 检查与 shutdown 调用。"""
    source = injector.build_agent_bootstrap({"port": 1}).decode("utf-8")
    assert 'sys.modules.get("' + injector.AGENT_MODULE_NAME + '")' in source
    assert ".shutdown()" in source


@pytest.mark.parametrize(
    "action,stage,api",
    [("unload", "unloaded", "_mod.shutdown()"), ("status", "status", "_mod.status()")],
)
def test_action_source_contracts(action, stage, api):
    source = injector.build_action_source(action).decode("utf-8")
    assert "__RESULT_ADDR__" in source
    assert "__ACTION__" not in source and "__AGENT_NAME__" not in source
    assert repr(action) in source
    assert api in source
    assert f'"stage": "{stage}"' in source
    assert injector.AGENT_MODULE_NAME in source
    compile(source, f"<action-{action}>", "exec")
    _assert_py27_safe(source, f"action:{action}")


def test_action_source_rejects_unknown_action():
    with pytest.raises(ValueError, match="未知动作"):
        injector.build_action_source("shutdown")


def test_templates_write_result_with_memmove():
    """结果区写入必须用 ctypes.memmove —— 不依赖任何平台的数组赋值行为。"""
    for source in (
        injector.build_agent_bootstrap({"port": 1}).decode("utf-8"),
        injector.build_action_source("unload").decode("utf-8"),
    ):
        assert "ctypes.memmove(_RESULT_ADDR" in source
        assert re.search(r"\+ b\"\\x00\"", source), "结果必须 NUL 结尾（工具端按 C 字符串读回）"


# ------------------------------------------------------------------ say 发布


def test_publish_dedup_blocks_repeats_and_allows_new_text(agent_text):
    module = _fresh_queue_module(agent_text)
    assert module._publish("Charles", "Same.", "poll") is True
    assert module._publish("Charles", "Same.", "poll") is False  # 重复文本被去重
    assert module._publish("Charles", "Other.", "poll") is True
    assert module._STATE["captured"] == 2


def test_publish_rolls_back_dedup_when_emit_fails(agent_text):
    """入队失败（队列未就绪）时回退去重标记：同一句可被后续轮询重发。"""
    import collections

    module = _load_agent_module(agent_text)
    module._STATE["queue"] = None  # 队列未就绪：_emit 直接失败
    assert module._publish("Charles", "Hello.", "poll") is False
    assert module._STATE["last_what"] == ""  # 去重标记回退
    assert module._STATE["captured"] == 0  # 计数同步回退

    module._STATE["queue"] = collections.deque()
    assert module._publish("Charles", "Hello.", "poll") is True  # 就绪后重发成功
    assert module._STATE["last_what"] == "Hello."
    assert module._STATE["captured"] == 1


# ---- say_menu_text_filter：显示瞬间捕获（旧引擎「落后一句」根治） ---------------


def _node(class_name: str, what, who=""):
    """伪造 AST 节点：类名即判别依据（Say / Menu）。"""
    node = type(class_name, (), {})()
    node.what = what
    node.who = who
    return node


def _fake_renpy(current_node, store=None):
    import types

    context = types.SimpleNamespace(current=current_node)
    return types.SimpleNamespace(
        store=store or types.SimpleNamespace(),
        game=types.SimpleNamespace(context=lambda: context),
        config=types.SimpleNamespace(say_menu_text_filter=None),
    )


def test_say_text_filter_publishes_at_display(agent_text, monkeypatch):
    """Say.execute 过滤调用：显示瞬间上报，who 经 store 解析为展示名，文本原样透传。"""
    import sys
    import types

    module = _fresh_queue_module(agent_text)
    store = types.SimpleNamespace(c=types.SimpleNamespace(name="Charles"))
    fake = _fake_renpy(_node("Say", "Good morning Dad.", who="c"), store)
    monkeypatch.setitem(sys.modules, "renpy", fake)

    assert module._say_text_filter("Good morning Dad.") == "Good morning Dad."
    message = module._STATE["queue"][-1]
    assert message["t"] == "say"
    assert message["who"] == "Charles"  # 与轮询源的展示名一致，去重键可匹合
    assert message["what"] == "Good morning Dad."
    assert message["src"] == "text_filter"


def test_say_text_filter_skips_predict_and_menu_and_chains(agent_text, monkeypatch):
    """预测调用（文本与当前节点不一致）与 Menu 过滤不发布；原过滤器链式透传。"""
    import sys
    import types

    module = _fresh_queue_module(agent_text)
    store = types.SimpleNamespace(c=types.SimpleNamespace(name="Charles"))
    fake = _fake_renpy(_node("Say", "当前句。", who="c"), store)
    monkeypatch.setitem(sys.modules, "renpy", fake)

    # 预测路径：传入未来句文本与当前执行节点原文不一致 → 不发布（防剧透）
    module._say_text_filter("未来句。")
    assert len(module._STATE["queue"]) == 0

    # 菜单节点的过滤调用（选项文本）→ 不发布（由 menu 钩子负责）
    fake.game.context().current = _node("Menu", "选项甲")
    module._say_text_filter("选项甲")
    assert len(module._STATE["queue"]) == 0

    # 链式：原有过滤器存在时调用并透传其返回值
    calls = []
    module._STATE["say_text_filter_prev"] = lambda text: calls.append(text) or text + "!"
    fake.game.context().current = _node("Say", "当前句。", who="c")
    assert module._say_text_filter("当前句。") == "当前句。!"
    assert calls == ["当前句。"]


# ------------------------------------------------------------------ 版本推断


@pytest.mark.parametrize(
    "name,expected",
    [
        ("python27.dll", "2.7"),
        ("python39.dll", "3.9"),
        ("python310.dll", "3.10"),
        ("python312.dll", "3.12"),
        ("python313.dll", "3.13"),
        ("python3.9.dll", "3.9"),
        ("python3.dll", ""),
        ("kernel32.dll", ""),
        ("", ""),
    ],
)
def test_dll_version_hint(name, expected):
    assert injector.dll_version_hint(name) == expected
