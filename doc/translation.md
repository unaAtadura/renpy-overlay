# 翻译链路与两级缓存

对应源码：`src/renpy_overlay/translator.py`（API 客户端）、`translation_cache.py`（内存缓存）、`translation_store.py`（SQLite 持久化）、`stream/translate_flow.py`（宿主链路）、`translation_history_window.py`（历史窗口）。测试：`test_translator.py`、`test_translator_stream.py`、`test_translation_cache.py`、`test_translation_store.py`、`test_translation_history.py`。

## 1. API 客户端（translator.py）

只依赖标准库 `urllib`，不含线程 / GUI 逻辑——请求由调用方在后台线程执行，便于离线单测（本地 HTTP 桩）。

- 默认常量：`DEFAULT_BASE_URL = "http://127.0.0.1:1234"`（LM Studio）、`DEFAULT_TIMEOUT = 20.0`（config 层另有更宽松的 `api_timeout` 默认 30.0）、`DEFAULT_REASONING_EFFORT = "none"`、内置游戏翻译系统提示词（"你是游戏对话翻译器……"）；
- `_api_url(base, path)`：拼接 `/v1/chat/completions`、`/v1/models`；基址已以 `/v1` 结尾时**不重复追加**（否则阿里云 compatible-mode 等网关拼出 `/v1/v1/...` 返回空体 404，实测）；
- `_chat_payload`（流式与非流式共用）：`messages = [system?] + [user]`、`temperature: 0.2`、`stream`；**思考模式三方方言**一并声明同一语义——`enable_thinking`（Qwen Cloud / DashScope 顶层字段）、`chat_template_kwargs.enable_thinking`（vLLM / SGLang）、`reasoning_effort`（LM Studio 等本地服务忽略前两者，实测需同时声明 `"none"` 才能真正关闭思考；关闭后单条翻译约 0.5s，开启思考 30s+）。配置 `reasoning_effort` 为空串则不发送该字段；
- `first_model()`：`model` 配置为空时 `GET /v1/models` 取第一个已加载模型 id；
- 错误分型：`EndpointUnavailable`（连接失败/超时——仅这类错误会触发备选链路降级语义）与 `TranslationError`（HTTP 错误、协议缺字段、空译文）；空文本抛 `ValueError`；
- `translate_text_stream()`：SSE 流式版。返回生成器——**首次迭代才真正发请求**（模型自动发现也在此时）；按行阻塞读取、每行尽快产出降低显示延迟；`data: [DONE]` 结束；空 delta / 非 JSON 行跳过；思考型模型的 reasoning 内容不在 `delta.content` 中，天然被跳过；迭代中途连接中断（URLError / socket.timeout / IncompleteRead 族）收敛为 `TranslationError`。

API 三链路（文本 / 视觉 / 非实时 ASR）共用上述基座；实时 ASR 走独立 WebSocket，见 [asr.md](asr.md)。

## 2. 内存缓存（translation_cache.py）

- **键**：`hash_original(text) = sha256(utf8)[:32]`（前 128 位、32 个十六进制字符）。跨进程跨运行稳定——不用内置 `hash()`（带随机化盐，重启即变）；与 SQLite 存储共用同一哈希函数，两级一一对应；
- **分桶**：`_buckets: 哈希 → [(原文, 译文), ...]`。同一哈希下多条不同原文的记录共存，查询时桶内逐一精确比对原文，绝不返回其他原文的译文；同哈希同原文 = 覆盖译文并**刷新淘汰顺序**；
- **容量计量与淘汰**：`_order: OrderedDict[(哈希, 原文) → 记录字节数]` 全局索引；总占用 = 全部记录的哈希+原文+译文 UTF-8 字节之和；超限按**记录**粒度从队首 FIFO 淘汰（不整桶淘汰）；单条自身超限拒绝写入（记 WARNING，不影响译文上屏）；
- 上限来自 `config.json`：宿主构造 `TranslationCache(max_bytes=app_config.translation_cache_size_kb * 1024)`（默认 2048KB；模块常量 `MAX_CACHE_BYTES = 256KB` 仅为缺省参数）；
- **线程约束**：只在 Qt 主线程读写；后台翻译线程把原文随结果回传主线程后再 `put`；
- `choice_translation_text(items)`：分支选项的翻译/缓存键——编号逐行拼接 `1. 选项一\n2. 选项二`（与正文编号列表一致；保留序号让译文与选项一一对应；回退重放与存档载入可命中缓存）。原居 stream_window，为让预构建包无 Qt 依赖复用而迁入，stream_window 保留 re-export。

## 3. SQLite 持久化（translation_store.py）

- 位置：**游戏目录** `renpy_overlay_cache/translations.db`（跳过注入模式则落所选目录）；
- 表结构（`_SCHEMA`）：

  ```sql
  CREATE TABLE IF NOT EXISTS translations (
      hash       TEXT NOT NULL,
      original   TEXT NOT NULL,
      translated TEXT NOT NULL,
      PRIMARY KEY (hash, original)
  )
  ```

  复合主键与内存分桶语义一致；**旧版 `hash` 单主键结构打开时自动迁移**（临时表重建 + `INSERT OR REPLACE` 搬运，保留全部历史数据，日志提示）；
- 写入：`INSERT ... ON CONFLICT(hash, original) DO UPDATE`（UPSERT，同一原文覆盖译文，同哈希不同原文互不覆盖）；单条单事务；
- **进程级写锁** `_WRITE_LOCK`：同一库存在两条写链路——前台翻译落库（Qt 主线程）与离线预构建落库（后台线程、独立连接、`check_same_thread=False`）；put/delete 持锁串行化（粒度单条，主线程恒立即获得）；读不加锁；`PRAGMA busy_timeout = 5000` 兜底跨进程写占用；
- **降级**：目录不可建 / 库不可写 → `open_store` 返回 `None`（调用方退化为仅内存缓存）；运行时读写失败 `_degrade` 置 `_broken` 短路（避免反复报错刷屏）；`has()` 在降级时返回 True（视为已存在，避免向不可写库发起无意义的预构建请求）；
- `entries()` 按 rowid 升序返回全部 `(hash, original, translated)`（历史窗口浏览顺序）；`delete(original)` 按复合主键精确删一条——同桶其它原文保留，桶空整个条目消失。

## 4. 宿主链路（stream/translate_flow.py）

### 单击翻译（`_start_translation(origin="manual")`）

入口条件链：跳过注入模式直接拦截 → `_translating/_recognizing/_chat_busy` 任一为真则忽略（"已有请求在途"）→ 取 `_last_say.what`（空则跳过）→ 置位、`_translation_seq += 1`、记录 `_last_translation_input`（自动翻译据此去重）→ 标题「翻译中...」→ daemon 线程执行 `_translate_worker`。

worker：SSE 迭代逐块投递 `("chunk", {seq, text})`；stop 置位即停喂块；异常收敛为 `{"ok": False}`；成功消息携带完整译文与**原文 input**。

`_handle_translation`（主线程）：seq 不匹配直接丢弃（世代校验）；失败标题「翻译失败」（部分流内容保留在正文区便于排查）；成功后 seq 一致且无有效增量、或样式 2 时整段补显，随后**同步写入两级缓存**（内存 put + store put，手动/自动一致）。

`_abort_translation`：世代号递增使在途结果作废 + stop 置位让网络等待尽快结束。双击锁定、自动翻译缓存命中等场景调用。

### 自动翻译（`_auto_translate_check`）

主线程 QTimer（`_auto_timer`，间隔 `max(200, auto_translate_interval * 1000)` ms）驱动；另有事件驱动路径（新 say 到达时 `immediate=True`，跳过间隔门）。条件链（跳过原因去重记日志）：

1. `_auto_translate_on`（运行时模式，初值取 config `auto_translate`）；
2. `_locked`（仅锁定状态）；
3. 非事件驱动时要求"已锁定超过一个完整轮询间隔"（`_lock_started_at`）；
4. 有新原文且 ≠ `_last_translation_input`（同一原文只翻一次）；
5. 识曲 / 对话不在途；
6. **两级缓存命中优先**：内存 `cache.get` → 数据库 `store.get`（命中回填内存缓存）→ 任一命中直接上屏（`_auto_use_cached`：记录 `_last_cache_hit_original` 供「定位翻译位置」入口使用、作废在途请求、标题「翻译完成」），不发起网络请求；
7. 均未命中且无在途请求 → `_start_translation(origin="auto")`。

解锁即停用轮询并中止在途（手动单击路径不受影响）。标题入口「切换翻译模式」只改 `_auto_translate_on` 内存状态。

## 5. 翻译历史窗口（translation_history_window.py）

- 两列表格（原文/译文），数据 = `store.entries()` 逐行展开（同一哈希下多条不同原文的记录逐行显示）；
- 查找：部分文字匹配、不区分大小写，上一个/下一个循环跳转并整行高亮；Ctrl+C 复制选中格；左键点击选中；
- 删除：仅移除选中的一组 `(哈希, 原文)` 记录（`store.delete`），同桶其它原文保留；
- **译文编辑**：译文列双击（或选中后 F2）进入行内编辑，回车经 `store.put` 写库；空译文不写入、恢复显示原译文；修改不立即上屏——配合「定位翻译位置」清空内存缓存（`cache.clear()`）后，下一次自动翻译命中即回源数据库取到新译文，无需重启工具；
- **定位入参**：窗口打开/刷新可携带 `(哈希, 原文)`，直接滚动并选中指定条目（入口条「定位翻译位置」用 `_last_cache_hit_original` 生成）。

## 6. 数据流小结

```
游戏原文（agent 捕获）
   │  单击（锁定态）            自动翻译 QTimer / 事件驱动
   ▼                              ▼
互斥检查 ──► 内存缓存 ──► SQLite ──► 都未命中 ──► SSE 流式 API（后台线程）
                │ 命中直接上屏                │ chunk 逐块入队
                ▼                             ▼
            正文窗打字机上屏 ◄──────── Qt 主线程消费（80ms）
                                              │ 成功
                                              ▼
                              两级缓存同时写入（内存 + UPSERT 落库）
```

注意：**单击翻译固定调用 API（不读任何缓存）**，成功后覆盖写入两级；只有自动翻译走"缓存命中优先"。

## 交叉链接

- 请求体的平台方言细节：[config.md](config.md)（`enable_thinking` / `reasoning_effort`）
- 数据库表结构汇总：[history-and-storage.md](history-and-storage.md)
- 离线预填同一张表：[pretranslate.md](pretranslate.md)
