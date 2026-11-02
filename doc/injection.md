# 进程发现与注入链路

对应源码：`src/renpy_overlay/discovery.py`、`win32api.py`、`pe.py`、`shellcode.py`、`injector.py`、`picker.py`、`folder_dialog.py`、`cli.py`（主流程）。测试：`tests/test_pe.py`、`test_shellcode.py`、`test_win32api.py`、`test_skip_injection.py`。

## 1. 目标发现（discovery.py）

判定策略是"打分 + 可解释"：命中特征加分并把理由记进 `Candidate.reasons`，选择界面原样展示。分值常量：

| 判据 | 分值 | 说明 |
| --- | --- | --- |
| 进程名为 `renpy.exe`/`renpyw.exe`/`renpy` 或含 "renpy" | +4 | `SCORE_NAME_RENPY` |
| 进程名为 Python 解释器（`python.exe` 等） | +3 | `SCORE_NAME_PYTHON` |
| exe 同级/父级目录存在 `renpy`/`lib`/`game` | 每项 +3，封顶 +6 | `SCORE_DIR_HINT` / `SCORE_DIR_HINT_CAP` |
| 有 ≥640x360 的顶层窗口 | +2 | `BIG_WINDOW = (640, 360)` |
| 命令行含 renpy / .rpy | +2 | `SCORE_CMDLINE_RENPY` |
| 进程内加载了 Python 运行库 dll（深探测） | +5 | `SCORE_PYTHON_DLL` |

两类判据分离以控制开销：**廉价判据**（进程名 / 目录 / 窗口 / 命令行，psutil + `win32api.list_windows`）套在全部进程上；**强判据**（`OpenProcess` + 枚举模块找 `python*.dll`）只对"廉价分 ≥ `SCORE_DIR_HINT`(3) 或有大窗口"的少量进程执行（`_inspect_python_modules`，用 `QUERY_ACCESS` 权限）。

`enumerate_candidates` 永远排除本进程自身与工具自己的启动器（`_is_own_launcher`：`uv run renpy-overlay` 会在 venv 里启动 `renpy-overlay.exe` 重定向器，也加载 pythonXX.dll，会误排到前列）。结果按 `(-score, pid)` 排序。架构判定用 `arch_from_file` 直读本地 PE 文件头（比 `IsWow64Process` 准且不受调用方位数影响）。`--pid` 路径走 `find_candidate(pid)`，跳过打分但保留运行时探测。

## 2. Win32 封装（win32api.py）

纯 `ctypes`，无 C 编译器依赖。关键常量：

- 注入权限组合 `INJECTION_ACCESS = PROCESS_CREATE_THREAD | PROCESS_QUERY_INFORMATION | PROCESS_VM_OPERATION | PROCESS_VM_READ | PROCESS_VM_WRITE`；
- 只读探测组合 `QUERY_ACCESS = PROCESS_QUERY_INFORMATION | PROCESS_VM_READ`；
- 内存保护 `PAGE_READWRITE`（结果区）/ `PAGE_EXECUTE_READWRITE`（桩区）。

`RemoteProcess` 持有进程句柄并跟踪全部远程分配（`alloc/write/read/free/free_all`）、`modules()`（`EnumProcessModulesEx`）与 `create_remote_thread`。另提供窗口枚举、`GetWindowRect`、`move_window`（物理像素定位）、`double_click_time_ms`、DPI awareness 设置等，供悬浮窗复用。

## 3. Python 运行库定位（pe.py）

Ren'Py 进程内本就加载了完整 CPython 运行库，因此不需要引导 DLL，直接远程调用其解释器。**为什么不读本机 Python 的导出偏移**：本机与游戏内置版本通常不一致，函数在导出表的偏移甚至存在性都会变；只解析目标进程内存里的真实 PE 数据即可完全避开版本耦合。

候选筛选 `rank_python_modules`（按"越像"排序，排序只影响尝试顺序，正确性由导出验证裁决）：

| 优先级 | 模式 | 说明 |
| --- | --- | --- |
| 0 | `^(?:lib)?python(?:2\d|3\d{1,2}|[23]\.\d{1,2})\.dll$` | 标准 CPython 命名（含 Ren'Py 8.x 新构建改名的 `libpython3.9.dll`） |
| 1 | `^python3\.dll$` | 稳定 ABI 转发层，仅作兜底（本身不实现解释器，导出验证会自然排除） |
| 2 | `^(?:lib)?renpython\.dll$` | Ren'Py 8.x 扩展集合 dll（一万多导出但通常不含 Python C API，实测 Eternum 0.9.5 即如此），最后兜底 |
| 3 | 名字含 "python" 的其它 dll | 个别自编译构建可能把解释器静态链进去 |

`RemotePE` 用 **RVA 语义 reader**（`reader(rva, size) -> bytes`）解耦读取来源：注入场景是 `lambda rva, size: process.read(base + rva, size)`，单测场景是 `ctypes.string_at`（`self_reader`）——同一套解析逻辑可被 `test_pe.py` 离线验证（拿本进程 `kernel32.dll` 当样本、`GetProcAddress` 交叉验证）。

导出查找 `export_rva`：`AddressOfNames` 数组按字典序有序，直接**二分查找**，每次探测只读一个 128 字节字符串，整个查找只有 log2(N) 次远程读取。找到 RVA 后检查两点：序号越界返回 None；RVA 落在导出目录区间内说明是**转发导出**（转发到别的 dll），不可当本模块内地址，返回 None——`python3.dll` 转发层与 `librenpython.dll` 就是这样被自然排除的。`PeHeader.machine`（`IMAGE_FILE_MACHINE_*`）同时给出目标架构（x86/x64/arm/arm64，仅前两者受支持）。防御上限 `_MAX_EXPORT_NAMES = 200_000`。

## 4. 引导桩机器码（shellcode.py）

桩的语义等价 C 代码：

```c
PyGILState_STATE gil = PyGILState_Ensure();
int rc = PyRun_SimpleString(source);
PyGILState_Release(gil);
return rc;   // 线程退出码 = PyRun_SimpleString 返回值
```

- `PyGILState_Ensure` 是必须的：远程线程是全新原生线程，没有 Python 线程状态，直接 `PyRun_SimpleString` 会崩；
- 退出码是刻意设计的诊断信号：`0` = 引导代码执行完毕；`0xFFFFFFFF` = 引导代码抛异常（回溯写进游戏自身 log.txt）。

一次性分配同一块 RWX 内存，布局为 `机器码 | 三个函数指针数据槽 | 引导源码(UTF-8 + \0)`：

| 架构 | 机器码长 | 数据槽偏移 | 源码偏移 | 寻址方式 |
| --- | --- | --- | --- | --- |
| x64 | `0x37` | `0x40`（ensure/run/release 各 8 字节） | `0x60` | `call qword ptr [rip+disp32]`，RIP 相对位移只取决于桩内偏移，与实际落点无关 |
| x86 | `0x32` | `0x34`（各 4 字节） | `0x40` | `call dword ptr [abs32]` 绝对地址，须在拿到远程基址后回填（`build_stub(remote_base=...)`） |

x64 用 `sub rsp,0x28` 预留影子空间并对齐 16 字节；gil 暂存在影子空间内（`[rsp+0x20]`）不额外动栈指针。工具端自身不加载任何 32 位代码，全程只操作目标进程内存，所以 64 位工具端可同时注入 32 位（Ren'Py 7.x / Python 2.7）与 64 位（Ren'Py 8.x / Python 3.9+）游戏。`test_shellcode.py` 对两种桩做字节级布局与 RIP 相对位移断言。

## 5. 注入编排（injector.py）

一次注入的完整链路（`Injector.run_bootstrap`）：

1. `OpenProcess`（`INJECTION_ACCESS`）；
2. `_locate_python`：枚举模块 → 候选排序 → 逐个解析 PE、验证 `REQUIRED_EXPORTS = ("PyGILState_Ensure", "PyRun_SimpleString", "PyGILState_Release")` 三导出齐全；失败时把完整模块清单写入日志 DEBUG（命名差异排查第一手证据）；
3. 组装引导源码：`_BOOTSTRAP_TEMPLATE` 内嵌 **base64 的 agent 源码 + JSON 配置 + 结果区地址**（`__RESULT_ADDR__` 占位符分配后替换；模板内 `\x00` 是刻意双重转义）。注意模板用 `eval(compile(...))` 而非 `exec(...)`——Python 2 里 `exec(x, g)` 会被解析成 exec 语句 + 元组表达式，运行时 TypeError，而 eval 模式在 2.7 与 3.x 行为一致；
4. `VirtualAllocEx` 分配两块内存：**结果区**（RW，`RESULT_SIZE = 8192`）与**桩区**（RWX）。桩先以 `remote_base=0` 探长度，再以真实地址重建一次写入（x86 回填需要）；
5. `CreateRemoteThread` 执行桩 → `wait_for_thread`（`--timeout`，默认 `DEFAULT_TIMEOUT = 15.0` 秒）；
6. 读回结果区（读到 `\0` 截断、UTF-8 解码、JSON 解析为 `payload_result`）→ 未超时则释放两块内存。

**结果区的意义**：`PyRun_SimpleString` 只能回报成功/失败，结果区能带回 agent 的详细信息（Python 版本、实际装上的 Hook 层、异常类型）。写入由引导代码在 `start()` 返回后**同步**完成（`ctypes.memmove`），"线程结束 → 读取 → 释放"的顺序无 use-after-free 风险。**线程等待超时**（游戏可能卡在执行中）是唯一不释放内存的场合，日志明确提示；残留分配由 `Injector.close()` 记 WARNING 后统一释放。

除注入外还有两个动作模板 `_ACTION_TEMPLATE`：`unload`（调 `agent.shutdown()`）与 `status`（调 `agent.status()`）；代理未加载时报 `not_loaded`。`BootstrapOutcome.ok` 要求：线程正常结束、未超时、退出码 0、且 `payload_result.ok` 为真。重复注入前引导代码会先调旧模块的 `shutdown()`（`AGENT_MODULE_NAME = "renpy_overlay_agent"`）。

## 6. 目标选择与跳过注入（picker.py / folder_dialog.py）

- `choose_target(candidates, refresh, use_gui)`：优先 Tkinter 图形选择窗（`--no-gui` 或 Tcl/pywin32 异常时回退命令行菜单）；窗口带刷新按钮重新枚举；选中未检出 Python 运行库的进程时 CLI 层给 WARNING；
- **跳过注入**：选择窗的「跳过注入」按钮经 `folder_dialog.py`（IFileOpenDialog，COM/ctypes 直调）选定数据目录——选择期间选择窗保持打开，取消则退回选择窗；随后关闭选择窗、以 `SkipSelection` 返回。缓存目录解析 `resolve_cache_dir` 三规则：目录名即为 `renpy_overlay_cache` → 直接用；其下已包含 → 用该子目录；否则自动创建（`game_dir` 取解析结果父目录）。既有数据库一律不覆盖；
- 跳过注入模式运行 `_run_skip_injection_mode`：不启动 IPC 与进程监控，悬浮窗 `skip_injection=True`，关闭即退出；单击/自动翻译入口在 `translate_flow` 内被显式拦截。

## 7. 命令行参数（cli.build_parser）

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `--pid PID` | — | 直接指定目标进程，跳过选择界面 |
| `--list` | — | 只列出候选进程后退出（配 `--all` 连低分候选一起列） |
| `--no-gui` | — | 命令行菜单替代图形选择窗 |
| `--unload` / `--status` | — | 对目标进程执行卸载 / 状态查询后退出 |
| `--no-overlay` | — | 不显示悬浮窗，对话打印到控制台 |
| `--dock POS` | `top-center` | `top-center/top-left/top-right/bottom-center/bottom-left/bottom-right` |
| `--port` | `0` | IPC 监听端口（系统分配空闲端口） |
| `--timeout` | `15.0` | 等待远程线程结束秒数 |
| `--idle-exit` | `20.0` | 游戏进程消失后自动退出的秒数 |
| `--poll-hz` | `7.0` | 游戏内兜底轮询频率（透传 agent 配置） |
| `--log-level / --log-dir / --log-file` | `INFO / logs / 自动命名` | 日志配置 |

启动顺序（`main`）：`force_utf8_stdio` → 解析参数 → `setup_logging` → `enable_dpi_awareness`（悬浮窗物理像素坐标的前提）→ Windows 平台检查 → 目标选择 → `Session`。

## 交叉链接

- 引导进游戏后发生什么：[dialogue-capture.md](dialogue-capture.md)
- 通道建立与报文：[ipc.md](ipc.md)
- 排障（权限 / 杀软 / 找不到运行库）：见根目录 [README.md](../README.md) 排障表
