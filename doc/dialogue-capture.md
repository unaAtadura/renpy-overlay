# 游戏内对话捕获（注入代理 agent.py）

对应源码：`src/renpy_overlay/payload/agent.py`（约 980 行，单文件）。测试：`tests/test_payload.py`。

## 1. 设计约束

四条约束决定了全部写法：

1. **不能影响游戏**：每个 Hook 回调整体包在 `try/except BaseException` 里，绝不把异常抛回游戏；网络 IO 全部放到独立守护线程，主循环里只做"读值 + 入队"；
2. **必须兼容 Python 2.7 / 3.x**：Ren'Py 7.x 内置 2.7、8.x 内置 3.9+，同一份源码跑两边——禁用 f-string / 注解 / nonlocal 等新语法（`test_payload.py` 静态断言），`_to_text` 统一处理 bytes/str/unicode；
3. **多层捕获 + 去重**：各版本对话实现差异大（ADV/NVL/气泡/多角色），同时挂官方回调与轮询兜底，谁先捕到都算，靠 `(who, what)` 去重；
4. **可完全卸载**：安装时记录反注册函数，`shutdown()` 逆序执行，还原 config 列表、被包装的原有回调、socket、线程，并移出 `sys.modules`。

全局状态集中放在单个 `_STATE` 字典（避免 `global` 语句，跨线程读写是"单键"级原子操作）。

## 2. Hook 层总表

任一层生效即可，全部包 `try/except`；按 `(who, what)` 去重（`_publish`，入队失败时回退去重标记允许轮询重发，避免一句因一次入队失败被永久吞掉）：

| 层 | Hook 点 | 类型 | 说明 |
| --- | --- | --- | --- |
| 主 | `config.all_character_callbacks` | 列表 append | `event == "begin"` 时 `kwargs["what"]` 是完整对话文本（`force=True`：即使与上一句相同也输出）；`event == "show"` 时按 `start/end` 切片上报 `{w}/{p}` 分段（`callback-segment`） |
| 主 | `config.say_arguments_callback` | 链式包装 | 单个函数（非列表），只借用第一个入参取当前发言角色名进 `who_hint`，原值记入 `say_arguments_prev`，卸载时还原（原不存在则删除属性） |
| 主 | `config.say_menu_text_filter` | 链式包装 | **「显示即达」层**：`Say.execute` 进正文显示之前被调用，比 `_history_list` 入账（do_done）早一个交互周期——旧引擎（7.3/8.0/8.1）上唯一能拿到显示瞬间当前句的公开钩子。双重判别：当前节点必须是 `Say`（排除 Menu 选项文本等过滤调用），且传入文本与当前节点 `what` 一致（预测路径会用未来节点调用，不一致即跳过，防止提前上报未显示台词） |
| 主 | `renpy.exports.menu` | 模块属性替换 | **分支选项唯一入口**：进入时 `_publish_choice`（force=True：menu 语句真执行了——首次 / 回退重放 / 存档载入都视为新事件），原函数返回后 `_publish_choice_pick` 上报玩家所选。原函数的异常原样抛回（那是游戏自己的流程） |
| 备 | `config.periodic_callbacks` | 列表 append | 引擎约 20Hz 调用，内部按 `poll_hz`（默认 7.0）限流后做两层扫描 + 选项屏幕兜底 + 注册看门狗 |
| 备 | `renpy.store._last_say_what` 直读 | 轮询扫描 | 引擎写入的当前句原文显示期间常驻（finally 只清 `_last_raw_what`），覆盖「注入时该句已在屏」的盲区（该句执行早于注入，过滤器不会再触发）；偏移 0，who 取配套 `_last_say_who` 经 store 解析为展示名 |
| 备 | `renpy.get_screen("say").scope` | 轮询扫描 | say 界面作用域的 `who/what` |
| 备 | `renpy.store._history_list[-1]` | 轮询扫描 | 对话历史兜底（旧引擎当前句要交互结束才入账，只作兜底源） |
| 备 | `renpy.game.context().current` | 轮询扫描 | 当前语句节点上的 `who/what` |
| 备 | `renpy.get_screen("choice").scope` | 轮询扫描 | choice screen 选项兜底（详见 §4） |
| 退出 | `config.python_exit_callbacks` | 列表 append | 游戏正常退出时自动执行 `shutdown()` 清理 |

**轮询两层扫描顺序**（`_periodic_callback`）：当前句源（say 界面 → store 直读 → 语句节点）优先，任一有值即停；仅当它们都无值时才轮到历史兜底源。这样旧值（典型为 history 的上一句）既不会占据优先位饿死当前句源，也不会在当前句已发布后翻旧账造成乒乓重复上报。

**版本实测说明**（`_install_now` 内注释）：分支选项的 `renpy.exports.menu` 替换在 Ren'Py 8.2.1.24030407（脚本版本 `(8, 2, 1)`，构建于 2025-07-06，Python 3.9.10 / x64，测试游戏 Sicae-Ep.7）验证。该版本中 menu 语句经 `Menu.execute` 调用 `renpy.exports.menu(choices, ...)`（唯一入口，返回值即所选 value）；`renpy` 命名空间**无** `display_menu`/`menu` 的 re-export，`renpy/display/menu.py` 也不存在——因此直接替换 `renpy.exports` 模块上的 `menu`（旧文档描述的 `renpy.display_menu` 包装与当前代码不符，以本节为准）。替换未生效或被绕过的场景（从选项节点存档载入）由 `_scan_choice_screen` 轮询兜底覆盖，但兜底拿不到玩家所选。

## 3. 文本清洗与说话人

- `_clean_text`：`{{`→`\x01`、`[[`→`\x02` 占位 → 正则 `\{[^{}]*\}` 去标签 → 还原占位 → strip。这条口径被离线预构建 `pretranslate/cleaning.py` 逐字节复刻（等价性由 `tests/test_pretranslate_cleaning.py` 钉住），保证预填缓存条目能命中运行时原文；
- `_resolve_who` 优先级：回调显式给出 > `say_arguments_callback` 缓存（`who_hint`）> say 界面 > 历史 > 当前语句；
- `_who_from_raw`：who 原始表达式是简单变量名时经 `renpy.store` 解析为角色展示名，与 history/过滤器源的去重键保持同一口径；解析失败（旁白 / 复杂表达式）返回空串，不误报旧角色。

## 4. 分支选项捕获

- `_publish_choice(items)`：解析 display_menu 的 items（`(caption, label, chosen)` 三元组取第一项；choice screen 传带 `caption` 属性的条目对象）→ 以选项文本组合作**指纹去重**（choice screen 轮询 20Hz 重复扫同一屏只报一次；主 hook 以 `force=True` 绕过指纹）→ 上报 `t=choice`（items、触发对话上下文取 `_scan_history()`、src、ts）；
- `_publish_choice_pick(result)`：把 `exports.menu` 返回值映射回选项序号与文本——8.x menu_actions 形态在三元组第三位、旧二组形态在第二位，两处都尝试匹配（caption 相等也算）；都匹配不上降级为仅上报返回值文本。上报 `t=choice_pick`；
- `_scan_choice_screen()`：每轮轮询检查 choice screen 在屏状态；**菜单关闭即清空指纹**，允许同一菜单下次出现时重新上报。覆盖两类场景：exports_menu 替换未生效、从停在选项节点的存档载入。

## 5. 注册路径与看门狗

`_install_hooks`：优先 `renpy.invoke_in_main_thread(_install_now)`（保证在主线程修改 config），并设 `install_deadline = now + 2.0` 秒；`_periodic_callback` 里的看门狗 `_ensure_installed` 在超时未执行时补一次直接注册。`invoke_in_main_thread` 不可用则直接注册。安装完成即上报 `t=hooks`（priority=True，`appendleft` 插队）并 `renpy.notify` 提示。

## 6. 上报通道与网络线程

- 配置 `DEFAULT_CONFIG`：`host=127.0.0.1`、`port`（由注入配置下发）、`token`、`poll_hz=7.0`、`max_queue=512`、`heartbeat=2.0`、`reconnect_max=5.0`、`notify=True`、`log_level=info`；
- `_emit(obj, priority=False)`：消息入 `collections.deque`；超 `max_queue` 丢最旧（`dropped` 计数），`priority` 消息 `appendleft` 插队（hooks / bye 必须抢先）；
- `_sender_loop`（守护线程，0.2s 节拍）：`_ensure_socket` 建连（connect 超时 2s）→ **`hello` 同步直发不经队列**（服务端要求它必须是首个报文，字段：token、proto=1、pid、py、hooks、cwd）→ `_flush()` 刷队列 → 每 `heartbeat` 秒发 `t=hb`（uptime + captured/sent/dropped/queued 统计）。连接失败按 0.5s 起步指数退避，封顶 `reconnect_max`（5s），成功后回 0.5s；
- 退出前尽力 flush 剩余队列（含 `t=bye`）再关 socket。

## 7. 卸载与自检

- `shutdown()`：置 stop → **逆序**执行 `remove_hooks`（列表回调移除、链式包装还原、模块属性还原）→ 发 `bye`（priority）并 flush → 关 socket → 移出 `sys.modules`；返回 `{unloaded, captured, sent, dropped, errors}`（errors 最多保留 20 条）；
- `status()`：外部（`--status`）可要求回传 `{layers, install_done, py, stats, errors}`。

## 交叉链接

- 怎么被送进游戏：[injection.md](injection.md)
- 报文格式与工具端接收：[ipc.md](ipc.md)
- 清洗口径的离线复刻：[pretranslate.md](pretranslate.md)
