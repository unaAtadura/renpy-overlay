# 历史记录与数据库

本项目共 5 个 SQLite 数据库，全部位于**游戏目录**（跳过注入模式为用户所选目录）下的 `renpy_overlay_cache/`：

| 库文件 | 表 | 主键 | 写入方 | 历史 |
| --- | --- | --- | --- | --- |
| `translations.db` | `translations(hash, original, translated)` | 复合 `(hash, original)` | 单击/自动翻译成功、预构建 | `translation_history_window.py` |
| `screenshot.db` | `game_screenshot(id, ts, ocr_text, img_original, img_thumbnail)` | 自增 `id` | 截图翻译/遥控自动化成功 | `screenshot/history_window.py` |
| `chat.db` | `chat(id, user_msg, ai_msg)` | 自增 `id` | AI 对话成功 | `screenshot/chat_history_window.py` |
| `game_song.db` | `music_master(song_id, artist, song_name, game_scene, remark)` | 自增 `song_id` | 听歌识曲成功 | `song_recognition/history_window.py` |
| `asr.db` | `asr(asr_id, audio, text, duration)` | 自增 `asr_id` | 语音识别成功（**实时识别不入库**） | `video/asr_history_window.py` |

各库的详细 DDL 见对应功能文档：[translation.md](translation.md)、[screenshot.md](screenshot.md)、[ai-chat.md](ai-chat.md)、[song-recognition.md](song-recognition.md)、[asr.md](asr.md)。本文讲公共模式与各历史窗口交互。

## 1. 公共模式（五个 store 完全一致）

- **打开**（各 store 的 `open_store(game_dir)`）：`game_dir/renpy_overlay_cache/` 不存在则 `mkdir(parents=True)` → `sqlite3.connect` → `CREATE TABLE IF NOT EXISTS`；任何失败（未提供 game_dir / 目录不可建 / 库不可写）记日志返回 `None`——调用方降级为"只显示不入库"，功能流程不受影响。唯一例外是 translations.db 额外执行旧版单主键结构自动迁移并设 `busy_timeout = 5000`；
- **降级短路**：实例内 `_broken` 标志，首次 sqlite3.Error 后置位并记 ERROR，后续操作直接短路返回（None / False / 空列表），避免反复报错刷屏；
- **线程约束**：**只在 Qt 主线程读写**。后台线程（翻译请求 / 图像处理 / 录音识别）只做网络与计算，结果经队列回主线程后统一落库。例外：预构建的后台写连接与主线程连接并发访问同一 translations.db，由进程级写锁 `_WRITE_LOCK`（put/delete 持锁）+ `busy_timeout` 串行化（见 [translation.md](translation.md) §3）；
- **宿主装配**：五库在 `stream/host_core.py` 一次打开（`_translation_store` / `_screenshot_store` / `_song_store` / `_chat_store` / `_asr_store`），内存缓存容量 `translation_cache_size_kb * 1024` 字节；
- **跳过注入模式**：`picker.resolve_cache_dir` 三规则解析目录（目录名即为 `renpy_overlay_cache` → 直接用；其下已包含 → 用该子目录；否则自动创建），既有数据库一律不覆盖（`CREATE TABLE IF NOT EXISTS` 语义）。

`screenshot.db` 的 `ts` 为 13 位毫秒时间戳；`asr.db` 的 `entries()` 不拉取 audio 大字段（按需单条 `audio(asr_id)` 读取）。

## 2. 翻译历史窗口

文件：`translation_history_window.py`。细节见 [translation.md](translation.md) §5。要点：分桶逐行展开的两列表格；删除仅移除一组 `(哈希, 原文)`；译文列双击 / F2 行内编辑回车写库；支持 `(哈希, 原文)` 定位入参（入口条「定位翻译位置」打开并定位最近缓存命中，随后清空内存缓存——改译后下一次命中即取新译文，无需重启）。

## 3. 截图历史窗口

文件：`screenshot/history_window.py`（814 行，本包最大窗口）。布局与交互：

- **左上**：年/月/日下拉（年范围取库内首末记录、日智能平闰年）与首末记录日期标签；
- **左中**：缩略图条带——滚轮上滚看早期 / 下滚看后期、按住左键拖动浏览；中垂线扫到的缩略图停留 1 秒自动选中，左键点击跳过延迟直接选中；
- **左下**：选中记录的译文；**右侧**：原图；
- **删除**：按 id 删库；
- **导出**：日期行右侧「导出/全部导出」——把选中/全部记录的原图**原始字节**写入 `renpy_overlay_cache/screenshot/`（文件名 `<id>.<ext>`，扩展名按 BLOB 魔数判定，不重编码不缩放；重名一律跳过绝不覆盖，前台静默仅记日志）；
- `viewer_window.py`：原图全尺寸查看弹窗（无边框置顶、拖拽移动、单击关闭）。

## 4. 对话历史窗口

文件：`screenshot/chat_history_window.py`。两列表格（用户消息 / AI 回复）；查找两列都参与、不区分大小写、循环跳转整行高亮；Ctrl+C 复制选中格；删除同步删库并原位回选。

## 5. 识曲历史窗口

文件：`song_recognition/history_window.py`。四列表格（曲名/艺术家/游戏场景/备注）；查找与翻译/语音识别历史同口径；游戏场景与备注可编辑、回车经 `update_meta` 写库；删除同步删库。

## 6. 语音识别历史窗口

文件：`video/asr_history_window.py`。查找行 + 共用音量条（每次打开重置为静音 0）+ 三列表格（播放/停止按钮、时长倒计时、语音文本）；同时只播一条；逐条删除原位回选；Ctrl+C 复制语音文本。细节见 [asr.md](asr.md) §3。

## 7. 数据生命周期小结

```
游戏目录/renpy_overlay_cache/
├─ translations.db     ← 单击/自动翻译、预构建（增量 UPSERT，永不重建清空）
├─ screenshot.db       ← 截图翻译 / 遥控自动化（原图+缩略图 BLOB；实时可导出原图目录 screenshot/）
├─ chat.db             ← AI 对话成对记录
├─ game_song.db        ← 听歌识曲成功结果（scene/remark 可后编辑）
├─ asr.db              ← 非实时语音识别（音频 BLOB + 文本 + 时长）
├─ video/              ← 录制产物 yyyyMMdd-hhmmss.mp4（失败回退 .mkv）
└─ unrpa/              ← 预构建反编译/解包产物（归档专属目录）
```

历史数据只会增量追加与单条删除/覆盖：即使内存缓存因 FIFO 淘汰或进程重启而丢失，数据库中的记录仍保留。

## 交叉链接

- 写入时机与命中逻辑：[translation.md](translation.md)
- 查找交互的统一口径（部分匹配、不区分大小写、整行高亮、循环跳转）：各历史窗口实现各自维护，行为一致
- 缓存目录解析（跳过注入）：[injection.md](injection.md) §6
