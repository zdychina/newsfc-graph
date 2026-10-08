"""telemetry 表：append-only INSERT + 聚合查询（替代 jsonl 全表扫）。

level=object（取用统计：SKILL 旧接口 + MCP 工具）+ level=request（请求轨迹，
activity 统计）+ level=tool（MCP 工具调用级，含 search 类可观测，MCP 服务化
2026-08-24）。ts 存 ISO8601 UTC 字符串（字典序 = 时间序，cutoff 用字符串比较）。
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

# timeline 桶展示时区：内网部署统一北京时间（ts 存 UTC，分桶前转换——
# 2026-09-07 修复：此前按 UTC 直接 strftime，桶标签比北京时间差 8h，
# 与底表前端 new Date(ts) 本地化口径不一致）。
_TZ_DISPLAY = timezone(timedelta(hours=8))

# 取用统计口径：SKILL 旧两接口（历史行）+ MCP 对应工具（新行）——无缝衔接
_STATS_ENDPOINTS = ("/md", "/domains", "mcp:get_md", "mcp:get_domains")
_STATS_CALLERS = ("skill", "mcp")
# 底表口径（2026-09-04 用户重定）：
# - call（默认）：**调用级**——每次调用一行。REST /md、/domains 各 1 行
#   （level=tool，含 params/result）+ MCP 图谱工具每调用 1 行（level=tool）。
# - object：**对象级**细粒度——每取一个对象一行（get_md/get_domains/REST，
#   level=object），供单独导出；运维页统计热榜同此数据源。
# - all：两类全含。
# caller 恒为 skill/mcp（web 不在暴露面）；网页端浏览不打点。
# v12（2026-09-08 三工具重构）：新增 mcp:search_graph；legacy 三端点保留
# （历史数据与兼容期直调继续进统计/底表，§13.2）。
# 2026-09-09：搜索补 REST 通道（POST /search）。
_CALL_ENDPOINTS = ("/md", "/domains", "/search", "/files",
                   "mcp:get_md", "mcp:get_domains", "mcp:search_graph",
                   "mcp:search_files",
                   "mcp:search_objects", "mcp:search_md", "mcp:get_object")
_SCOPE_LEVELS = {"call": ("tool",), "object": ("object",),
                 "all": ("object", "tool")}
_SCOPE_ENDPOINTS = {"call": _CALL_ENDPOINTS, "object": _STATS_ENDPOINTS,
                    "all": _CALL_ENDPOINTS}


def insert(conn: sqlite3.Connection, *, ts: str, level: str, caller: str,
           endpoint: str, obj_id: str, obj_type: str, user: str, operator: str,
           session_id: str = "", params: str = "", result: str = "") -> None:
    conn.execute(
        "INSERT INTO telemetry(ts, level, caller, endpoint, obj_id, obj_type, user, "
        "operator, session_id, params, result) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (ts, level, caller, endpoint, obj_id, obj_type, user, operator,
         session_id, params, result),
    )


def _cutoff_iso(days: int):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat() if days > 0 else None


def _norm_bound(t: str, *, is_end: bool) -> str:
    """时间窗边界归一：纯日期（YYYY-MM-DD，len 10）→ 起点补 T00:00:00 /
    终点补 T23:59:59（ts 为完整 ISO8601，字典序比较需同量级——裸日期当终点
    会把当天的完整时间戳全部排除）。"""
    t = (t or "").strip()
    if len(t) == 10:
        return f"{t}T23:59:59" if is_end else f"{t}T00:00:00"
    return t


def aggregate_stats(conn: sqlite3.Connection, days: int = 30,
                    start: str = "", end: str = "") -> dict:
    """**调用级**聚合（2026-09-04 用户重定）：level=tool + caller∈{skill,mcp} +
    endpoint∈全部已登记的 REST/MCP 图谱调用端点。
    一次 REST /md（不管带几个 id）= 1 次调用；一次 MCP 工具调用 = 1 次。

    返回：total=累计调用次数；by_endpoint=按端点计数；top_users=最活跃用户 TOP10
    （按调用次数，优先工号展示，无工号则账号）；timeline=**自适应粒度**时间桶
    （时间窗 ≤2 天按小时、>2 天按天）；by_user/by_operator/by_session 同调用级。

    占位符动态生成（对抗审查 B4）。时间窗：start/end（ISO8601 或纯日期归一）
    优先于 days（近 N 天）。
    """
    ep_ph = ",".join("?" * len(_CALL_ENDPOINTS))
    ca_ph = ",".join("?" * len(_STATS_CALLERS))
    sql = (f"SELECT endpoint, user, operator, session_id, ts FROM telemetry "
           f"WHERE level='tool' AND caller IN ({ca_ph}) AND endpoint IN ({ep_ph})")
    params = [*list(_STATS_CALLERS), *list(_CALL_ENDPOINTS)]
    start = _norm_bound(start, is_end=False)
    end = _norm_bound(end, is_end=True)
    # 任一显式时间边界都优先于“近 N 天”：只给 end 表示从最早记录到该日，
    # 不能再偷偷叠加默认 30 天下界（否则测试/查询会随当前日期漂移）。
    c = start if start else (None if end else _cutoff_iso(days))
    if c:
        sql += " AND ts >= ?"
        params.append(c)
    if end:
        sql += " AND ts <= ?"
        params.append(end)

    # 自适应粒度：算出窗口实际跨度决定小时/天（统一补 UTC 时区防 naive/aware 混减）
    def _aware(ts_iso: str) -> datetime:
        dt = datetime.fromisoformat(ts_iso)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    span_hours = 999.0
    try:
        now = datetime.now(timezone.utc)
        t_start = _aware(c) if c else now - timedelta(days=days)
        t_end = _aware(end) if end else now
        span_hours = (t_end - t_start).total_seconds() / 3600
    except ValueError:
        pass
    by_day = span_hours > 48

    by_endpoint: dict[str, int] = {}
    by_user: dict[str, int] = {}
    by_operator: dict[str, int] = {}
    sessions: set = set()
    by_bucket: dict[str, int] = {}
    total = 0
    for r in conn.execute(sql, params).fetchall():
        total += 1
        ep = r["endpoint"] or "?"
        by_endpoint[ep] = by_endpoint.get(ep, 0) + 1
        u = r["user"] or "?"
        by_user[u] = by_user.get(u, 0) + 1
        op = r["operator"] or ""
        if op:
            by_operator[op] = by_operator.get(op, 0) + 1
        sid = r["session_id"] or ""
        if sid:
            sessions.add(sid)
        try:
            dt = datetime.fromisoformat((r["ts"] or "").replace("Z", "+00:00"))
            if dt.tzinfo is None:  # 存量行可能无时区标记 → 视作 UTC
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(_TZ_DISPLAY)
            bucket = local.strftime("%m-%d") if by_day else local.strftime("%m-%d %H:00")
            by_bucket[bucket] = by_bucket.get(bucket, 0) + 1
        except ValueError:
            continue

    timeline = [{"date": d, "count": n, "granularity": "day" if by_day else "hour"}
                for d, n in sorted(by_bucket.items())]

    # 最活跃用户 TOP10：按调用次数，工号优先展示（同一工号可能对应同一账号）
    user_calls: dict[str, int] = {}
    for r in conn.execute(sql, params).fetchall():
        label = r["operator"] or r["user"] or "?"
        user_calls[label] = user_calls.get(label, 0) + 1
    top_users = [{"user": u, "count": c}
                 for u, c in sorted(user_calls.items(), key=lambda x: -x[1])[:10]]

    return {
        "total": total,
        "by_endpoint": by_endpoint,
        "top_users": top_users,
        "timeline": timeline,
        "by_user": by_user,
        "by_operator": by_operator,
        "by_session": len(sessions),
    }


def _parse_cursor(since: str) -> tuple[str, int]:
    """拆 next_since 游标：'ts|rowid' → (ts, rowid)；纯 ISO → (ts, 0) 表示含边界起点。"""
    if "|" in since:
        ts, _, rid = since.partition("|")
        try:
            return ts, int(rid)
        except ValueError:
            return ts, 0
    return since, 0


def _jload(v: str):
    """params/result 列存 JSON 字符串 → 解析回对象（失败原样返回，底表不丢信息）。"""
    if not v:
        return None
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return v


def _event_of(r, *, with_payload: bool) -> dict:
    """telemetry 行 → 底表 event（caller 恒显式；payload 仅调用级含）。"""
    e = {"ts": r["ts"], "caller": r["caller"], "endpoint": r["endpoint"],
         "obj_id": r["obj_id"], "obj_type": r["obj_type"], "user": r["user"],
         "operator": r["operator"], "session_id": r["session_id"] or "",
         "level": r["level"] or ""}
    if with_payload:
        for k, col in (("params", r["params"]), ("result", r["result"])):
            v = _jload(col)
            if v is not None:
                e[k] = v
    return e


def list_skill_usage(conn: sqlite3.Connection, since: str = "", limit: int = 1000,
                     start: str = "", end: str = "", scope: str = "call") -> dict:
    """底表导出：原始事件 + next_since 游标 + has_more（ts 升序，供外部拉取拼接）。

    scope（2026-09-04 用户重定）：
      - ``call``（默认）：**调用级**——REST /md、/domains 每请求 1 行 +
        MCP 图谱工具每调用 1 行（level=tool，含 params/result）；
      - ``object``：**对象级**细粒度——每取一个对象 1 行（level=object，4 端点）；
      - ``all``：两类全含。
    每行显式带 caller（skill/mcp）。游标语义（next_since 不透明，原样回传）：
    since 留空 → 以 start（若有）为起点；'ts|rowid' → 精确推进（同 ts 多行不重不漏）。
    时间窗 start/end（ISO8601 或纯日期归一当天起止）；end 翻页全程生效。
    """
    levels = _SCOPE_LEVELS.get(scope, _SCOPE_LEVELS["call"])
    endpoints = _SCOPE_ENDPOINTS.get(scope, _SCOPE_ENDPOINTS["call"])
    lv_ph = ",".join("?" * len(levels))
    ep_ph = ",".join("?" * len(endpoints))
    ca_ph = ",".join("?" * len(_STATS_CALLERS))
    where = [f"level IN ({lv_ph})", f"caller IN ({ca_ph})", f"endpoint IN ({ep_ph})"]
    params = [*levels, *list(_STATS_CALLERS), *list(endpoints)]
    start = _norm_bound(start, is_end=False)
    end = _norm_bound(end, is_end=True)
    if since:
        cur_ts, cur_rowid = _parse_cursor(since)
        if cur_rowid > 0:
            where.append("((ts > ?) OR (ts = ? AND rowid > ?))")
            params += [cur_ts, cur_ts, cur_rowid]
        else:
            where.append("ts >= ?")
            params.append(cur_ts)
    elif start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)
    sql = ("SELECT ts, endpoint, obj_id, obj_type, user, operator, session_id, "
           "params, result, level, caller, rowid FROM telemetry "
           "WHERE " + " AND ".join(where) + " ORDER BY ts ASC, rowid ASC LIMIT ?")
    params.append(limit + 1)
    rows = conn.execute(sql, params).fetchall()
    returned = rows[:limit]
    events = [_event_of(r, with_payload=True) for r in returned]
    next_since = f"{returned[-1]['ts']}|{returned[-1]['rowid']}" if returned else (since or "")
    return {"events": events, "next_since": next_since, "has_more": len(rows) > limit}


def list_usage_table(conn: sqlite3.Connection, *, scope: str = "call",
                     start: str = "", end: str = "", endpoints: tuple = (),
                     user_like: str = "", page: int = 1, size: int = 50) -> dict:
    """运维页底表表格（2026-09-04）：**时间倒序** + 服务端分页 + 筛选。

    筛选：scope 粒度（call/object/all）+ 时间窗 + 端点多选 + 账号/工号子串。
    返回 {rows, total}；行结构与 list_skill_usage 一致（含 caller，payload 仅调用级）。
    """
    levels = _SCOPE_LEVELS.get(scope, _SCOPE_LEVELS["call"])
    eps = tuple(e for e in endpoints if e in _CALL_ENDPOINTS) or _SCOPE_ENDPOINTS.get(
        scope, _SCOPE_ENDPOINTS["call"])
    lv_ph = ",".join("?" * len(levels))
    ep_ph = ",".join("?" * len(eps))
    ca_ph = ",".join("?" * len(_STATS_CALLERS))
    where = [f"level IN ({lv_ph})", f"caller IN ({ca_ph})", f"endpoint IN ({ep_ph})"]
    params = [*levels, *list(_STATS_CALLERS), *list(eps)]
    start = _norm_bound(start, is_end=False)
    end = _norm_bound(end, is_end=True)
    if start:
        where.append("ts >= ?")
        params.append(start)
    if end:
        where.append("ts <= ?")
        params.append(end)
    if user_like:
        where.append("(user LIKE ? OR operator LIKE ?)")
        params += [f"%{user_like}%", f"%{user_like}%"]
    cond = " AND ".join(where)
    total = conn.execute(
        f"SELECT COUNT(*) FROM telemetry WHERE {cond}", params).fetchone()[0]
    page = max(1, page or 1)
    size = min(max(1, size or 50), 200)
    rows = conn.execute(
        "SELECT ts, endpoint, obj_id, obj_type, user, operator, session_id, "
        "params, result, level, caller FROM telemetry "
        f"WHERE {cond} ORDER BY ts DESC, rowid DESC LIMIT ? OFFSET ?",
        (*params, size, (page - 1) * size)).fetchall()
    return {"rows": [_event_of(r, with_payload=True) for r in rows], "total": total}


def aggregate_activity(conn: sqlite3.Connection, username: str, days: int = 30) -> list:
    sql = ("SELECT ts, endpoint, caller, operator FROM telemetry "
           "WHERE user=? AND level='request'")
    params = [username]
    c = _cutoff_iso(days)
    if c:
        sql += " AND ts >= ?"
        params.append(c)
    sql += " ORDER BY ts DESC"
    return [{"ts": r["ts"], "endpoint": r["endpoint"], "caller": r["caller"],
             "operator": r["operator"]} for r in conn.execute(sql, params).fetchall()]
