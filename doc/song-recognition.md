# 听歌识曲

对应源码：`src/renpy_overlay/song_recognition/`（recorder / recognizer / store / history_window）+ 宿主侧 `stream/song_flow.py`。测试：`test_song_recognition.py`、`test_song_recognition_store.py`、`test_song_recognition_history.py`。

## 1. 功能形态

快捷菜单「听歌识曲」——**纯后台行为**（无新窗口/弹窗）：录制 `recording_duration`（默认 8 秒，最小 3）系统音频交 Shazam 识别。标题窗提示录制倒计时（建议关闭游戏音效提升识别率）/ 录制失败 / 识曲中 / 识曲成功 / 识曲失败；成功时正文窗显示「歌曲名 / 艺术家」，失败提示"多次失败可能是这首歌没有被收录"。

互斥与打断：与翻译/截图翻译/AI 对话共用"同时仅一个 API 请求"互斥（在途时点击被忽略且不更新提示）；**双击**（悬浮窗锁定交互）可打断录制与识曲（识别结果作废）。

## 2. 系统音频录制（recorder.py）

三重回退方案（可用性标志模块导入时经 `find_spec` 确定，重库延迟到录制函数内部导入）：

| 方案 | 库 | 采样率 | 特点 |
| --- | --- | --- | --- |
| 1. soundcard 环回 | soundcard + soundfile + numpy | 44100，双声道 | 整段阻塞录制无法中途打断（录制完成后才检查 stop）；检测到蓝牙耳机（名称含"蓝牙耳机"/"Bluetooth"）直接跳过 |
| 2. pyaudiowpatch WASAPI 环回 | pyaudiowpatch | **跟随设备默认采样率** | Windows 原生环回，支持蓝牙耳机；逐块读取（`_CHUNK = 1024` 帧）可中断 |
| 3. pyaudio 立体声混音 | pyaudio | 44100 | 依赖用户在 Windows 声音控制面板手动启用的录音设备；逐块可中断 |

- `record_system_audio(duration, stop_event)`：按 1→2→3 顺序尝试；**已被中止（stop_event 置位）后不再回退下一方案**；全部失败返回 None，日志给出各方案错误与排查提示（蓝牙耳机不支持环回 / 未安装录制库 / 未启用立体声混音）；
- 产物：系统临时目录 `renpy_song_*.wav`（PCM_16 / paInt16），**调用方负责删除**。

## 3. Shazam 识别（recognizer.py）

- `recognize(audio_path)`：shazamio 延迟导入（未安装时 `SHAZAMIO_AVAILABLE = False`，调用方降级提示不影响启动）；内部 `asyncio.run(Shazam().recognize(...))` 执行异步识别；
- 响应无 `track` 字段 → 视为未识别（采样不足或曲目未收录），返回 None；
- `extract_track_info`：从 track 字典提取 (title, subtitle)，字段缺失回退「未知」；
- `format_success_body / format_fail_body`：正文展示纯函数（成功两行"歌曲名/艺术家"；失败固定文案）。

## 4. 宿主流程（stream/song_flow.py）

后台线程：`record_system_audio` → `recognize` → 结果（含临时 wav 路径）入队 → 主线程消费：成功则正文显示 + 写 `game_song.db`（`game_scene`/`remark` 留空）+ 删除临时 wav；失败显示失败文案 + 删除临时 wav；录制期间标题窗每 `RECORD_TICK_INTERVAL_MS = 1000` 刷新倒计时。

## 5. 存储（store.py）

```sql
CREATE TABLE IF NOT EXISTS music_master (
    song_id INTEGER PRIMARY KEY AUTOINCREMENT,
    artist TEXT NOT NULL,   -- 艺术家名
    song_name TEXT NOT NULL,-- 歌曲名
    game_scene TEXT,        -- 游戏场景：战斗/城镇/过场/菜单等
    remark TEXT             -- 备注：版本、变奏、来源说明
)
```

识曲成功时 `game_scene`/`remark` 写 NULL；`update_meta(song_id, game_scene, remark)` 供历史窗口的第三、四列编辑回写。降级模式与主线程约束同全局口径。

## 6. 识曲历史窗口（history_window.py）

- 四列表格：曲名 / 艺术家 / 游戏场景 / 备注，按写入顺序（song_id 升序）滚动浏览；
- 上部「查找」输入框与上一个/下一个/删除按钮：部分文字匹配（与翻译/语音识别历史同口径）、不区分大小写并整行高亮；Ctrl+C 复制选中内容；
- 删除选中行同步删除数据库记录；游戏场景与备注两列可编辑，回车经 `update_meta` 写入数据库。

## 交叉链接

- 持续版环回录音（视频录制复用）：[video.md](video.md)
- game_song.db 汇总：[history-and-storage.md](history-and-storage.md)
- 互斥模型：[architecture.md](architecture.md) §4
