"""截图数据库的离线验证：建库、插入、时间戳范围、升序列表与降级。"""

from __future__ import annotations

import random

from renpy_overlay.screenshot.remote_automation import region_means
from renpy_overlay.screenshot.store import open_store


def _insert(store, ts: int, text: str = "译文") -> int:
    record_id = store.insert(
        ts=ts,
        ocr_text=text,
        img_original=b"original-" + str(ts).encode(),
        img_thumbnail=b"thumb-" + str(ts).encode(),
    )
    assert record_id is not None
    return record_id


def _sig_phase(base: int, k: int) -> tuple[int, ...]:
    """同粗筛互不命中的签名族：6 格周期三正三负相位（区均值 = base±1.25）。

    不同 k 的两签名多数格差 40（超阈）→ 零超阈判据下互不命中；区均值
    偏差 ≤2（预筛阈值 9 内）→ 全部通过 SQL 预筛，适合验证截断与淘汰。
    """
    return tuple(base + (20 if (i + k) % 6 < 3 else -20) for i in range(512))


def _set_updated_ts(store, sig: tuple[int, ...], ts: int) -> None:
    """白盒：直接改写指定签名的 updated_ts（控制淘汰顺序用）。"""
    store._conn.execute(
        "UPDATE screenshot_signature_cache SET updated_ts = ? WHERE signature = ?",
        (int(ts), bytes(sig)),
    )
    store._conn.commit()


def test_insert_and_query_roundtrip(tmp_path):
    store = open_store(str(tmp_path))
    first = _insert(store, 1000, "第一条")
    second = _insert(store, 2000, "第二条")
    assert first == 1 and second == 2  # 自增 id
    assert store.count() == 2
    assert store.first_last_ts() == (1000, 2000)
    assert [ts for _id, ts in store.entries()] == [1000, 2000]  # 按 ts 升序
    text, original = store.record(first)
    assert text == "第一条"
    assert original == b"original-1000"
    assert store.thumbnail(first) == b"thumb-1000"
    store.close()


def test_missing_record_returns_none(tmp_path):
    store = open_store(str(tmp_path))
    assert store.record(999) is None
    assert store.thumbnail(999) is None
    store.close()


def test_empty_database_returns_none_and_empty(tmp_path):
    store = open_store(str(tmp_path))
    assert store.first_last_ts() is None
    assert store.entries() == []
    assert store.count() == 0
    store.close()


def test_empty_game_dir_degrades_to_none():
    assert open_store("") is None  # 未提供游戏目录：不入库（与 translation_store 一致）


def test_database_persists_across_connections(tmp_path):
    store = open_store(str(tmp_path))
    _insert(store, 1234567890123, "持久化")
    store.close()
    reopened = open_store(str(tmp_path))
    assert reopened.count() == 1
    text, _original = reopened.record(1)
    assert text == "持久化"
    reopened.close()


def test_delete_removes_only_target_record(tmp_path):
    store = open_store(str(tmp_path))
    _insert(store, 1000, "第一条")
    second = _insert(store, 2000, "第二条")
    third = _insert(store, 3000, "第三条")
    assert store.delete(second) is True
    assert store.count() == 2
    assert store.record(second) is None  # 被删记录不可再读
    assert [ts for _id, ts in store.entries()] == [1000, 3000]  # 其余记录保留
    assert store.record(third)[0] == "第三条"
    store.close()


def test_delete_missing_id_returns_false(tmp_path):
    store = open_store(str(tmp_path))
    _insert(store, 1000, "唯一一条")
    assert store.delete(999) is False  # id 不存在：未命中
    assert store.count() == 1
    store.close()


def test_delete_persists_after_reopen(tmp_path):
    store = open_store(str(tmp_path))
    _insert(store, 1000, "保留")
    victim = _insert(store, 2000, "将被删除")
    store.close()
    reopened = open_store(str(tmp_path))
    assert reopened.delete(victim) is True
    reopened.close()
    final = open_store(str(tmp_path))
    assert final.count() == 1
    assert final.record(victim) is None
    final.close()


def test_schema_matches_specification(tmp_path):
    import sqlite3

    store = open_store(str(tmp_path))
    store.close()
    connection = sqlite3.connect(tmp_path / "renpy_overlay_cache" / "screenshot.db")
    columns = {
        row[1]: row[2] for row in connection.execute("PRAGMA table_info(game_screenshot)")
    }
    connection.close()
    assert columns == {
        "id": "INTEGER",
        "ts": "BIGINT",
        "ocr_text": "TEXT",
        "img_original": "BLOB",
        "img_thumbnail": "BLOB",
    }


# ---- 签名缓存（遥控器自动化持久化两级命中） ---------------------------------------


def test_signature_cache_schema_and_autoincrement(tmp_path):
    """新表列集合固定；AUTOINCREMENT 保证 rowid 不复用（取模采样均匀性前提）。"""
    import sqlite3

    store = open_store(str(tmp_path))
    sig = _sig_phase(100, 0)
    assert store.put_signature(bytes(sig), region_means(sig), "甲", 100, 50, None) is True
    sig_b = _sig_phase(140, 1)
    assert store.put_signature(bytes(sig_b), region_means(sig_b), "乙", None, None, None) is True
    assert store.signature_cache_count() == 2
    connection = sqlite3.connect(tmp_path / "renpy_overlay_cache" / "screenshot.db")
    columns = {
        row[1]: row[2]
        for row in connection.execute("PRAGMA table_info(screenshot_signature_cache)")
    }
    sequence = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'screenshot_signature_cache'"
    ).fetchone()
    connection.close()
    assert columns == {
        "id": "INTEGER",
        "signature": "BLOB",
        "translated": "TEXT",
        "region_width": "INTEGER",
        "region_height": "INTEGER",
        **{f"region_mean_{i}": "INTEGER" for i in range(8)},
        "screenshot_id": "INTEGER",
        "created_ts": "BIGINT",
        "updated_ts": "BIGINT",
    }
    assert sequence is not None  # AUTOINCREMENT 已登记（rowid 不复用）
    store.close()


def test_signature_put_and_lookup_roundtrip(tmp_path):
    store = open_store(str(tmp_path))
    sig = _sig_phase(100, 0)
    assert store.put_signature(bytes(sig), region_means(sig), "缓存译文", 300, 200, 7) is True
    assert store.lookup_signature(bytes(sig), region_means(sig)) == "缓存译文"
    other = _sig_phase(100, 1)  # 同粗筛但零超阈判据下互不命中
    assert store.lookup_signature(bytes(other), region_means(other)) is None
    store.close()


def test_signature_put_dedup_overwrites_instead_of_insert(tmp_path):
    store = open_store(str(tmp_path))
    sig = _sig_phase(100, 0)
    assert store.put_signature(bytes(sig), region_means(sig), "旧译文", None, None, None) is True
    assert store.put_signature(bytes(sig), region_means(sig), "新译文", None, None, None) is True
    assert store.signature_cache_count() == 1  # 相似条目覆盖不新增
    assert store.lookup_signature(bytes(sig), region_means(sig)) == "新译文"
    store.close()


def test_signature_lookup_matches_within_cell_threshold(tmp_path):
    """零超阈命中：全格差恰 8 的变体与原签名互为命中（预筛阈值 9 内不误排斥）。"""
    store = open_store(str(tmp_path))
    sig = _sig_phase(100, 0)
    variant = tuple(v + 8 for v in sig)
    assert store.put_signature(bytes(variant), region_means(variant), "变体译文", None, None, None) is True
    assert store.lookup_signature(bytes(sig), region_means(sig)) == "变体译文"
    store.close()


def test_signature_blank_and_empty_translated_rejected(tmp_path):
    store = open_store(str(tmp_path))
    blank = tuple([128] * 512)  # 恒色：极差 0 低于信息量阈值
    assert store.put_signature(bytes(blank), region_means(blank), "黑场", None, None, None) is False
    sig = _sig_phase(100, 0)
    assert store.put_signature(bytes(sig), region_means(sig), "   ", None, None, None) is False
    assert store.signature_cache_count() == 0
    store.close()


def test_signature_lookup_refreshes_updated_ts(tmp_path):
    store = open_store(str(tmp_path))
    sig = _sig_phase(100, 0)
    assert store.put_signature(bytes(sig), region_means(sig), "译文", None, None, None) is True
    _set_updated_ts(store, sig, 1000)
    assert store.lookup_signature(bytes(sig), region_means(sig)) == "译文"
    row = store._conn.execute(
        "SELECT updated_ts FROM screenshot_signature_cache WHERE signature = ?",
        (bytes(sig),),
    ).fetchone()
    assert row is not None and int(row[0]) > 1000  # LRU 活性已刷新
    store.close()


def test_signature_lookup_persists_after_reopen(tmp_path):
    store = open_store(str(tmp_path))
    sig = _sig_phase(100, 2)
    assert store.put_signature(bytes(sig), region_means(sig), "跨会话", None, None, None) is True
    store.close()
    reopened = open_store(str(tmp_path))
    assert reopened.lookup_signature(bytes(sig), region_means(sig)) == "跨会话"
    reopened.close()


def test_signature_query_limit_truncates_safely(tmp_path, monkeypatch):
    """细比较候选上限：最旧候选被截断后不命中（漏命中=安全方向），新候选仍命中。"""
    from renpy_overlay.screenshot import store as store_module

    store = open_store(str(tmp_path))
    sigs = {k: _sig_phase(100, k) for k in range(5)}
    for k, sig in sigs.items():
        assert store.put_signature(bytes(sig), region_means(sig), f"v{k}", None, None, None) is True
        _set_updated_ts(store, sig, 1000 + k)  # 递增：k4 最新、k0 最旧
    monkeypatch.setattr(store_module, "SIGNATURE_CACHE_QUERY_LIMIT", 3)
    assert store.lookup_signature(bytes(sigs[4]), region_means(sigs[4])) == "v4"  # 前三候选可命中
    assert store.lookup_signature(bytes(sigs[0]), region_means(sigs[0])) is None  # 最旧被截断
    store.close()


def test_signature_prune_cold_random_watermark(tmp_path):
    """水位淘汰：超上限后仅冷端（最旧一半）采样删除，热端条目全部存活。"""
    store = open_store(str(tmp_path))
    store._rng = random.Random(42)  # 固定随机源保证确定性
    # 基准步长 2：任意两签名互不零超阈命中（同相位 base 差 = 2|d|，|d|≥5 时
    # ≥10 全超阈；|d|<5 时相位差≠0，异段格差 ≥ 40-2|d| ≥ 32 → 超阈占比 ≥4/6）；
    # 格值范围 [1, 249] 不溢出
    sigs = {k: _sig_phase(21 + 2 * k, k) for k in range(105)}
    for k in range(105):
        assert store.put_signature(
            bytes(sigs[k]), region_means(sigs[k]), f"v{k}", None, None, None, capacity=100
        ) is True
        _set_updated_ts(store, sigs[k], 1000 + k)  # 递增：淘汰顺序稳定可控
    # k=100 写入后 count=101 触发淘汰：删 6 条至水位 95；采样仅在
    # updated_ts 最旧一半（rn≤50 ⇔ ts 1000..1049 ⇔ k0..k49）内进行
    assert store.signature_cache_count() == 99  # 95 + k101..k104 四条
    survived_cold = store._conn.execute(
        "SELECT COUNT(*) FROM screenshot_signature_cache WHERE updated_ts <= 1049"
    ).fetchone()[0]
    survived_hot = store._conn.execute(
        "SELECT COUNT(*) FROM screenshot_signature_cache WHERE updated_ts >= 1050"
    ).fetchone()[0]
    assert int(survived_cold) == 44  # 冷端 50 条被采样删除 6 条
    assert int(survived_hot) == 55  # 热端（k50..k104）结构性零淘汰
    assert (
        store.lookup_signature(bytes(sigs[104]), region_means(sigs[104])) == "v104"
    )  # 最新条目免疫淘汰且可命中
    store.close()


def test_signature_store_degraded_short_circuits(tmp_path):
    """降级短路：_broken 后 lookup/put/prune/count 全部安全返回空值。"""
    store = open_store(str(tmp_path))
    store._broken = True  # 白盒置位（真实路径由 sqlite 错误触发）
    sig = _sig_phase(100, 0)
    assert store.lookup_signature(bytes(sig), region_means(sig)) is None
    assert store.put_signature(bytes(sig), region_means(sig), "译文", None, None, None) is False
    assert store.prune_cold_random(100) == 0
    assert store.signature_cache_count() == -1
    store.close()
