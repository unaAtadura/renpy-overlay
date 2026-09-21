"""缓存键文本清洗：与运行时上报口径逐字节对齐的纯函数。

预构建写入 translations.db 的 ``original`` 必须与注入代理运行时上报的文本
完全一致，否则预填条目永远无法命中。两条口径的运行时出处：

- 对白：``payload/agent.py`` 的 ``_clean_text(_to_text(what))`` —— 去除
  ``{...}`` 文本标签、还原 ``{{`` / ``[[`` 转义、去除首尾空白。离线侧由
  :func:`clean_text` 复刻同一实现（agent.py 面向 py2/py3 不宜 import），
  等价性由 ``tests/test_pretranslate_cleaning.py`` 用样本表钉住；
- 分支选项：工具端 ``_handle_choice`` 对 items 逐项 ``strip`` 并剔除空项，
  再经 ``choice_translation_text``（现居 ``translation_cache``）编号拼接；
  离线侧由 :func:`build_choice_key` 复刻同一顺序。

本模块只依赖标准库，供离线解析与单元测试使用。
"""

from __future__ import annotations

import re

#: 与 agent._TAG_RE 同一正则：去除 ``{w}`` ``{i}`` 等文本标签（不含转义形态）
_TAG_RE = re.compile(r"\{[^{}]*\}")


def clean_text(text: str) -> str:
    """把引擎内文本清洗为缓存键原文（与 agent._clean_text 同一口径）。

    ``{{`` → ``{``、``[[`` → ``[`` 的转义先占位保护，再去掉全部 ``{...}``
    标签，最后还原占位并去除首尾空白。
    """
    text = text.replace("{{", "\x01").replace("[[", "\x02")
    text = _TAG_RE.sub("", text)
    text = text.replace("\x01", "{").replace("\x02", "[")
    return text.strip()


def build_choice_key(items: list[str]) -> str:
    """菜单选项组的缓存键原文（与运行时 _handle_choice 同一口径）。

    每项 ``strip``、剔除空项后经 ``choice_translation_text`` 编号拼接；
    全部为空时返回空串（调用方据此跳过该组）。
    """
    from ..translation_cache import choice_translation_text  # 局部导入避免环

    cleaned = [str(item).strip() for item in items if str(item).strip()]
    if not cleaned:
        return ""
    return choice_translation_text(cleaned)
