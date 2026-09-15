# -*- coding: utf-8 -*-
"""注入到目标进程内的源码包。

重要：本目录下的模块**不会**被工具端 import —— 它们经由
``importlib.resources`` 读成文本，随注入引导代码一起送进目标进程，再由游戏内置的
Python 解释器 ``exec`` 执行。因此：

- 必须兼容 Python 2.7（Ren'Py 7.x）与 Python 3.x（Ren'Py 8.x）两套语法，
  不能用 f-string、类型注解、nonlocal、关键字限定参数等新语法。
- 不能出现任何工具端专有的 import（psutil / win32api / typer 等）。
- 顶层不得执行有副作用的代码（游戏进程会在 ``exec`` 时立即执行顶层语句）。
"""
