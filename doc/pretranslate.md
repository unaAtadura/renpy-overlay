# 预构建翻译缓存（离线批量预翻译）

不依赖游戏运行时的注入捕获，直接扫描游戏目录脚本，把对白 / 分支选项 / tl 文本交给配置的 API 逐条翻译并写入 `translations.db`，运行时自动翻译的两级缓存即可直接命中。对应源码：`src/renpy_overlay/pretranslate/`（parser / cleaning / concurrency / runner / overlay_paths / formatting）、`prebuild_window.py`（三页签主窗口）、`external_tools.py` + `tools_runner.py`（.rpyc / .rpa 处理）。测试：`test_pretranslate_parser.py`、`test_pretranslate_cleaning.py`、`test_pretranslate_runner.py`、`test_prebuild_formatting.py`、`test_prebuild_window.py`、`test_concurrency.py`、`test_external_tools.py`、`test_tools_runner.py`、`test_overlay_paths.py`。

## 1. 脚本扫描与解析（pretranslate/parser.py）

三层能力，全部只依赖标准库（不依赖 renpy / PyQt6，可离线测试）：

1. **`scan_rpy_files(game_dir, tl_langs)`**：枚举 `game/` 下全部 `.rpy` 相对路径（升序稳定）。`.rpa` 归档内的 `.rpy` 以 `game/x.rpa!inner/path.rpy` 形式参与（分隔符 `RPA_SEPARATOR = "!"`）；`tl_langs` 非 None 时只保留指定语言的 `tl/<语言>/` 文件（归档内条目按路径中 `tl` 的下一段判定语言）；只列文件不读内容（毫秒级，供弹窗同步展示）；损坏归档记日志跳过不阻塞；
2. **`extract_blocks(text)`**：轻量**启发式行解析**，产出 `(kind, payload)` 块序列：
   - `("say", [文本])`：对白三形态（`e "文本"` 角色前缀 / `"文本"` 旁白 / `e "文本" with x` 修饰）。`_NON_SAY_PREFIX` 黑名单排除 show/jump/define/screen language 等约 50 类关键字开头行；python 块内整块跳过（缩进栈）；三引号跨行 say 逐行吸收直到字面量可闭合解析（防御上限 64 行后放弃）；
   - `("menu", [caption, ...])`：同一 menu 语句的选项组（缩进栈支持嵌套；caption = 行首字符串后紧跟 `:` 或 `if 条件:`）；选项体内嵌的非 caption say 行按对白收集（分支差分对白）；
   - `("tl_str", [文本])`：`translate <语言> strings:` 块内的 `new` 文本（`old` 是原文不收集）；translate label 块内未注释的 say 行按普通 say 收集（即 new）；
   - 已知局限：.rpyc 不解析；screen language 文本按钮不收集；单文件解析失败记日志跳过；
3. **`collect_texts(files, game_dir, on_file)`**：加载（普通文件直读 / rpa 条目解包）→ 解析 → 清洗去重 → 全局去重（同一原文只保留首个来源），产出 `SourceText(text, kind, source)` 列表；`on_file(done, total, discovered)` 每文件回调供 GUI 渐进显示。

**RPA-3.0 解包**（`_rpa_index` / `unpack_rpa_entry`）：首行 `RPA-3.0 <索引偏移hex> <异或键hex>` → seek 到索引段 → zlib 解压 → pickle 反序列化 `{内部路径: [(偏移, 长度), ...]}` → 偏移逐项异或还原；条目数据首字节 `\x78` 为未混淆 zlib 流，否则首字节 = 混淆前缀长度、跳过前缀后解压。

## 2. 缓存键清洗（pretranslate/cleaning.py）

预构建写入 `translations.db` 的 `original` 必须与运行时上报文本**逐字节一致**，否则预填条目永远无法命中：

- `clean_text(text)`：复刻 `agent._clean_text`（`{{`/`[[` 占位保护 → 去 `{...}` 标签 → 还原 → strip）；等价性由 `tests/test_pretranslate_cleaning.py` 用样本表钉住（agent.py 面向 py2/py3 不宜 import，故离线侧复刻）；
- `build_choice_key(items)`：复刻运行时 `_handle_choice` 口径——逐项 strip、剔除空项、经 `choice_translation_text` 编号拼接（与 [translation.md](translation.md) §2 的选项缓存键同一函数）。

## 3. 自适应并发（pretranslate/concurrency.py）

`AdaptiveLimiter`：AIMD 并发上限控制器（纯逻辑无线程代码，仅调度线程调用）：

- **加性增长**：连续成功 ≥ `increase_after`（默认 6）条，且近 16 条成功时延的**中位数** < `latency_guard_seconds`（默认 15s，服务端未过载信号）→ 并发 +1（封顶 `max_concurrency`，默认 8，上限硬限 64）；
- **乘性减少**：任何失败（普通翻译错误 / 端点不可达）把并发减半（下限 1）——失败通常意味着服务端过载或模型切换中；
- **熔断**：连续 `circuit_break_threshold`（默认 5）条端点不可达 → `broken = True`，批量任务收束本轮；
- 时延滚动窗口 `LATENCY_WINDOW = 16`（deque maxlen），空窗口视为未过载。

## 4. 批量任务（pretranslate/runner.py）

`BatchTask.run()` 由预构建主窗口在后台守护线程执行，参数经 `BatchOptions.from_config`（API 参数与运行时单击翻译**完全一致**：base_url / timeout / model / system_prompt / api_key / enable_thinking / reasoning_effort，非流式）。两阶段：

1. **统计**（无 API 调用）：`collect_texts` 解析清洗 → 逐条 `store.has` 判存 → 得到本轮待翻译清单（进度分母）；条目间响应 `stop_event`；源文件根 `source_dir` 可指向 unrpa 目录（三页签联动，见 §6）；
2. **翻译**：调度循环按 `limiter.current` 派发到 `ThreadPoolExecutor(max_workers=max_concurrency)`；`wait(..., timeout=0.2, FIRST_COMPLETED)` 收割；单条 `_translate_worker`：
   - 重试 `retries`（默认 2，上限 10）次仍失败计 KIND_FAILURE 继续；
   - **`EndpointUnavailable` 不做单条重试**（备用链路切换在运行时流程内，预构建重试无意义）→ 直接 KIND_UNREACHABLE 参与熔断计数；
   - 成功即 `store.put`（**单条单事务**，任意时刻终止不留残缺条目；写库经进程级写锁 `_WRITE_LOCK` 与前台翻译链路串行化）；
- **取消**：停止派发、等待在途请求自然收束（已完成译文照常入库），终态 `cancelled`；游戏退出自动停止；终态 `on_finished` 只发一次（completed / cancelled / circuit_break / error 收敛）；
- store 为任务私有连接（主线程创建、后台线程使用，跨线程由写锁串行化），`finally` 自行关闭。

## 5. 三页签主窗口（prebuild_window.py）

`PrebuildWindow(QMainWindow)`，尺寸 680×640，状态机 `select`（勾选）/ `running`（进度）；页签 `_PAGE_TITLES = (".rpy 翻译缓存", ".rpyc 反编译", ".rpa 解包")`；`_CLOSE_JOIN_TIMEOUT = 10.0` 关闭时等待后台任务收束。页 0 的扫描根可切到 unrpa（`overlay_paths.resolve_scan_root`：unrpa 目录存在且含目标后缀文件 → 用 unrpa，否则用 `game`）；游戏目录可改并自动记忆 `last_game_dir`；进度条附实时并发档位与已用/剩余时间估算（`formatting.py` 纯函数产出文案）。

**与互斥模型的关系**：预构建不受前台"同时仅一个 API 请求"互斥约束（其余场景后台翻译期间单击/自动翻译照常）；**实时语音识别开启期间除外**——双向互斥，在途时入口被忽略。「隐藏到后台」后再次点击入口唤回，弹窗内可取消。

## 6. .rpyc 反编译 / .rpa 解包（external_tools.py / tools_runner.py / overlay_paths.py）

- 外部工具随包分发于 `assets/plugin/`：`unrpyc.exe`（.rpyc 反编译）、`rpaextract.exe`（.rpa 解包，基于 .NET 10 运行时，运行机器需 .NET 10 Desktop Runtime）；`external_tools.run_tool` 统一子进程执行：超时 `TOOL_TIMEOUT_SECONDS = 600`、`CREATE_NO_WINDOW` 防控制台闪窗、超时/异常先优雅后强杀（宽限 `KILL_GRACE_SECONDS = 5.0`）；
- `tools_runner`：`DecompileTask`（目标 .rpy 已存在恒跳过）/ `ExtractTask`（**输出目录非空整档跳过 = 同归档幂等**）逐文件后台执行，进度/失败经回调上报；
- 路径规则（`overlay_paths.py` 纯函数）：unrpa 工作目录 = `<游戏根>/renpy_overlay_cache/unrpa`；反编译产物统一 `unrpa/<相对路径去后缀>.rpy`；解包产物入归档专属目录（`game/x.rpa → unrpa/x/`、`game/folder/y.rpa → unrpa/folder/y/`），不同归档同名条目互不覆盖；`_strip_game_prefix` 自动剥离产物自带的 `game/` 前缀。unrpa 内已反编译的 `.rpy` 会被页 0 优先扫描。

## 7. 外部工具来源

| 工具 | 用途 | 仓库 |
| --- | --- | --- |
| unrpyc | `.rpyc` 反编译 | https://github.com/CensoredUsername/unrpyc |
| rpaextract | `.rpa` 解包 | https://github.com/Kaskadee/rpaextract |

## 交叉链接

- 写入的表与运行时命中逻辑：[translation.md](translation.md)、[history-and-storage.md](history-and-storage.md)
- 并发参数配置键：[config.md](config.md)
