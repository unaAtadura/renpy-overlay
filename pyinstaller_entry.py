"""PyInstaller 打包专用入口：仅用于生成 exe，不影响开发模式运行。

与 ``src/renpy_overlay/__main__.py`` 的区别：PyInstaller 把入口脚本当作顶层
``__main__`` 执行，入口脚本内部的相对导入（``from .cli import main``）没有
父包可依托，会直接报错。因此这里必须使用绝对导入，并在 spec 中把 ``src``
加入 ``pathex`` 以便静态分析找到 ``renpy_overlay`` 包。
"""

import sys

from renpy_overlay.cli import main

if __name__ == "__main__":
    sys.exit(main())
