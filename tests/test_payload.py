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
