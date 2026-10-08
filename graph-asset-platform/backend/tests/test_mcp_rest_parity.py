"""MCP / REST 双通道对账测试（M1，需求 §10/§15.3）。

同一 fixture 同时走 MCP（/mcp JSON-RPC）与 REST（POST /api/v1/domains|/md）：
业务数据、错误、护栏、遥测归因必须一致，仅允许 caller/endpoint 与外层包装差异。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

from tests.test_api_objects import _setup, CMD_EDGES, CMD_V2, CFG

BIZ = (
    "---\n"
    "id: alpha@BusinessDomain@demo\n"
    "type: BusinessDomain\n"
    "domain: demo\n"
    "---\n"
    "# 业务感知\n"
    "引用 [[alpha@MMLCommand@ADD DEMO]]。\n"
)

ACC = {"Accept": "application/json, text/event-stream"}
CTX = {"AGENT_USERNAME": "00234567", "AGENT_SESSION_ID": "sess-parity-1"}

client = TestClient(app)


def _seed(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch,
           {"cmd.md": CMD_EDGES, "v2.md": CMD_V2, "cfg.md": CFG, "biz.md": BIZ})


def _mcp(c, name, arguments, sid=1):
    r = c.post("/mcp", headers={"X-API-Key": "gap_test_admin", **ACC},
               json={"jsonrpc": "2.0", "id": sid, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _mcp_ok(c, name, arguments, sid=1):
    result = _mcp(c, name, arguments, sid)
    assert result["isError"] is False, result
    return json.loads(result["content"][0]["text"])


def _mcp_err(c, name, arguments):
    result = _mcp(c, name, arguments)
    assert result["isError"] is True, result
    return json.loads(result["content"][0]["text"])


# ---------------- 业务数据一致 ----------------

def test_domains_parity_mcp_envelope_equals_rest_array(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        mcp_out = _mcp_ok(c, "get_domains", dict(CTX))
        rest = c.post("/api/v1/domains", json=dict(CTX))
    assert rest.status_code == 200, rest.text
    # 唯一允许的包装差异：mcp.domains == rest 裸数组
    assert mcp_out["domains"] == rest.json()


def test_get_md_parity_full_map(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    ids = ["alpha@MMLCommand@ADD DEMO", "alpha@ConfigObject@DEMO_OBJ", "nope@MMLCommand@X"]
    with TestClient(app) as c:
        mcp_out = _mcp_ok(c, "get_md", {**CTX, "ids": ids})
        rest = c.post("/api/v1/md", json={**CTX, "ids": ids})
    assert rest.status_code == 200, rest.text
    assert mcp_out == rest.json()


def test_get_md_parity_dedup_and_version_semantics(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    ids = [" alpha@ConfigObject@DEMO_OBJ ", "alpha@ConfigObject@DEMO_OBJ"]
    with TestClient(app) as c:
        mcp_out = _mcp_ok(c, "get_md", {**CTX, "ids": ids})
        rest = c.post("/api/v1/md", json={**CTX, "ids": ids})
        # 显式旧版本语义一致
        mcp_old = _mcp_ok(c, "get_md",
                          {**CTX, "ids": ["alpha@MMLCommand@ADD DEMO"], "version": "20.15.2"},
                          sid=2)
        rest_old = c.post("/api/v1/md", json={
            **CTX, "ids": ["alpha@MMLCommand@ADD DEMO"], "version": "20.15.2"})
        # 版本缺失单项结构一致
        mcp_miss = _mcp_ok(c, "get_md",
                           {**CTX, "ids": ["alpha@MMLCommand@ADD DEMO"], "version": "19.0.0"},
                           sid=3)
        rest_miss = c.post("/api/v1/md", json={
            **CTX, "ids": ["alpha@MMLCommand@ADD DEMO"], "version": "19.0.0"})
    assert mcp_out == rest.json()
    assert list(mcp_out.keys()) == ["alpha@ConfigObject@DEMO_OBJ"]  # trim 去重
    assert mcp_old == rest_old.json()
    assert mcp_old["alpha@MMLCommand@ADD DEMO"]["version"] == "20.15.2"
    assert mcp_miss == rest_miss.json()
    miss = mcp_miss["alpha@MMLCommand@ADD DEMO"]
    assert miss["error_code"] == "VERSION_NOT_FOUND"
    assert set(miss["available_versions"]) == {"20.15.2", "20.16.0"}


# ---------------- 护栏一致 ----------------

def test_ids_cap_101_fails_both_channels(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    ids = [f"x@y@{i}" for i in range(101)]
    with TestClient(app) as c:
        mcp_err = _mcp_err(c, "get_md", {**CTX, "ids": ids})
        rest = c.post("/api/v1/md", json={**CTX, "ids": ids})
    assert mcp_err["error"]["code"] == "INVALID_ARGUMENT"
    assert rest.status_code == 422
    assert rest.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_byte_cap_2mb_fails_both_channels(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    from app.graph_query import contracts as gq
    monkeypatch.setattr(gq, "MAX_TOTAL_BYTES", 10)
    ids = ["alpha@MMLCommand@ADD DEMO", "alpha@ConfigObject@DEMO_OBJ"]
    with TestClient(app) as c:
        mcp_err = _mcp_err(c, "get_md", {**CTX, "ids": ids})
        rest = c.post("/api/v1/md", json={**CTX, "ids": ids})
    assert mcp_err["error"]["code"] == "RESULT_TOO_LARGE"
    assert rest.status_code == 413
    assert rest.json()["error"]["code"] == "RESULT_TOO_LARGE"


# ---------------- REST 错误 wire 形态 ----------------

def test_rest_validation_error_envelope(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        # 缺归因字段
        r1 = c.post("/api/v1/domains", json={})
        # 归因字段超长
        r2 = c.post("/api/v1/md", json={
            "AGENT_USERNAME": "x" * 65, "AGENT_SESSION_ID": "s", "ids": ["a@b@c"]})
        # 未知字段（extra=forbid，防拼错静默忽略）
        r3 = c.post("/api/v1/md", json={
            **CTX, "ids": ["alpha@ConfigObject@DEMO_OBJ"], "id_list": ["x"]})
        # 非 JSON body
        r4 = c.post("/api/v1/domains", content=b"not-json",
                    headers={"Content-Type": "application/json"})
    for r in (r1, r2, r3, r4):
        assert r.status_code == 422, r.text
        err = r.json()["error"]
        assert err["code"] == "INVALID_ARGUMENT"
        assert err["message"]
        assert isinstance(err["retryable"], bool)


def test_internal_error_no_leak_both_channels(tmp_data_dir, monkeypatch):
    """INTERNAL_ERROR 对外只有固定通用消息；traceback/SQL/路径不得进响应。"""
    _seed(tmp_data_dir, monkeypatch)
    secret = "RuntimeError: SELECT * FROM objects WHERE x='\\'; C:/secret/path/db.sqlite"

    def _boom(ids, version=None):
        raise RuntimeError(secret)

    import app.graph_query.read as gq_read
    monkeypatch.setattr(gq_read, "get_md_core", _boom)
    with TestClient(app) as c:
        mcp_err = _mcp_err(c, "get_md", {**CTX, "ids": ["alpha@ConfigObject@DEMO_OBJ"]})
        rest = c.post("/api/v1/md", json={**CTX, "ids": ["alpha@ConfigObject@DEMO_OBJ"]})
    assert mcp_err["error"]["code"] == "INTERNAL_ERROR"
    assert secret not in json.dumps(mcp_err)
    assert rest.status_code == 500
    body = rest.json()
    assert body["error"]["code"] == "INTERNAL_ERROR"
    assert secret not in json.dumps(body)


# ---------------- 遥测对账 ----------------

def test_telemetry_parity_except_caller_endpoint(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    ids = ["alpha@MMLCommand@ADD DEMO", "nope@MMLCommand@X"]
    with TestClient(app) as c:
        _mcp_ok(c, "get_md", {**CTX, "ids": ids})
        c.post("/api/v1/md", json={**CTX, "ids": ids})
    from app.service import get_service
    from app.telemetry.recorder import flush
    assert flush()
    db = get_service().db
    rows = [dict(r) for r in db.execute(
        "SELECT level, endpoint, caller, user, operator, session_id, params, result "
        "FROM telemetry WHERE endpoint IN ('mcp:get_md', '/md') ORDER BY rowid"
    ).fetchall()]
    assert len(rows) == 4  # 每通道 1 tool + 1 object（失败 id 不写 object 行）
    mcp_rows = sorted((r for r in rows if r["caller"] == "mcp"), key=lambda r: r["level"])
    rest_rows = sorted((r for r in rows if r["caller"] == "skill"), key=lambda r: r["level"])
    for m, s in zip(mcp_rows, rest_rows):
        assert m["level"] == s["level"]
        assert m["operator"] == s["operator"] == "00234567"
        assert m["session_id"] == s["session_id"] == "sess-parity-1"
        assert m["user"] == s["user"] == "admin"
        if m["level"] == "tool":  # object 行 params/result 为空串（设计如此）
            assert json.loads(m["params"]) == json.loads(s["params"])
            assert json.loads(m["result"]) == json.loads(s["result"])
    # 失败 id 不产生 object 行（每通道 object 行仅 1 条）
    assert sum(1 for r in rows if r["level"] == "object") == 2


def test_get_md_telemetry_only_after_guard_pass(tmp_data_dir, monkeypatch):
    """整单通过护栏后才写 object 遥测——护栏失败不留『成功取用』对象点。"""
    _seed(tmp_data_dir, monkeypatch)
    from app.graph_query import contracts as gq
    monkeypatch.setattr(gq, "MAX_TOTAL_BYTES", 10)
    with TestClient(app) as c:
        _mcp_err(c, "get_md", {**CTX, "ids": ["alpha@MMLCommand@ADD DEMO"]})
        c.post("/api/v1/md", json={**CTX, "ids": ["alpha@MMLCommand@ADD DEMO"]})
    from app.service import get_service
    from app.telemetry.recorder import flush
    assert flush()
    db = get_service().db
    n_object = db.execute(
        "SELECT COUNT(*) FROM telemetry WHERE level='object'").fetchone()[0]
    n_tool = db.execute(
        "SELECT COUNT(*) FROM telemetry WHERE level='tool'").fetchone()[0]
    assert n_object == 0  # 护栏失败：无对象行
    assert n_tool == 2    # 两通道各 1 条 tool 行（失败也留痕）


# ---------------- search：MCP search_graph 与 REST POST /search 对账 ----------------

def test_search_parity_full_response(tmp_data_dir, monkeypatch):
    """REST /search 与 MCP search_graph 返回完全相同（同 core，§10 同款要求）。"""
    _seed(tmp_data_dir, monkeypatch)
    args = {"terms": ["DEMO"], "match": "any", "type": "MMLCommand", "size": 5}
    with TestClient(app) as c:
        mcp_out = _mcp_ok(c, "search_graph", {**CTX, **args})
        rest = c.post("/api/v1/search", json={**CTX, **args})
    assert rest.status_code == 200, rest.text
    assert mcp_out == rest.json()


def test_search_parity_zero_result_and_filters(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        mcp_zero = _mcp_ok(c, "search_graph", {**CTX, "terms": ["不存在的词"]})
        rest_zero = c.post("/api/v1/search", json={**CTX, "terms": ["不存在的词"]})
        # 过滤生效口径一致（type 收窄；fixture nf 为小写 alpha，会被自动转
        # 大写后判非法——那是生产行为，在错误用例里单独断言）
        mcp_nf = _mcp_ok(c, "search_graph", {**CTX, "terms": ["DEMO"], "type": "ConfigObject"})
        rest_nf = c.post("/api/v1/search", json={
            **CTX, "terms": ["DEMO"], "type": "ConfigObject"})
        # nf 自动大写 + 合法值回带（两通道一致）
        mcp_bad_nf = _mcp_err(c, "search_graph", {**CTX, "terms": ["DEMO"], "nf": "alpha"})
        rest_bad_nf = c.post("/api/v1/search", json={**CTX, "terms": ["DEMO"], "nf": "alpha"})
    assert rest_zero.status_code == 200
    assert mcp_zero == rest_zero.json()
    assert mcp_zero["total"] == 0
    assert "REMOVE_OR_REPHRASE_TERM" in mcp_zero["diagnostics"]["recovery_codes"]
    assert mcp_nf == rest_nf.json()
    assert mcp_nf["applied_filters"] == {"type": "ConfigObject"}
    assert rest_bad_nf.status_code == 422
    assert rest_bad_nf.json()["error"]["code"] == "INVALID_FILTER"
    assert rest_bad_nf.json()["error"]["details"]["available_values"] == ["alpha"]
    assert mcp_bad_nf["error"] == rest_bad_nf.json()["error"]


def test_search_parity_invalid_filter_and_validation(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        mcp_err = _mcp_err(c, "search_graph", {**CTX, "terms": ["DEMO"], "nf": "NOPE"})
        rest_bad_filter = c.post("/api/v1/search", json={
            **CTX, "terms": ["DEMO"], "nf": "NOPE"})
        # 模型层校验（非法 enum/越界）→ INVALID_ARGUMENT 422 envelope
        rest_bad_enum = c.post("/api/v1/search", json={
            **CTX, "terms": ["DEMO"], "match": "phrase"})
        rest_bad_size = c.post("/api/v1/search", json={
            **CTX, "terms": ["DEMO"], "size": 500})
        rest_unknown_field = c.post("/api/v1/search", json={
            **CTX, "terms": ["DEMO"], "q": "旧字段"})
    assert rest_bad_filter.status_code == 422
    assert rest_bad_filter.json()["error"]["code"] == "INVALID_FILTER"
    assert rest_bad_filter.json()["error"]["details"]["field"] == "nf"
    assert mcp_err["error"]["code"] == rest_bad_filter.json()["error"]["code"]
    for r in (rest_bad_enum, rest_bad_size, rest_unknown_field):
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_search_telemetry_parity(tmp_data_dir, monkeypatch):
    """两通道各 1 条 tool 行（search 无 object 行）；params/result 摘要一致，
    仅 caller/endpoint 不同（§7.9 同款对账）。"""
    _seed(tmp_data_dir, monkeypatch)
    args = {"terms": ["DEMO"], "match": "any", "type": "MMLCommand"}
    with TestClient(app) as c:
        _mcp_ok(c, "search_graph", {**CTX, **args})
        c.post("/api/v1/search", json={**CTX, **args})
    from app.service import get_service
    from app.telemetry.recorder import flush
    assert flush()
    db = get_service().db
    rows = [dict(r) for r in db.execute(
        "SELECT caller, endpoint, level, user, operator, session_id, params, result "
        "FROM telemetry WHERE endpoint IN ('mcp:search_graph', '/search') "
        "AND level='tool' ORDER BY rowid").fetchall()]
    assert len(rows) == 2
    assert {(r["caller"], r["endpoint"]) for r in rows} == {
        ("mcp", "mcp:search_graph"), ("skill", "/search")}
    m, s = rows[0], rows[1]
    assert m["operator"] == s["operator"] == "00234567"
    assert m["session_id"] == s["session_id"] == "sess-parity-1"
    assert m["user"] == s["user"] == "admin"
    assert json.loads(m["params"]) == json.loads(s["params"])
    rm, rs = json.loads(m["result"]), json.loads(s["result"])
    assert rm["total"] == rs["total"] and rm["returned"] == rs["returned"]
    assert rm["top_ids"] == rs["top_ids"]
    assert rm["recovery_codes"] == rs["recovery_codes"]
    # search 不产生 object 行
    n_object = db.execute(
        "SELECT COUNT(*) FROM telemetry WHERE level='object'").fetchone()[0]
    assert n_object == 0


# ---------------- search_files 第四对（2026-09-29） ----------------

def test_search_files_parity_query(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    args = {**CTX, "query": "ADD DEMO"}
    with TestClient(app) as c:
        mcp_out = _mcp_ok(c, "search_files", args)
        rest = c.post("/api/v1/files", json=args)
    assert rest.status_code == 200, rest.text
    assert mcp_out == rest.json()


def test_search_files_parity_ls_dir_rows_and_error(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        mcp_ls = _mcp_ok(c, "search_files", {**CTX, "path": "Command"})
        rest_ls = c.post("/api/v1/files", json={**CTX, "path": "Command"})
        mcp_err = _mcp_err(c, "search_files", {**CTX, "path": "NoSuch"})
        rest_err = c.post("/api/v1/files", json={**CTX, "path": "NoSuch"})
    assert rest_ls.status_code == 200, rest_ls.text
    assert mcp_ls == rest_ls.json()   # 目录行 obj_id/version=None 两边同构
    assert rest_err.status_code == 422
    assert mcp_err == rest_err.json()  # 错误 envelope 同构


def test_search_files_core_validation_failure_parity_and_telemetry(
        tmp_data_dir, monkeypatch):
    """schema 可接受、core 拒绝的空白 query：双通道同构且各留一条失败打点。"""
    _seed(tmp_data_dir, monkeypatch)
    args = {**CTX, "query": "  "}
    with TestClient(app) as c:
        mcp_err = _mcp_err(c, "search_files", args)
        rest = c.post("/api/v1/files", json=args)
    assert rest.status_code == 422
    assert mcp_err == rest.json()

    from app.telemetry.recorder import flush as flush_telemetry
    assert flush_telemetry()
    from app.service import get_service
    rows = get_service().db.execute(
        "SELECT caller, endpoint, result FROM telemetry "
        "WHERE endpoint IN ('mcp:search_files', '/files') AND level='tool' "
        "ORDER BY rowid"
    ).fetchall()
    assert [(r["caller"], r["endpoint"]) for r in rows] == [
        ("mcp", "mcp:search_files"), ("skill", "/files")]
    for row in rows:
        assert json.loads(row["result"])["error"]["code"] == "INVALID_ARGUMENT"


@pytest.mark.parametrize(("field", "value"), [
    ("query", "q" * 201),
    ("path", "p" * 1025),
    ("ext", "e" * 65),
    ("after", "a" * 1025),
])
def test_search_files_length_validation_error_is_identical_across_channels(
        tmp_data_dir, monkeypatch, field, value):
    _seed(tmp_data_dir, monkeypatch)
    args = {**CTX, field: value}
    with TestClient(app) as c:
        mcp_err = _mcp_err(c, "search_files", args)
        rest = c.post("/api/v1/files", json=args)
    assert rest.status_code == 422
    assert mcp_err == rest.json()
