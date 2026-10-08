"""telemetry 测试：recorder（INSERT DB）+ aggregator（SQL stats/activity）。

DB 化后：recorder 写 telemetry 表（不再分 jsonl 文件）；aggregator 用 SQL WHERE/GROUP BY。
"""
from datetime import datetime, timedelta, timezone


def _use_tmp_telemetry(tmp_path, monkeypatch):
    """重置 _shared 与 recorder._conn 到独立 tmp DB（隔离每个 telemetry 测试）。"""
    import app.db as dbmod
    import app.telemetry.recorder as tel_recorder
    db = dbmod.get_db(tmp_path / "test.db")
    dbmod.init_schema(db)
    monkeypatch.setattr(dbmod, "_shared", db, raising=False)
    # recorder 独立连接（2026-08-25）：不再随 _shared 走，须一并指到本测试库
    monkeypatch.setattr(tel_recorder, "_conn", db, raising=False)
    return db


def _now():
    return datetime.now(timezone.utc).isoformat()


def _seed(db, rows):
    """seed 多条打点到 DB（rows 字段对齐旧 jsonl 格式，含 id/type/level）。"""
    from app.repos import telemetry_repo
    for r in rows:
        telemetry_repo.insert(
            db,
            ts=r.get("ts", _now()), level=r.get("level", "request"),
            caller=r.get("caller", ""), endpoint=r.get("endpoint", ""),
            obj_id=r.get("id", ""), obj_type=r.get("type", ""),
            user=r.get("user", ""), operator=r.get("operator", ""),
            params=r.get("params", ""), result=r.get("result", ""),
        )
    db.commit()


# ---------- recorder：INSERT DB ----------

def test_record_object_inserts_to_db(tmp_path, monkeypatch):
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    from app.telemetry.recorder import record, flush
    record("/md", "F@1", "Feature", user="sa", caller="skill", level="object")
    assert flush()  # 异步写线程落库（v3 队列化）
    rows = db.execute("SELECT * FROM telemetry WHERE level='object'").fetchall()
    assert len(rows) == 1
    assert rows[0]["obj_id"] == "F@1" and rows[0]["caller"] == "skill"


def test_record_request_inserts_to_db(tmp_path, monkeypatch):
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    from app.telemetry.recorder import record, flush
    record("/api/v1/objects/F@1/md", user="fe", caller="web", level="request")
    assert flush()
    rows = db.execute("SELECT * FROM telemetry WHERE level='request'").fetchall()
    assert len(rows) == 1


def test_record_swallows_failure(tmp_path, monkeypatch):
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    # monkeypatch insert 抛：入队不抛 + 写线程丢批不抛不重试
    from app.repos import telemetry_repo
    def _boom(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(telemetry_repo, "insert", _boom)
    from app.telemetry.recorder import record, flush
    record("/md", "x", "y", user="u", caller="c", level="object")  # 入队不抛
    assert flush()  # 写线程丢批完成（pending 归零）
    assert db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 0  # 整批丢弃


# ---------- 打点瘦身（方案B，2026-08-26）：中间件/MCP 不再记 request 级 ----------

def test_middleware_no_longer_records_requests(tmp_path, monkeypatch):
    """普通 /api 请求不再产生 request 级打点（轮询/浏览噪音根治）。"""
    _use_tmp_telemetry(tmp_path, monkeypatch)
    from app.users.store import add_user
    add_user({"username": "admin", "key": "gap_admin", "can_frontend": True, "is_admin": True})
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        r = c.get("/api/v1/stats", headers={"X-API-Key": "gap_admin"})
        assert r.status_code == 200
    from app.telemetry.recorder import flush
    assert flush()
    n = _shared_row_count()
    assert n == 0  # 无 request 级行


def _shared_row_count() -> int:
    import app.db as dbmod
    return dbmod.get_shared_db().execute(
        "SELECT COUNT(*) FROM telemetry").fetchone()[0]


def test_purge_historical_request_rows_once(tmp_path, monkeypatch):
    """v8 一次性清理：历史 request 行删除（object/tool 保留），此后不再删。"""
    import app.db as dbmod
    from app.repos import telemetry_repo
    from datetime import datetime, timezone
    db = dbmod.get_db(tmp_path / "t.db")
    dbmod.init_schema(db)
    ts = datetime.now(timezone.utc).isoformat()
    for lv in ("request", "object", "tool"):
        telemetry_repo.insert(db, ts=ts, level=lv, caller="web" if lv == "request" else "mcp",
                              endpoint="/x", obj_id="", obj_type="", user="u", operator="")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 3
    # 模拟存量库：首次 init_schema 在空表上已消耗掉一次性标记 → 重置后再触发
    db.execute("DELETE FROM meta WHERE key='telemetry_request_purged'")
    db.commit()
    dbmod.init_schema(db)  # 触发一次性清理
    assert db.execute("SELECT COUNT(*) FROM telemetry WHERE level='request'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 2  # object/tool 保留
    # 清理后新写入的 request 行（fs/import 审计）不再被删
    telemetry_repo.insert(db, ts=ts, level="request", caller="web",
                          endpoint="/fs/upload", obj_id="", obj_type="", user="u", operator="")
    db.commit()
    dbmod.init_schema(db)  # 幂等重跑
    assert db.execute("SELECT COUNT(*) FROM telemetry WHERE level='request'").fetchone()[0] == 1


# ---------- aggregator ----------

def test_aggregate_stats_call_level(tmp_path, monkeypatch):
    """调用级口径（2026-09-04 重定）：level=tool + 7 端点；一次调用=1 次。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        # 2 次调用（tool 行）
        {"ts": _now(), "user": "sk1", "operator": "EMP001", "caller": "skill",
         "endpoint": "/md", "level": "tool", "params": '{"ids": ["F@1", "F@2"], "version": null}',
         "result": '{"ok": 2, "error": 0}'},
        {"ts": _now(), "user": "sk1", "operator": "EMP001", "caller": "skill",
         "endpoint": "/md", "level": "tool"},
        {"ts": _now(), "user": "sk2", "operator": "EMP002", "caller": "mcp",
         "endpoint": "mcp:search_md", "level": "tool"},
        # 对象级行不计入调用级统计
        {"ts": _now(), "user": "sk1", "operator": "EMP001", "caller": "skill",
         "endpoint": "/md", "id": "F@1", "type": "Feature", "level": "object"},
        {"ts": _now(), "user": "sk1", "operator": "EMP001", "caller": "skill",
         "endpoint": "/md", "id": "F@2", "type": "Feature", "level": "object"},
        # web/request 不计
        {"ts": _now(), "user": "fe", "caller": "web", "endpoint": "/md", "level": "tool"},
        {"ts": _now(), "user": "fe", "caller": "web", "endpoint": "/api/v1/stats", "level": "request"},
    ])
    from app.telemetry.aggregator import aggregate_stats
    r = aggregate_stats(days=30)
    assert r["total"] == 3  # 2 次 /md + 1 次 mcp:search_md
    assert r["by_endpoint"]["/md"] == 2
    assert r["by_endpoint"]["mcp:search_md"] == 1
    assert "by_type" not in r and "top_ids" not in r  # 对象级口径不再返回
    assert r["by_user"] == {"sk1": 2, "sk2": 1}
    assert r["by_operator"] == {"EMP001": 2, "EMP002": 1}
    # top_users：工号优先展示，EMP001 排第一
    assert r["top_users"][0] == {"user": "EMP001", "count": 2}
    assert r["top_users"][1] == {"user": "EMP002", "count": 1}
    # timeline：近 30 天>48h → 按天粒度
    assert all(item["granularity"] == "day" for item in r["timeline"])


def test_search_files_calls_appear_in_aggregate_and_call_details(
        tmp_path, monkeypatch):
    """REST /files 与 MCP search_files 都属于公开调用面，聚合和明细不得漏掉。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        {"ts": _now(), "user": "rest-user", "caller": "skill",
         "endpoint": "/files", "level": "tool"},
        {"ts": _now(), "user": "mcp-user", "caller": "mcp",
         "endpoint": "mcp:search_files", "level": "tool"},
    ])
    from app.telemetry.aggregator import (
        aggregate_stats, list_skill_usage, list_usage_table)

    stats = aggregate_stats(days=30)
    assert stats["total"] == 2
    assert stats["by_endpoint"] == {"/files": 1, "mcp:search_files": 1}

    stream = list_skill_usage(limit=10, scope="call")
    assert {event["endpoint"] for event in stream["events"]} == {
        "/files", "mcp:search_files"}
    table = list_usage_table(
        scope="call", endpoints=("/files", "mcp:search_files"))
    assert table["total"] == 2
    assert {row["endpoint"] for row in table["rows"]} == {
        "/files", "mcp:search_files"}


def test_aggregate_stats_hour_granularity_short_window(tmp_path, monkeypatch):
    """时间窗 ≤2 天 → 按小时粒度。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    t1 = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
    _seed(db, [
        {"ts": t1, "user": "sk", "caller": "skill", "endpoint": "/md", "level": "tool"},
    ])
    from app.telemetry.aggregator import aggregate_stats
    r = aggregate_stats(days=1)  # 近 1 天 → ≤48h → hour
    assert all(item["granularity"] == "hour" for item in r["timeline"])
    assert all(":00" in item["date"] for item in r["timeline"])


def test_aggregate_stats_timeline_in_utc8(tmp_path, monkeypatch):
    """timeline 桶标签按 UTC+8 展示（ts 存 UTC；2026-09-07 修复：此前按 UTC
    分桶比北京时间差 8h，与底表前端本地化口径不一致）。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        # UTC 20:30 = 北京 09-02 04:30 → 小时桶 "09-02 04:00"（旧 UTC 桶错为 "09-01 20:00"）
        {"ts": "2026-09-01T20:30:00+00:00", "user": "sk", "caller": "skill",
         "endpoint": "/md", "level": "tool"},
        # UTC 08-31 20:00 = 北京 09-01 04:00 → 天桶 "09-01"（旧 UTC 桶错为 "08-31"）
        {"ts": "2026-08-31T20:00:00+00:00", "user": "sk", "caller": "skill",
         "endpoint": "/md", "level": "tool"},
    ])
    from app.telemetry.aggregator import aggregate_stats
    # 窗口 ≤2 天（08-31~09-01，纯日期归一为当天起止）→ 小时粒度
    r = aggregate_stats(days=365, start="2026-08-31", end="2026-09-01")
    assert r["timeline"] == [{"date": "09-01 04:00", "count": 1, "granularity": "hour"},
                             {"date": "09-02 04:00", "count": 1, "granularity": "hour"}]
    # 窗口 >2 天 → 天粒度
    r2 = aggregate_stats(days=365, start="2026-08-01", end="2026-09-02")
    assert [b["date"] for b in r2["timeline"]] == ["09-01", "09-02"]
    assert all(b["granularity"] == "day" for b in r2["timeline"])


def test_aggregate_stats_days_filter(tmp_path, monkeypatch):
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    new = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    _seed(db, [
        {"ts": old, "user": "sk", "operator": "", "caller": "skill",
         "endpoint": "/md", "level": "tool"},
        {"ts": new, "user": "sk", "operator": "", "caller": "skill",
         "endpoint": "/md", "level": "tool"},
    ])
    from app.telemetry.aggregator import aggregate_stats
    r = aggregate_stats(days=10)
    assert r["total"] == 1
    assert r["by_endpoint"]["/md"] == 1


def test_aggregate_activity_scans_requests_only(tmp_path, monkeypatch):
    """activity 只扫 level=request；object 级不进 activity。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        {"ts": _now(), "user": "fe", "caller": "web", "endpoint": "/api/v1/objects/F@1/md", "level": "request"},
        {"ts": _now(), "user": "fe", "caller": "web", "endpoint": "/api/v1/stats", "level": "request"},
        {"ts": _now(), "user": "other", "caller": "web", "endpoint": "/api/v1/objects/F@2/md", "level": "request"},
        {"ts": _now(), "user": "fe", "caller": "web", "endpoint": "/md", "id": "F@1", "type": "Feature", "level": "object"},  # object 不计
    ])
    from app.telemetry.aggregator import aggregate_activity
    r = aggregate_activity("fe", days=30)
    assert len(r) == 2  # fe 的两条 request
    assert all(item["endpoint"] for item in r)


# ---------- list_skill_usage：SKILL 取用明细增量流 ----------

def test_list_skill_usage_filters_to_skill_objects_only(tmp_path, monkeypatch):
    """只返回 skill+object+/md,/domains；web、request 级、其它 endpoint 全排除。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        {"ts": _now(), "user": "sk", "operator": "E1", "caller": "skill", "endpoint": "/md", "id": "F@1", "type": "Feature", "level": "object"},
        {"ts": _now(), "user": "sk", "operator": "E1", "caller": "skill", "endpoint": "/domains", "id": "BD@x", "type": "BusinessDomain", "level": "object"},
        {"ts": _now(), "user": "fe", "caller": "web", "endpoint": "/md", "id": "F@1", "type": "Feature", "level": "object"},           # web 排除
        {"ts": _now(), "user": "sk", "caller": "skill", "endpoint": "/api/v1/stats", "level": "request"},                                 # request 排除
        {"ts": _now(), "user": "sk", "caller": "skill", "endpoint": "/fs/upload", "id": "p", "level": "object"},                          # 其它 endpoint 排除
    ])
    from app.telemetry.aggregator import list_skill_usage
    r = list_skill_usage(limit=100, scope="object")
    assert len(r["events"]) == 2
    assert {e["endpoint"] for e in r["events"]} == {"/md", "/domains"}
    assert all(e["user"] == "sk" for e in r["events"])
    assert all(e["caller"] == "skill" for e in r["events"])
    assert all(set(e) == {"ts", "caller", "endpoint", "obj_id", "obj_type", "user",
                          "operator", "session_id", "level"} for e in r["events"])  # 不泄露 rowid


def test_list_skill_usage_since_inclusive_and_cursor(tmp_path, monkeypatch):
    """since 纯 ISO 含边界（ts>=）；透传 next_since 增量推进且不重复。"""
    t0 = "2026-01-01T00:00:00+00:00"
    t1 = "2026-01-02T00:00:00+00:00"
    t2 = "2026-01-03T00:00:00+00:00"
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        {"ts": t0, "user": "sk", "caller": "skill", "endpoint": "/md", "id": "A", "type": "Feature", "level": "object"},
        {"ts": t1, "user": "sk", "caller": "skill", "endpoint": "/md", "id": "B", "type": "Feature", "level": "object"},
        {"ts": t2, "user": "sk", "caller": "skill", "endpoint": "/md", "id": "C", "type": "Feature", "level": "object"},
    ])
    from app.telemetry.aggregator import list_skill_usage
    # since=t1 含边界 → B、C；next_since 落在 C
    r = list_skill_usage(since=t1, limit=100, scope="object")
    assert [e["obj_id"] for e in r["events"]] == ["B", "C"]
    assert r["next_since"].split("|", 1)[0] == t2
    assert r["has_more"] is False
    # limit=1 → 只 B，has_more=True，next_since 落在 B
    r2 = list_skill_usage(since=t1, limit=1, scope="object")
    assert [e["obj_id"] for e in r2["events"]] == ["B"]
    assert r2["has_more"] is True
    assert r2["next_since"].split("|", 1)[0] == t1
    # 透传 next_since 推进 → 只剩 C，不再带 B（不重复）
    r3 = list_skill_usage(since=r2["next_since"], limit=100, scope="object")
    assert [e["obj_id"] for e in r3["events"]] == ["C"]


def test_list_skill_usage_empty_since_bootstrap(tmp_path, monkeypatch):
    """since 留空=全量起点；无匹配时 next_since 回填 since。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        {"ts": "2026-01-01T00:00:00+00:00", "user": "sk", "caller": "skill", "endpoint": "/md", "id": "A", "type": "Feature", "level": "object"},
    ])
    from app.telemetry.aggregator import list_skill_usage
    r = list_skill_usage(limit=100, scope="object")
    assert len(r["events"]) == 1
    # 未来时间起点 → 空，next_since 回填传入的 since
    future = "2099-01-01T00:00:00+00:00"
    r2 = list_skill_usage(since=future, limit=100)
    assert r2["events"] == []
    assert r2["next_since"] == future
    assert r2["has_more"] is False


def test_list_skill_usage_tie_ts_advances_without_dup(tmp_path, monkeypatch):
    """同 ts 多行：靠 rowid 推进，不卡死、不重复，has_more 正确。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    t = "2026-01-01T00:00:00+00:00"
    _seed(db, [{"ts": t, "user": "sk", "caller": "skill", "endpoint": "/md",
                "id": f"O{i}", "type": "Feature", "level": "object"} for i in range(5)])
    from app.telemetry.aggregator import list_skill_usage
    seen, cursor, rounds = [], "", 0
    while rounds < 10:
        r = list_skill_usage(since=cursor, limit=2, scope="object")
        seen += [e["obj_id"] for e in r["events"]]
        cursor = r["next_since"]
        rounds += 1
        if not r["has_more"]:
            break
    assert seen == ["O0", "O1", "O2", "O3", "O4"]  # 无重复，全部推进
    assert rounds == 3  # 2+2+1


# ---------- 时间窗（2026-09-03，外部系统对接：start/end 优先于 days）----------

def _win_seed(db):
    _seed(db, [
        # 调用级行（stats 新口径）
        {"ts": "2026-08-31T10:00:00+00:00", "user": "sk", "operator": "", "caller": "skill",
         "endpoint": "/md", "level": "tool", "params": '{"ids": ["AUG31"], "version": null}',
         "result": '{"ok": 1, "error": 0}'},
        {"ts": "2026-09-01T08:00:00+00:00", "user": "sk", "operator": "", "caller": "skill",
         "endpoint": "/md", "level": "tool"},
        {"ts": "2026-09-01T20:00:00+00:00", "user": "sk", "operator": "", "caller": "mcp",
         "endpoint": "mcp:get_md", "level": "tool"},
        {"ts": "2026-09-02T09:00:00+00:00", "user": "sk", "operator": "", "caller": "skill",
         "endpoint": "/md", "level": "tool"},
        # 对象级行（scope=object 用）
        {"ts": "2026-08-31T10:00:00+00:00", "user": "sk", "operator": "", "caller": "skill",
         "endpoint": "/md", "id": "AUG31", "type": "Feature", "level": "object"},
        {"ts": "2026-09-01T08:00:00+00:00", "user": "sk", "operator": "", "caller": "skill",
         "endpoint": "/md", "id": "SEP1_AM", "type": "Feature", "level": "object"},
        {"ts": "2026-09-01T20:00:00+00:00", "user": "sk", "operator": "", "caller": "mcp",
         "endpoint": "mcp:get_md", "id": "SEP1_PM", "type": "MMLCommand", "level": "object"},
        {"ts": "2026-09-02T09:00:00+00:00", "user": "sk", "operator": "", "caller": "skill",
         "endpoint": "/md", "id": "SEP2", "type": "Feature", "level": "object"},
    ])


def test_stats_time_window(tmp_path, monkeypatch):
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _win_seed(db)
    from app.telemetry.aggregator import aggregate_stats
    # 纯日期窗口：2026-09-01 当天（含 mcp 行）= 2；days 被忽略（否则全 4 条也含 8/31）
    r = aggregate_stats(days=365, start="2026-09-01", end="2026-09-01")
    assert r["by_endpoint"] == {"/md": 1, "mcp:get_md": 1}
    # 只给起点：09-01 起 3 条
    assert aggregate_stats(start="2026-09-01")["total"] == 3
    # 只给终点：09-01 前 1 条
    assert aggregate_stats(end="2026-08-31")["total"] == 1


def test_skill_usage_time_window_and_paging(tmp_path, monkeypatch):
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _win_seed(db)
    from app.telemetry.aggregator import list_skill_usage
    # 窗口首轮：09-01~09-02 共 3 条；limit=2 翻页，end 上界全程生效
    r = list_skill_usage(limit=2, start="2026-09-01", end="2026-09-02", scope="object")
    assert [e["obj_id"] for e in r["events"]] == ["SEP1_AM", "SEP1_PM"]
    assert r["has_more"] is True
    r2 = list_skill_usage(since=r["next_since"], limit=2,
                          start="2026-09-01", end="2026-09-02", scope="object")
    assert [e["obj_id"] for e in r2["events"]] == ["SEP2"]
    assert r2["has_more"] is False
    # 无窗口 + 全量起点 = 4 条
    assert list_skill_usage(limit=10, scope="object")["next_since"] != ""


def test_skill_usage_scopes_call_object_all(tmp_path, monkeypatch):
    """三口径（2026-09-04 重定）：call=调用级（默认，REST 调用行+MCP 工具行）；
    object=对象级细粒度；all=全含。每行显式 caller。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        {"ts": "2026-09-01T08:00:00+00:00", "user": "sk", "caller": "skill",
         "endpoint": "/md", "level": "tool", "params": '{"ids": ["F@1", "F@2"], "version": null}',
         "result": '{"ok": 2, "error": 0}'},
        {"ts": "2026-09-01T09:00:00+00:00", "user": "sk", "operator": "E1", "caller": "mcp",
         "endpoint": "mcp:search_objects", "level": "tool", "params": '{"q": "AMF"}',
         "result": '{"total": 12, "returned": 12}'},
        {"ts": "2026-09-01T08:00:01+00:00", "user": "sk", "caller": "skill",
         "endpoint": "/md", "id": "F@1", "type": "Feature", "level": "object"},
        {"ts": "2026-09-01T08:00:02+00:00", "user": "sk", "caller": "skill",
         "endpoint": "/md", "id": "F@2", "type": "Feature", "level": "object"},
        {"ts": "2026-09-01T10:00:00+00:00", "user": "fe", "caller": "web",
         "endpoint": "/md", "id": "F@1", "type": "Feature", "level": "object"},
        {"ts": "2026-09-01T11:00:00+00:00", "user": "sk", "caller": "skill",
         "endpoint": "/fs/upload", "level": "request"},
    ])
    from app.telemetry.aggregator import list_skill_usage
    call = list_skill_usage(limit=10)
    assert [e["endpoint"] for e in call["events"]] == ["/md", "mcp:search_objects"]
    assert call["events"][0]["caller"] == "skill"
    assert call["events"][0]["params"] == {"ids": ["F@1", "F@2"], "version": None}
    assert call["events"][0]["result"] == {"ok": 2, "error": 0}
    assert call["events"][1]["caller"] == "mcp" and call["events"][1]["operator"] == "E1"
    obj = list_skill_usage(limit=10, scope="object")
    assert [e["obj_id"] for e in obj["events"]] == ["F@1", "F@2"]
    assert "params" not in obj["events"][0] and obj["events"][0]["caller"] == "skill"
    assert len(list_skill_usage(limit=10, scope="all")["events"]) == 4


def test_usage_table_filters_and_paging(tmp_path, monkeypatch):
    """运维页底表表格：时间倒序 + 端点/账号筛选 + 分页 total。"""
    db = _use_tmp_telemetry(tmp_path, monkeypatch)
    _seed(db, [
        {"ts": "2026-09-01T08:00:00+00:00", "user": "a1", "operator": "E1", "caller": "skill",
         "endpoint": "/md", "level": "tool", "params": "{}", "result": '{"ok": 1}'},
        {"ts": "2026-09-02T08:00:00+00:00", "user": "a2", "operator": "E2", "caller": "mcp",
         "endpoint": "mcp:get_md", "level": "tool"},
        {"ts": "2026-09-03T08:00:00+00:00", "user": "a1", "operator": "", "caller": "skill",
         "endpoint": "/md", "id": "F@1", "type": "Feature", "level": "object"},
    ])
    from app.telemetry.aggregator import list_usage_table
    t = list_usage_table()
    assert [r["endpoint"] for r in t["rows"]] == ["mcp:get_md", "/md"] and t["total"] == 2
    t2 = list_usage_table(endpoints=("mcp:get_md",))
    assert t2["total"] == 1 and t2["rows"][0]["endpoint"] == "mcp:get_md"
    t3 = list_usage_table(user_like="E2")
    assert t3["total"] == 1 and t3["rows"][0]["user"] == "a2"
    t4 = list_usage_table(scope="object", page=1, size=1)
    assert t4["total"] == 1 and len(t4["rows"]) == 1
    t5 = list_usage_table(start="2026-09-02", end="2026-09-02")
    assert t5["total"] == 1 and t5["rows"][0]["endpoint"] == "mcp:get_md"
