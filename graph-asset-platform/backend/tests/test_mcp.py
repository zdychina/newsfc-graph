"""MCP 服务测试（Streamable HTTP /mcp，stateless + json_response）。

覆盖：鉴权（401/403）、tools/list、get_domains / get_md（部分失败容错 + 双护栏）/
search_objects / search_md / get_object、三层打点（request/tool/object +
operator/session_id 上下文参数）与统计口径（caller IN ('skill','mcp')）。

HTTP 直测（TestClient + JSON-RPC）：真实客户端需带 Accept: application/json。
"""
import io
import json
import zipfile

import app.db as dbmod
import app.service as svc
from app.index import Index
from app.registry import Registry
from app.store import Store

ACC = {"Accept": "application/json, text/event-stream"}

CMD = """---
id: UDG@MMLCommand@ADD URR
type: MMLCommand
name: ADD URR
version: 20.15.2
---

# ADD URR

在线计费的使用量上报规则配置命令。参数 RG 表示计费组。

## 边

- 参见 [[UDG@MMLCommand@LST URR]]
"""

FEATURE = """---
id: UDG@Feature@GWFD-020300
type: Feature
name: 在线计费特性
version: 20.15.2
---

特性正文：支持在线计费的配额管理与用量上报。
"""

DOMAIN = """---
id: BusinessDomain@business-awareness
type: BusinessDomain
name: 业务感知
domain: business-awareness
---

业务域正文：流量识别与业务感知，覆盖计费与策略控制场景。
"""


def _setup(tmp_data_dir, monkeypatch, files=None):
    s = svc.Service.__new__(svc.Service)
    s.store = Store(tmp_data_dir)
    s.registry = Registry.load_default()
    s.db = dbmod.get_db(tmp_data_dir.parent / "test.db")
    dbmod.init_schema(s.db)
    monkeypatch.setattr(dbmod, "_shared", s.db, raising=False)
    from app.bundle import import_bundle
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in (files or {}).items():
            z.writestr(name, content)
    import_bundle(buf.getvalue(), s.store, s.registry)
    s.rebuild()
    s.fts_rebuilding = False
    monkeypatch.setattr(svc, "_service", s)
    from app.users.store import add_user
    add_user({"username": "admin", "key": "gap_admin", "can_frontend": True,
              "can_skill": True, "is_admin": True})
    add_user({"username": "ao", "key": "gap_assets_only", "can_assets": True})
    return s


def _client():
    from app.main import app
    from fastapi.testclient import TestClient
    return TestClient(app)


def _call(c, name, arguments, key="gap_admin", sid=1):
    r = c.post("/mcp", headers={"X-API-Key": key, **ACC},
               json={"jsonrpc": "2.0", "id": sid, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("result", {}).get("isError") is False, body
    return json.loads(body["result"]["content"][0]["text"])


def _call_err(c, name, arguments, key="gap_admin"):
    r = c.post("/mcp", headers={"X-API-Key": key, **ACC},
               json={"jsonrpc": "2.0", "id": 99, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["result"]["isError"] is True, body
    return body["result"]["content"][0]["text"]


_CTX = {"AGENT_USERNAME": "00234567", "AGENT_SESSION_ID": "sess-1"}


# ---------------- 鉴权 ----------------

def test_mcp_401_without_key(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        r = c.post("/mcp", headers=ACC, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 401
        assert "detail" in r.json()  # 401 响应体可读（审查 A4）


def test_mcp_403_without_skill_perm(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        r = c.post("/mcp", headers={"X-API-Key": "gap_assets_only", **ACC},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 403


# ---------------- 协议 ----------------

def test_tools_list_returns_4_public_tools(tmp_data_dir, monkeypatch):
    """v13 三态迁移后 tools/list 只展示公开工具（search_files 注册后 4 个，§15.4）。"""
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        r = c.post("/mcp", headers={"X-API-Key": "gap_admin", **ACC},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert r.status_code == 200
        tools = r.json()["result"]["tools"]
        names = {t["name"] for t in tools}
        assert names == {"get_domains", "get_md", "search_graph", "search_files"}
        by = {t["name"]: t for t in tools}
        # 上下文参数必填（required）且 description 指向沙箱环境变量
        get_md = by["get_md"]
        assert "AGENT_USERNAME" in get_md["inputSchema"]["required"]
        assert "AGENT_SESSION_ID" in get_md["inputSchema"]["required"]
        assert "_AGENT_USERNAME" in get_md["inputSchema"]["properties"]["AGENT_USERNAME"]["description"]
        assert get_md["inputSchema"]["properties"]["AGENT_USERNAME"]["minLength"] == 1
        assert get_md["inputSchema"]["properties"]["AGENT_USERNAME"]["maxLength"] == 64
        assert get_md["inputSchema"]["properties"]["AGENT_SESSION_ID"]["minLength"] == 1
        assert get_md["inputSchema"]["properties"]["AGENT_SESSION_ID"]["maxLength"] == 128
        # schema-first（§9.1/§15.2）：enum/min/max/description/additionalProperties
        sg = by["search_graph"]
        assert sg["inputSchema"]["properties"]["match"]["enum"] == ["any", "all"]
        assert sg["inputSchema"]["properties"]["terms"]["minItems"] == 1
        assert sg["inputSchema"]["properties"]["terms"]["maxItems"] == 10
        assert sg["inputSchema"]["properties"]["size"]["maximum"] == 50
        assert sg["inputSchema"]["properties"]["page"]["minimum"] == 1
        layer_spec = sg["inputSchema"]["properties"]["layer"]
        layer_enum = layer_spec.get("enum") or layer_spec["anyOf"][0]["enum"]
        assert layer_enum == ["命令层", "特性层", "任务层", "业务层"]
        for fname, spec in sg["inputSchema"]["properties"].items():
            assert spec.get("description"), f"search_graph.{fname} 缺 description"
        assert sg["inputSchema"]["additionalProperties"] is False
        # 三工具 outputSchema 非空（§8.3：真实 tools/list 验证）
        assert sg["outputSchema"], "search_graph outputSchema 必须非空"
        assert by["get_domains"]["outputSchema"], "get_domains outputSchema 必须非空"
        assert get_md["outputSchema"], "get_md outputSchema 必须非空"
        # get_md outputSchema：动态 ID map + success/failure 联合（§8.3）
        ap = get_md["outputSchema"].get("additionalProperties") or {}
        assert "anyOf" in ap or "oneOf" in ap or "$ref" in ap
        # ids 护栏进 schema
        assert get_md["inputSchema"]["properties"]["ids"]["maxItems"] == 100
        sf = by["search_files"]["inputSchema"]["properties"]
        def _max_length(spec):
            return spec.get("maxLength") or next(
                (branch.get("maxLength") for branch in spec.get("anyOf", [])
                 if branch.get("maxLength")), None)
        assert _max_length(sf["query"]) == 200
        assert _max_length(sf["path"]) == 1024
        assert _max_length(sf["ext"]) == 64
        assert _max_length(sf["after"]) == 1024


def test_hidden_legacy_still_callable_with_old_shape(tmp_data_dir, monkeypatch):
    """默认态旧工具不出现在 tools/list，但 hidden 可被旧客户端直调且形态冻结（§11.3）。"""
    s = _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD,
                                           "Feature/UDG/20.15.2/f.md": FEATURE})
    with _client() as c:
        # 直调 hidden 旧工具仍成功
        out = _call(c, "search_objects", {**_CTX, "q": "urr", "type": "MMLCommand"})
        assert out["total"] == 1
        assert out["rows"][0]["id"] == "UDG@MMLCommand@ADD URR"
        out2 = _call(c, "search_md", {**_CTX, "q": "计费"})
        assert out2["total"] >= 1 and "snippet" in out2["hits"][0]
        out3 = _call(c, "get_object", {**_CTX, "id": "UDG@MMLCommand@ADD URR"})
        assert out3["type"] == "MMLCommand" and "out_edges" in out3
        # legacy 打点：deprecated + replacement，且只写自己的旧 endpoint tool 行
        from app.telemetry.recorder import flush as _tel_flush
        assert _tel_flush()
        rows = [dict(r) for r in s.db.execute(
            "SELECT endpoint, result FROM telemetry WHERE level='tool' "
            "ORDER BY rowid").fetchall()]
        legacy = [r for r in rows if r["endpoint"].startswith("mcp:search")
                  or r["endpoint"] == "mcp:get_object"]
        assert len(legacy) == 3  # 每外部调用恰好 1 条旧 endpoint 行（§11.3）
        for r in legacy:
            res = json.loads(r["result"])
            assert res["deprecated"] is True
            assert res["replacement"] in ("search_graph", "get_md")
        # 不产生新 endpoint 双重打点
        assert not [r for r in rows if r["endpoint"] in ("mcp:search_graph", "mcp:get_md")]


def test_disabled_legacy_returns_tool_disabled(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD})
    from app.repos import mcp_tools_repo
    from app.db import get_shared_db
    from app.service import import_lock
    with _client() as c:
        conn = get_shared_db()
        with import_lock:
            mcp_tools_repo.upsert(conn, tool_name="search_md",
                                  visibility="disabled", description="",
                                  updated_by="admin")
            conn.commit()
        msg = _call_err(c, "search_md", {**_CTX, "q": "计费"})
        body = json.loads(msg)
        assert body["error"]["code"] == "TOOL_DISABLED"
        assert "search_graph" in body["error"]["message"]


def test_admin_rollback_makes_legacy_visible(tmp_data_dir, monkeypatch):
    """管理员显式回滚态（visibility=visible）legacy 才重新出现在 tools/list（§11.3）。"""
    _setup(tmp_data_dir, monkeypatch)
    from app.repos import mcp_tools_repo
    from app.db import get_shared_db
    from app.service import import_lock
    with _client() as c:
        conn = get_shared_db()
        with import_lock:
            mcp_tools_repo.upsert(conn, tool_name="search_md",
                                  visibility="visible", description="",
                                  updated_by="admin")
            conn.commit()
        r = c.post("/mcp", headers={"X-API-Key": "gap_admin", **ACC},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        names = {t["name"] for t in r.json()["result"]["tools"]}
        assert "search_md" in names
        assert {"get_domains", "get_md", "search_graph"} <= names


# ---------------- 工具语义 ----------------

def test_get_domains_returns_md(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch, {"Business/business-awareness/x.md": DOMAIN})
    with _client() as c:
        out = _call(c, "get_domains", dict(_CTX))
    assert [d["id"] for d in out["domains"]] == ["BusinessDomain@business-awareness"]
    assert "业务感知" in out["domains"][0]["md"]
    from app.telemetry.recorder import flush as _tel_flush
    assert _tel_flush()  # 打点异步落库（v3 队列化）
    rows = [dict(r) for r in s.db.execute(
        "SELECT * FROM telemetry WHERE level='object'").fetchall()]
    assert len(rows) == 1
    assert rows[0]["endpoint"] == "mcp:get_domains"
    assert rows[0]["caller"] == "mcp"
    assert rows[0]["operator"] == "00234567"
    assert rows[0]["session_id"] == "sess-1"
    assert rows[0]["user"] == "admin"


def test_get_md_partial_failure_and_version(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD})
    with _client() as c:
        out = _call(c, "get_md", {**_CTX, "ids": ["UDG@MMLCommand@ADD URR", "no@such@id"]})
    ok = out["UDG@MMLCommand@ADD URR"]
    assert ok["version"] == "20.15.2" and "在线计费" in ok["md"]
    assert out["no@such@id"]["error"] == "对象不存在"
    # version 全局不匹配但 id 存在 → 回带 available_versions（部分失败容错）
    with _client() as c:
        out2 = _call(c, "get_md", {**_CTX, "ids": ["UDG@MMLCommand@ADD URR"], "version": "9.9.9"})
    assert "available_versions" in out2["UDG@MMLCommand@ADD URR"]
    assert "20.15.2" in out2["UDG@MMLCommand@ADD URR"]["available_versions"]


def test_get_md_ids_cap(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD})
    with _client() as c:
        msg = _call_err(c, "get_md", {**_CTX, "ids": [f"x@y@{i}" for i in range(101)]})
    assert "100" in msg


def test_get_md_byte_cap(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD,
                                           "Feature/UDG/20.15.2/f.md": FEATURE})
    # 护栏常量已移共享核心（graph_query.contracts，2026-09-08 三工具重构 M1）
    import app.graph_query.contracts as gq
    monkeypatch.setattr(gq, "MAX_TOTAL_BYTES", 10)
    with _client() as c:
        msg = _call_err(c, "get_md", {**_CTX,
                                      "ids": ["UDG@MMLCommand@ADD URR", "UDG@Feature@GWFD-020300"]})
    assert "分批" in msg
    # 业务错误统一 envelope：{"error": {code, message, ...}}（§5.2）
    assert json.loads(msg)["error"]["code"] == "RESULT_TOO_LARGE"


def test_search_objects_over_http(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD,
                                       "Feature/UDG/20.15.2/f.md": FEATURE})
    with _client() as c:
        out = _call(c, "search_objects", {**_CTX, "q": "urr", "type": "MMLCommand"})
    assert out["total"] == 1
    assert out["rows"][0]["id"] == "UDG@MMLCommand@ADD URR"


def test_search_md_over_http(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD,
                                       "Feature/UDG/20.15.2/f.md": FEATURE})
    with _client() as c:
        out = _call(c, "search_md", {**_CTX, "q": "计费", "type": "MMLCommand"})
    assert out["total"] == 1
    hit = out["hits"][0]
    assert hit["id"] == "UDG@MMLCommand@ADD URR"
    assert "【" in hit["snippet"]


def test_get_object_over_http(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD})
    with _client() as c:
        out = _call(c, "get_object", {**_CTX, "id": "UDG@MMLCommand@ADD URR"})
    assert out["type"] == "MMLCommand"
    assert out["versions"] == ["20.15.2"]
    assert isinstance(out["out_edges"], list)
    with _client() as c:
        msg = _call_err(c, "get_object", {**_CTX, "id": "no@such"})
    assert "不存在" in msg


# ---------------- 打点与统计口径 ----------------

def test_telemetry_three_levels_and_stats(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD,
                                           "Business/business-awareness/x.md": DOMAIN})
    with _client() as c:
        _call(c, "get_md", {**_CTX, "ids": ["UDG@MMLCommand@ADD URR"]})
        _call(c, "search_md", {**_CTX, "q": "计费"}, sid=2)
    from app.telemetry.recorder import flush as _tel_flush
    assert _tel_flush()
    rows = [dict(r) for r in s.db.execute(
        "SELECT level, endpoint, caller, operator, session_id FROM telemetry ORDER BY rowid"
    ).fetchall()]
    levels = {(r["level"], r["endpoint"]) for r in rows}
    # request 级（"/mcp"）已随打点瘦身移除（2026-08-26·方案B）：仅 tool + object 两层
    assert ("request", "/mcp") not in levels
    assert ("tool", "mcp:get_md") in levels                  # tool 级（含 search 类可观测）
    assert ("tool", "mcp:search_md") in levels
    assert ("object", "mcp:get_md") in levels                # object 级（取用统计口径）
    mcp_rows = [r for r in rows if r["level"] == "object"]
    assert all(r["caller"] == "mcp" and r["operator"] == "00234567"
               and r["session_id"] == "sess-1" for r in mcp_rows)
    # 统计聚合：mcp 行进入口径 + by_session 计数
    from app.repos.telemetry_repo import aggregate_stats
    st = aggregate_stats(s.db)
    assert st["total"] >= 1
    assert st["by_session"] >= 1
    # skill 历史行与新 mcp 行同口径并存（stats 已改调用级 2026-09-04：tool 行 +1）
    from app.telemetry.recorder import record
    record("/md", user="sk", caller="skill", level="tool")
    assert _tel_flush()
    st2 = aggregate_stats(s.db)
    assert st2["total"] == st["total"] + 1


def test_tool_rows_record_params_and_result(tmp_data_dir, monkeypatch):
    """tool 级行记录入参（params）与出参摘要（result）——用户决策：输入输出都记。"""
    s = _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD,
                                           "Feature/UDG/20.15.2/f.md": FEATURE})
    with _client() as c:
        _call(c, "get_md", {**_CTX, "ids": ["UDG@MMLCommand@ADD URR", "no@such@id"]})
        _call(c, "search_md", {**_CTX, "q": "计费"}, sid=2)
    from app.telemetry.recorder import flush as _tel_flush
    assert _tel_flush()
    tool_rows = [dict(r) for r in s.db.execute(
        "SELECT endpoint, params, result FROM telemetry WHERE level='tool' ORDER BY rowid"
    ).fetchall()]
    by_ep = {r["endpoint"]: (json.loads(r["params"]), json.loads(r["result"]))
             for r in tool_rows}
    p, r = by_ep["mcp:get_md"]
    assert p == {"ids": ["UDG@MMLCommand@ADD URR", "no@such@id"], "version": None}
    assert r["ok"] == 1 and r["failed"] == 1
    assert r["failed_ids"] == ["no@such@id"]
    assert r["bytes"] > 0
    p2, r2 = by_ep["mcp:search_md"]
    assert p2["q"] == "计费"
    assert r2["total"] == 2 and r2["returned"] == 2
    assert "UDG@MMLCommand@ADD URR" in r2["top_ids"]


def test_tool_row_records_error_result(tmp_data_dir, monkeypatch):
    """护栏触发（响应超限）→ tool 行 result 记结构化 error 摘要（失败也留痕）。

    101 ids 现在在 schema 层被拒（maxItems 进 inputSchema，§9.1）——函数未执行
    不落 tool 行，与 REST 解析失败不落行同口径；业务护栏失败仍留痕。"""
    s = _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD})
    import app.graph_query.contracts as gq
    monkeypatch.setattr(gq, "MAX_TOTAL_BYTES", 10)
    with _client() as c:
        _call_err(c, "get_md", {**_CTX, "ids": ["UDG@MMLCommand@ADD URR"]})
    from app.telemetry.recorder import flush as _tel_flush
    assert _tel_flush()
    rows = [dict(r) for r in s.db.execute(
        "SELECT params, result FROM telemetry WHERE level='tool' AND endpoint='mcp:get_md'"
    ).fetchall()]
    assert rows
    result = json.loads(rows[0]["result"])
    assert result["error"]["code"] == "RESULT_TOO_LARGE"
    assert json.loads(rows[0]["params"])["ids"] == ["UDG@MMLCommand@ADD URR"]


# ---------------- search_files（2026-09-29） ----------------

def test_mcp_search_files_query(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, files={"cmd.md": CMD, "feat.md": FEATURE})
    with _client() as c:
        out = _call(c, "search_files", {**_CTX, "query": "ADD URR"})
    assert out["total"] == 1
    hit = out["files"][0]
    assert hit["obj_id"] == "UDG@MMLCommand@ADD URR"
    assert hit["version"] == "20.15.2"
    assert out["index_building"] is False


def test_mcp_search_files_ls_mode(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, files={"cmd.md": CMD})
    with _client() as c:
        out = _call(c, "search_files", {**_CTX, "path": "Command"})
    assert [(f["name"], f["is_dir"], f["obj_id"]) for f in out["files"]] == \
        [("UDG", True, None)]


def test_mcp_search_files_errors(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, files={"cmd.md": CMD})
    with _client() as c:
        err1 = json.loads(_call_err(c, "search_files", {**_CTX}))
        err2 = json.loads(_call_err(c, "search_files", {**_CTX, "query": "x"}))
        err3 = json.loads(_call_err(
            c, "search_files", {**_CTX, "query": "ADD URR", "typo": 1}))
    assert err1["error"]["code"] == "INVALID_ARGUMENT"   # 无过滤
    assert err2["error"]["code"] == "INVALID_ARGUMENT"   # 1 字符
    assert err3["error"]["code"] == "INVALID_ARGUMENT"   # 未知参数拦截
