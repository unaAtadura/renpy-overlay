"""流式悬浮窗的视觉参数常量：取值沿用参考项目的已验证方案。

被 ``windows`` 渲染层与 ``scroll_logic`` 蒙版条带计算共同引用；只增不改取值。
"""

from __future__ import annotations

BODY_MARGIN = 24.0  # 正文窗文本边距（逻辑像素，参考项目全屏窗用 64）
SPAWN_DURATION = 0.0  # 已废弃置 0：正文窗逐字出现动画已移除，字符直接在最终
# 位置绘制；保留符号维持 stream_window 门面的历史公开符号兼容
TYPE_INTERVAL_MS = 16  # 打字机节拍（~60 字/秒，积压越多消化越快）
RENDER_INTERVAL_MS = 16  # 重绘刷新周期（驱动平滑滚动与滚动插值）
TITLE_MARGIN_X = 16.0  # 标题文本左起点（逻辑像素）
TITLE_GLOW_FACTOR = 1.7  # 标题窗高度余量的字号倍数（保持既有几何外观）
BODY_FILL_COLOR = "#ADD8E6"  # 标题与正文统一的淡蓝色填充

#: 文字背后的半透明黑色蒙版（提升游戏画面上的对比度）：仅覆盖有文字的行区域，
#: 行距区域保持透明；alpha 取 110/255 ≈ 43%（可读性与遮挡感的平衡取值）
MASK_COLOR = "#000000"
MASK_ALPHA = 110
MASK_PAD_X = 6.0  # 条带水平内边距：文字与蒙版边缘不直接贴合
MASK_PAD_Y = 1.0  # 条带垂直内收：保证相邻两行蒙版之间留出空隙
MASK_RADIUS = 4.0  # 条带圆角半径
