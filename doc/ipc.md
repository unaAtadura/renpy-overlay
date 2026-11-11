# IPC 通信协议（NDJSON over TCP）

对应源码：工具端 `src/renpy_overlay/ipc.py`；游戏端发送侧在 `payload/agent.py`（见 [dialogue-capture.md](dialogue-capture.md) §6）。测试：`tests/test_ipc.py`（NDJSON 编解码、token 握手、非法报文容错、心跳、断开回调）。

## 1. 拓扑决策

**工具端做服务端、游戏代理做客户端**：

- 重连逻辑在游戏进程里，让它做客户端最简单——重连失败只影响它自己；
- 工具端只要 `listen` 一次就能稳定接收，中途重启工具端也不会留下悬挂的半连接；
- 绑定 `127.0.0.1` 的随机端口（`--port` 默认 0，由系统分配），端口号经注入配置传给代理，避免端口冲突。

**安全**：仅监听回环地址；连接后的**首个报文**必须是携带正确 `token` 的 `hello`，否则断开。token 由 `cli.Session` 每次运行用 `secrets.token_hex(16)` 随机生成，随注入配置下发，不对局域网/公网暴露任何端口。

## 2. 编解码

- `encode(message)`：`json.dumps(ensure_ascii=True)` → ASCII bytes + `\n`（`ensure_ascii` 保证任何终端/代码页都能安全传输）；
- `decode(line)`：非法 JSON / 空行 / 顶层非对象一律返回 `None` 不抛异常（协议层容错，DEBUG 记前 200 字符）；
- 单行上限 `MAX_LINE = 64 * 1024`；缓冲区超过 `MAX_LINE * 8` 判定异常增长，主动断开。

## 3. 服务端实现（DialogueLink）

- `start()`：`AF_INET + SOCK_STREAM`，`SO_REUSEADDR`，`listen(4)`，accept 超时 0.5s 轮询停止标志；返回实际端口；
- `_accept_loop`：接受连接后为每个客户端起一个 `_read_loop` 线程（daemon，命名 `ipc-client-<port>`）；
- `_read_loop`：4096 字节接收 → 拼缓冲 → 按行切分 → 解码 → 未认证时先走 `_authenticate`，认证后 `_dispatch`；
- `_authenticate`：首包 `t != "hello"` 或 `token` 不匹配 → 记日志并断开；匹配则填充 `PeerState`（pid、py、hooks 列表），触发 `on_connect` 回调；
- `_dispatch`：`t=hb` 时更新 `peer.stats`（DEBUG 记录）；其余消息交给 `on_message` 回调。回调异常被 `_safe_call` 捕获，**不终止读线程**；
- `_close_client`：关 socket、从客户端表移除、触发 `on_disconnect`（游戏端断开时 UI 显示「连接断开」，CLI 层提示代理可能已卸载或游戏正在退出）；
- `stop()`：置停止标志 → 关全部客户端 → 关服务端 → join 全部线程（超时 `JOIN_TIMEOUT = 2.0`）；
- 状态查询：`connected` 属性、`seconds_since_last_message()`（监控线程据此在 >20s 无消息时给 WARNING——游戏可能被暂停或卡住）。

## 4. 报文类型表（协议版本 proto = 1）

方向：游戏端 → 工具端（工具端不向游戏端发送业务报文）。

| `t` | 字段 | 时机 | 工具端处理（`cli.Session._on_message`） |
| --- | --- | --- | --- |
| `hello` | `token, proto, pid, py, hooks, cwd` | 建连后首个报文（同步直发，不经队列） | 认证后 `on_connect`；标题显示「已连接」 |
| `say` | `who, what, src, ts, pid` | 捕获到新对话 | `overlay.push_say`；未就绪则暂存 `_pending_say`（只留最新）；`--no-overlay` 时打印 |
| `choice` | `items[], who, what, src, ts` | 菜单出现 | `overlay.push_choice`（正文整段替换为「提问对白 + 编号选项列表」——`what` 为该菜单的紧邻提问，无则为空仅显示选项；标题显示预览） |
| `choice_pick` | `index, caption, src, ts` | 菜单关闭 | `overlay.push_choice_pick`（正文追加「→ 已选择：×××」） |
| `hooks` | `layers[], stats` | Hook 安装完成（priority 插队） | 日志确认 Hook 层；`overlay.mark_injected()`（入口条并入标题窗） |
| `log` | `level, msg` | agent 内部日志 | `log_from_game` 写入同一 logger（`renpy_overlay.game`） |
| `hb` | `uptime, stats{captured, sent, dropped, queued}` | 每 2s | 更新 `peer.stats`（DEBUG） |
| `bye` | `stats` | 卸载完成（priority 插队） | 日志「游戏端代理已卸载」 |

未知 `t` 值 DEBUG 记录不报错（向前兼容）。

## 5. 竞态处理（Session 侧）

- **say 早于悬浮窗就绪**：悬浮窗在 config 加载后才创建，注入时正显示的台词是最常见早到者。此前这些 say 会被静默丢弃且游戏端去重已记发布、无法重发，导致该句永远不被翻译。现为暂存 `_pending_say`（只留最新一条），悬浮窗就绪后 `_flush_pending_say` 补推；
- **hooks 早于悬浮窗就绪**：`_hooks_confirmed` 记录确认状态，就绪后 `_flush_injected` 幂等补发 `mark_injected`（补发即消费，重复调用无副作用）。

## 交叉链接

- 报文的产生端：[dialogue-capture.md](dialogue-capture.md)
- 消息上屏后的渲染：[overlay-window.md](overlay-window.md)
