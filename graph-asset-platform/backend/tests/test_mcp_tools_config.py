"""MCP 工具配置测试（admin GET/PATCH /mcp-tools + 动态生效）。

v13（三工具重构 M3，2026-09-08）新语义：
- visibility 三态（visible/hidden/disabled）；legacy 三工具默认 hidden；
- supplemental_description 只追加不覆盖 canonical（§9.2）；
- 总体说明=补充（canonical 在代码）；旧 PATCH enabled 映射兼容（§11.1）。
"""
import io
import zipfile

import app.db as dbmod
import app.service as svc
from app.registry import Registry
from app.store import Store

ACC = {"Accept": "application/json, text/event-stream"}

CMD = """---
id: UDG@MMLCommand@ADD URR
type: MMLCommand
name: ADD URR
version: 20.15.2
---

在线计费的使用量上报规则配置命令。
"""

PUBLIC_TOOLS = {"get_domains", "get_md", "search_graph", "search_files"}
LEGACY_TOOLS = {"search_objects", "search_md", "get_object"}
ALL_TOOLS = PUBLIC_TOOLS | LEGACY_TOOLS


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
    add_user({"username": "web1", "key": "gap_web", "can_frontend": True})
    return s


def _client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _get_cfg(c, key="gap_admin"):
    return c.get("/api/v1/mcp-tools", headers={"X-API-Key": key})


def _patch_cfg(c, body, key="gap_admin"):
    return c.patch("/api/v1/mcp-tools", headers={"X-API-Key": key}, json=body)


def _tools_list(c, key="gap_admin"):
    r = c.post("/mcp", headers={"X-API-Key": key, **ACC},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 200, r.text
    return r.json()["result"]["tools"]


def _init(c, key="gap_admin"):
    r = c.post("/mcp", headers={"X-API-Key": key, **ACC},
               json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                                "clientInfo": {"name": "t", "version": "0"}}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def _call(c, name, arguments, sid=1):
    r = c.post("/mcp", headers={"X-API-Key": "gap_admin", **ACC},
               json={"jsonrpc": "2.0", "id": sid, "method": "tools/call",
                     "params": {"name": name, "arguments": arguments}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


_CTX = {"AGENT_USERNAME": "00234567", "AGENT_SESSION_ID": "sess-1"}


# ---------------- 权限 ----------------

def test_get_config_permissions(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        assert _get_cfg(c, key="").status_code == 401
        assert _get_cfg(c, key="gap_assets_only").status_code == 403
        r = _get_cfg(c, key="gap_web")
        assert r.status_code == 403
        assert "admin" in r.json()["detail"]
        # admin → 200：全量视图（公开 visible + legacy hidden + 兼容字段）
        r = _get_cfg(c)
        assert r.status_code == 200
        body = r.json()
        by = {t["name"]: t for t in body["tools"]}
        assert set(by) == ALL_TOOLS
        assert all(by[n]["visibility"] == "visible" for n in PUBLIC_TOOLS)
        assert all(by[n]["visibility"] == "hidden" for n in LEGACY_TOOLS)
        # 兼容字段：enabled = visibility != 'disabled'
        assert all(by[n]["enabled"] for n in ALL_TOOLS)
        assert all(t["supplemental_description"] == "" for t in body["tools"])
        assert all(t["default_description"] for t in body["tools"])
        assert body["instructions"] == ""
        assert "配置知识图谱" in body["default_instructions"]
        assert "三层电信图谱" not in body["default_instructions"]
        assert "决策树" in body["default_instructions"]


# ---------------- 三态：hidden 不展示可直调 / disabled 拦截 ----------------

def test_visibility_three_states(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD})
    with _client() as c:
        # hidden：不展示 + 可直调（旧客户端兼容）
        r = _patch_cfg(c, {"tools": [{"name": "search_md",
                                      "visibility": "hidden"}]})
        assert r.status_code == 200
        assert {t["name"] for t in _tools_list(c)} == PUBLIC_TOOLS
        res = _call(c, "search_md", {**_CTX, "q": "计费"})
        assert res["isError"] is False
        # disabled：不展示 + TOOL_DISABLED envelope
        _patch_cfg(c, {"tools": [{"name": "search_md",
                                  "visibility": "disabled"}]})
        assert {t["name"] for t in _tools_list(c)} == PUBLIC_TOOLS
        res2 = _call(c, "search_md", {**_CTX, "q": "计费"})
        assert res2["isError"] is True
        body = __import__("json").loads(res2["content"][0]["text"])
        assert body["error"]["code"] == "TOOL_DISABLED"
        assert "已被管理员禁用" in body["error"]["message"]
        # 公开工具也可 disabled
        _patch_cfg(c, {"tools": [{"name": "get_md", "visibility": "disabled"}]})
        names = {t["name"] for t in _tools_list(c)}
        assert names == PUBLIC_TOOLS - {"get_md"}
        # 恢复
        _patch_cfg(c, {"tools": [{"name": "get_md", "visibility": "visible"},
                                 {"name": "search_md", "visibility": "hidden"}]})
        assert {t["name"] for t in _tools_list(c)} == PUBLIC_TOOLS


# ---------------- 旧 PATCH enabled 兼容（§11.1） ----------------

def test_legacy_patch_enabled_mapping(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD})
    with _client() as c:
        # 旧 PATCH：公开工具 enabled=false → disabled
        _patch_cfg(c, {"tools": [{"name": "get_md", "enabled": False}]})
        assert "get_md" not in {t["name"] for t in _tools_list(c)}
        # 旧 PATCH：legacy enabled=false → disabled；=true 只到 hidden
        _patch_cfg(c, {"tools": [{"name": "search_md", "enabled": False}]})
        res = _call(c, "search_md", {**_CTX, "q": "计费"})
        assert res["isError"] is True
        _patch_cfg(c, {"tools": [{"name": "search_md", "enabled": True}]})
        assert "search_md" not in {t["name"] for t in _tools_list(c)}  # hidden，不回 visible
        res2 = _call(c, "search_md", {**_CTX, "q": "计费"})
        assert res2["isError"] is False
        # GET 兼容 enabled 字段
        by = {t["name"]: t for t in _get_cfg(c).json()["tools"]}
        assert by["search_md"]["enabled"] is True
        assert by["search_md"]["visibility"] == "hidden"
        assert by["get_md"]["visibility"] == "disabled"


# ---------------- 补充说明：只追加不覆盖 canonical（§9.2） ----------------

def test_supplement_appends_to_canonical(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"Command/UDG/20.15.2/a.md": CMD})
    with _client() as c:
        _patch_cfg(c, {"tools": [{"name": "get_md",
                                  "supplemental_description": "定制补充：专用于计费场景"}]})
        t = next(t for t in _tools_list(c) if t["name"] == "get_md")
        assert "批量获取" in t["description"]  # canonical 保留
        assert "定制补充：专用于计费场景" in t["description"]  # 补充在后面
        assert "[管理员补充]" in t["description"]
        # schema 不受管理员配置影响
        assert t["inputSchema"]["properties"]["ids"]["maxItems"] == 100
        # 清空 → 纯 canonical
        _patch_cfg(c, {"tools": [{"name": "get_md",
                                  "supplemental_description": ""}]})
        t2 = next(t for t in _tools_list(c) if t["name"] == "get_md")
        assert "定制补充" not in t2["description"]
        assert "批量获取" in t2["description"]


# ---------------- 总体说明：补充语义（§9.3） ----------------

def test_instructions_supplement_semantics(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        base = _init(c)["instructions"]
        assert "决策树" in base  # canonical 短决策树
        _patch_cfg(c, {"instructions": "定制总体说明ABC"})
        eff = _init(c)["instructions"]
        assert "定制总体说明ABC" in eff
        assert "决策树" in eff  # canonical 不被覆盖
        # 清空 → 纯 canonical
        _patch_cfg(c, {"instructions": ""})
        assert "定制总体说明ABC" not in _init(c)["instructions"]
        instructions = _init(c)["instructions"]
        assert "配置知识图谱" in instructions
        assert "三层电信图谱" not in instructions


# ---------------- canonical 文案契约（上线审查 2026-10-08） ----------------

def test_default_instructions_scope_latest_version_to_graph_objects(
        tmp_data_dir, monkeypatch):
    """“最新版本”只适用于图谱对象，不能让 Agent 误以为物理文件也被折叠。"""
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        instructions = _init(c)["instructions"]

    assert "所有搜索与读取默认作用于" not in instructions
    assert "图谱对象" in instructions
    assert "最新现存版本" in instructions
    assert "search_files" in instructions
    assert "磁盘" in instructions or "具体版本文件" in instructions


def test_search_graph_description_does_not_redirect_bounded_body_results_to_files(
        tmp_data_dir, monkeypatch):
    """search_files 不搜正文，不能被描述成正文候选池截断后的“全量”出口。"""
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        by_name = {tool["name"]: tool for tool in _tools_list(c)}
    description = by_name["search_graph"]["description"]

    assert "全量获取场景请用 search_files" not in description
    assert "候选" in description
    assert "不保证找全" in description or "截断" in description


def test_legacy_default_descriptions_mark_deprecation_and_replacement(
        tmp_data_dir, monkeypatch):
    """旧工具即使被管理员重新设为 visible，也必须先告诉 Agent 替代工具。"""
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        body = _get_cfg(c).json()
    descriptions = {tool["name"]: tool["default_description"]
                    for tool in body["tools"]}

    replacements = {
        "search_objects": "search_graph",
        "search_md": "search_graph",
        "get_object": "get_md",
    }
    for legacy, replacement in replacements.items():
        description = descriptions[legacy]
        assert "deprecated" in description.lower() or "已废弃" in description
        assert replacement in description


def test_search_files_description_states_normalized_limit_and_empty_path_rule(
        tmp_data_dir, monkeypatch):
    """把实现边界写进 Agent 可见契约：规范化后 80 字，空 path 不是根目录。"""
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        search_files = next(
            tool for tool in _tools_list(c) if tool["name"] == "search_files")

    query_description = search_files["inputSchema"]["properties"]["query"]["description"]
    combined = search_files["description"] + "\n" + query_description
    assert "规范化后" in combined
    assert "80" in combined
    assert "空 path" in combined or "path=''" in combined or 'path=""' in combined
    assert "根目录" in combined


def test_instructions_survive_restart(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        _patch_cfg(c, {"instructions": "重启后仍生效的说明"})
        _patch_cfg(c, {"tools": [{"name": "search_objects",
                                  "visibility": "disabled"}]})
    with _client() as c:
        assert "重启后仍生效的说明" in _init(c)["instructions"]
        assert "search_objects" not in {t["name"] for t in _tools_list(c)}


def test_legacy_instructions_backed_up(tmp_data_dir, monkeypatch):
    """v13 迁移语义：active instructions 清空（旧全文覆盖不再生效）+ 哨兵已置
    （重复 init_schema 不重放迁移，管理员后续补充不被覆盖）。"""
    s = _setup(tmp_data_dir, monkeypatch)
    from app.repos import mcp_tools_repo
    assert mcp_tools_repo.get_instructions(s.db) == ""
    assert s.db.execute(
        "SELECT value FROM meta WHERE key='mcp_visibility_migrated'"
    ).fetchone() is not None
    # 管理员配置新补充后，重复 init_schema 不清掉
    with __import__("app.service", fromlist=["import_lock"]).import_lock:
        mcp_tools_repo.set_instructions(s.db, "管理员补充XYZ")
        s.db.commit()
    dbmod.init_schema(s.db)  # 幂等重放
    assert mcp_tools_repo.get_instructions(s.db) == "管理员补充XYZ"


# ---------------- 入参校验 ----------------

def test_whitespace_only_values_reset_to_default(tmp_data_dir, monkeypatch):
    """纯空白描述/说明去空白后视同清空 → 回默认。"""
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        r = _patch_cfg(c, {"tools": [{"name": "get_md",
                                      "supplemental_description": "   "}],
                           "instructions": "   "})
        assert r.status_code == 200
        by = {t["name"]: t for t in r.json()["tools"]}
        assert by["get_md"]["supplemental_description"] == ""
        t = next(t for t in _tools_list(c) if t["name"] == "get_md")
        assert "批量获取" in t["description"]
        assert "决策树" in _init(c)["instructions"]


def test_patch_unknown_tool_rejected(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    with _client() as c:
        r = _patch_cfg(c, {"tools": [{"name": "no_such_tool", "enabled": True}]})
        assert r.status_code == 400
        assert "no_such_tool" in r.json()["detail"]
        assert {t["name"] for t in _tools_list(c)} == PUBLIC_TOOLS


# ---------------- 批量任务持锁期间：限时失败而非无限等 ----------------

def test_patch_timeout_409_when_lock_held(tmp_data_dir, monkeypatch):
    import time as _time
    import app.routers.mcp_tools as rt
    from app.service import import_lock
    monkeypatch.setattr(rt, "_LOCK_WAIT_SECONDS", 0.3)
    _setup(tmp_data_dir, monkeypatch)
    import threading
    acquired = import_lock.acquire()
    assert acquired
    try:
        with _client() as c:
            t0 = _time.time()
            r = _patch_cfg(c, {"instructions": "不应保存"})
            dt = _time.time() - t0
            assert r.status_code == 409
            assert "批量任务" in r.json()["detail"]
            assert dt < 5
        from app.repos import mcp_tools_repo
        import app.db as dbmod
        assert mcp_tools_repo.get_instructions(dbmod.get_shared_db()) == ""
    finally:
        import_lock.release()
    with _client() as c:
        assert _patch_cfg(c, {"instructions": "锁释放后OK"}).status_code == 200
