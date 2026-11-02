# AI 对话

对应源码：`src/renpy_overlay/screenshot/chat_window.py`（对话窗口）、`chat_store.py`（chat.db）、`chat_history_window.py`（对话历史窗口）、宿主入口 `stream/history_flow.py`。测试：`test_chat_window.py`、`test_chat_store.py`、`test_chat_history.py`。

## 1. 定位与入口

快捷菜单「对话」打开常规窗口，向 AI 提问辅助深入学习外语。与翻译/识曲共用"同时只允许一个 API 调用"互斥（冲突时忽略点击且不更新提示）。对话**不保存上下文**——每次发送都是独立请求（不携带历史消息）。

## 2. 窗口布局（chat_window.py）

自上而下：

1. **「保留发送消息」复选框**（默认勾选；取消勾选时发送成功后清空输入框）；
2. **双击锁定截图窗的色块下拉列表**——选择用于 OCR 的截图窗口；
3. **OCR 与发送按钮**；
4. **无滚动条输入框**：空时占位「按 Ctrl+Enter 即可发送。」；输入框内 Ctrl+Enter 发送。

窗口尺寸由 `chat_window_width/height` 控制（缺省回退流式正文窗的配置值，见 [config.md](config.md)）。

## 3. OCR 链路

- 点击 OCR：识别选中颜色窗口的区域原文——**复用截图翻译的隐藏-截屏-恢复时序**（`QuickMenu.hide_all` → DWM 结算 0.18s → `capture_region` → finally 恢复，详见 [screenshot.md](screenshot.md) §5）；
- 图像处理与请求走 vision 链路的 `recognize_image`（`DEFAULT_OCR_PROMPT`：只输出图中文字原文，不翻译不改写不添加解释——与翻译指令明确区分，避免模型自行翻译或改写）；
- 成功后原文填充到输入框并置光标末尾；标题窗提示「OCR中，速度会稍慢... / OCR成功 / OCR失败」。

## 4. 发送链路

- 发送时附加**对话专用系统提示词**（令回复为无标签无格式的纯文本），与翻译提示词相互独立；API 参数取主链路三键（base_url / model / api_key）；
- 请求在后台线程执行；回复（完整文本）经队列回主线程后显示在**流式正文窗**（复用悬浮窗正文显示出口）；
- 标题窗状态：「对话发送中... / 发送失败 / 发送成功」；
- **落库**：成功后用户消息与回复**成对**写入 `renpy_overlay_cache/chat.db`（降级时只显示不入库）。

## 5. 存储（chat_store.py）

```sql
CREATE TABLE IF NOT EXISTS chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_msg TEXT NOT NULL,
    ai_msg TEXT NOT NULL
)
```

- `insert(user_msg, ai_msg)` 成对写入，返回自增 id；`entries()` 按 id 升序；`delete(record_id)` 按 id 删一条；
- 与其余各库同一套模式：`open_store` 打开失败返回 None、`_degrade` 短路、**仅 Qt 主线程读写**（详见 [history-and-storage.md](history-and-storage.md)）。

## 6. 对话历史窗口（chat_history_window.py）

- 两列表格（用户消息 / AI 回复，外观如两列 Excel），按写入顺序浏览 `chat.db`；
- 查找：部分文字匹配、不区分大小写（两列都参与），上一个/下一个循环跳转到匹配行并整行高亮；左键点击选中；Ctrl+C 复制选中格；
- 删除选中行同步删除数据库记录并**原位回选**（选中行号自动回退到相邻行）。

## 交叉链接

- OCR 时序与 vision 客户端：[screenshot.md](screenshot.md)
- 主链路 API 配置：[config.md](config.md)
- 存储公共模式：[history-and-storage.md](history-and-storage.md)
