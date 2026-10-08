"""SKILL 兼容双接口（2026-09-03 恢复，与 MCP 并行）——契约回归。

语义逐行对齐 e4922b4 删除前的旧测试：/domains 返回全部业务域最新 md；
/md 批量取 md（不传 version→最新现存；版本缺失→该 id 计错回带可用版本，
不影响其余 id）。权限=skill（与 MCP 一致：can_skill ∨ can_frontend，admin 全权）。
两个 POST 的 JSON body 均必传 AGENT_USERNAME / AGENT_SESSION_ID，并在 tool/object
打点中分别落 operator / session_id。
"""
import json

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
)

client = TestClient(app)
ATTRIBUTION = {
    "AGENT_USERNAME": "00234567",
    "AGENT_SESSION_ID": "session-rest-1",
}


def _seed(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch,
           {"cmd.md": CMD_EDGES, "v2.md": CMD_V2, "cfg.md": CFG, "biz.md": BIZ})


def test_domains_returns_all_business_domain_md(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    r = client.post("/api/v1/domains", json=ATTRIBUTION)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    d = body[0]
    assert d["id"] == "alpha@BusinessDomain@demo"
    assert "业务感知" in d["md"]
    assert "name" in d  # 旧契约 frontmatter.get("name")，无 name 字段时为 None


def test_md_batch_latest_version(tmp_data_dir, monkeypatch):
    """不传 version → 各 id 最新现存版本 + 原始 md。"""
    _seed(tmp_data_dir, monkeypatch)
    r = client.post("/api/v1/md", json={
        **ATTRIBUTION,
        "ids": ["alpha@MMLCommand@ADD DEMO", "alpha@ConfigObject@DEMO_OBJ"]})
    assert r.status_code == 200, r.text
    body = r.json()
    # CMD 有 20.15.2+20.16.0，不传 version → 最新 20.16.0
    cmd = body["alpha@MMLCommand@ADD DEMO"]
    assert cmd["version"] == "20.16.0"
    assert "ADD DEMO" in cmd["md"]
    assert "DEMO_OBJ" in body["alpha@ConfigObject@DEMO_OBJ"]["md"]


def test_md_version_missing_and_nonexistent(tmp_data_dir, monkeypatch):
    """版本缺失→该 id 计错回带 available_versions；不存在 id→对象不存在；互不影响。"""
    _seed(tmp_data_dir, monkeypatch)
    r = client.post("/api/v1/md", json={
        **ATTRIBUTION,
        "ids": ["alpha@MMLCommand@ADD DEMO", "nope@MMLCommand@X"],
        "version": "20.15.2"})
    body = r.json()
    ok = body["alpha@MMLCommand@ADD DEMO"]
    assert ok["version"] == "20.15.2" and "ADD DEMO" in ok["md"]
    miss = body["nope@MMLCommand@X"]
    assert miss["error"] == "对象不存在" and miss["available_versions"] == []
    # 版本不存在场景
    r2 = client.post("/api/v1/md", json={
        **ATTRIBUTION,
        "ids": ["alpha@MMLCommand@ADD DEMO"], "version": "19.0.0"})
    m = r2.json()["alpha@MMLCommand@ADD DEMO"]
    assert m["error"].startswith("版本不存在")
    assert set(m["available_versions"]) == {"20.15.2", "20.16.0"}


def test_md_dedup_same_id(tmp_data_dir, monkeypatch):
    """同 id 重复请求只算一次条目（dict.fromkeys 去重保序）。"""
    _seed(tmp_data_dir, monkeypatch)
    r = client.post("/api/v1/md", json={
        **ATTRIBUTION,
        "ids": ["alpha@ConfigObject@DEMO_OBJ", "alpha@ConfigObject@DEMO_OBJ"]})
    assert list(r.json().keys()) == ["alpha@ConfigObject@DEMO_OBJ"]


def test_skill_perm_same_as_mcp(tmp_data_dir, monkeypatch):
    """权限=skill：can_skill ∨ can_frontend 放行（admin 全权）；两者皆无 → 403。"""
    _seed(tmp_data_dir, monkeypatch)
    from app.middleware import auth as auth_mod

    def _as(user):
        monkeypatch.setattr(auth_mod, "authenticate", lambda key: user)
        return (client.post("/api/v1/domains", json=ATTRIBUTION).status_code,
                client.post("/api/v1/md", json={
                    **ATTRIBUTION,
                    "ids": ["alpha@ConfigObject@DEMO_OBJ"],
                }).status_code)

    assert _as({"username": "sk", "can_skill": True}) == (200, 200)
    assert _as({"username": "fe", "can_frontend": True}) == (200, 200)
    assert _as({"username": "none", "can_assets": True}) == (403, 403)


def test_attribution_fields_are_required_in_json_body(tmp_data_dir, monkeypatch):
    """REST 与 MCP 同契约：工号和会话是必填同名字段，不从旧 header 降级。"""
    _seed(tmp_data_dir, monkeypatch)
    legacy_headers = {
        "X-User-Id": ATTRIBUTION["AGENT_USERNAME"],
        "X-Session-Id": ATTRIBUTION["AGENT_SESSION_ID"],
    }

    incomplete_attribution = [
        {},
        {"AGENT_USERNAME": ATTRIBUTION["AGENT_USERNAME"]},
        {"AGENT_SESSION_ID": ATTRIBUTION["AGENT_SESSION_ID"]},
    ]
    domains = [
        client.post("/api/v1/domains", headers=legacy_headers, json=body)
        for body in incomplete_attribution
    ]
    md = [
        client.post("/api/v1/md", headers=legacy_headers, json={
            **body,
            "ids": ["alpha@ConfigObject@DEMO_OBJ"],
        })
        for body in incomplete_attribution
    ]

    assert all(response.status_code == 422 for response in domains)
    assert all(response.status_code == 422 for response in md)


def test_attribution_fields_reject_invalid_values(tmp_data_dir, monkeypatch):
    """两条通道共用的归因约束：拒绝空白和超长值。"""
    _seed(tmp_data_dir, monkeypatch)
    invalid_attribution = [
        {"AGENT_USERNAME": "", "AGENT_SESSION_ID": "session-rest-1"},
        {"AGENT_USERNAME": "   ", "AGENT_SESSION_ID": "session-rest-1"},
        {"AGENT_USERNAME": "x" * 65, "AGENT_SESSION_ID": "session-rest-1"},
        {"AGENT_USERNAME": "00234567", "AGENT_SESSION_ID": ""},
        {"AGENT_USERNAME": "00234567", "AGENT_SESSION_ID": "   "},
        {"AGENT_USERNAME": "00234567", "AGENT_SESSION_ID": "s" * 129},
    ]

    for attribution in invalid_attribution:
        domains = client.post("/api/v1/domains", json=attribution)
        md = client.post("/api/v1/md", json={
            **attribution,
            "ids": ["alpha@ConfigObject@DEMO_OBJ"],
        })
        assert domains.status_code == 422
        assert md.status_code == 422


def test_rest_attribution_matches_mcp_telemetry(tmp_data_dir, monkeypatch):
    """REST 的调用级与对象级行都记录工号/会话，通道恒为 skill。"""
    _seed(tmp_data_dir, monkeypatch)

    padded_attribution = {
        "AGENT_USERNAME": f'  {ATTRIBUTION["AGENT_USERNAME"]}  ',
        "AGENT_SESSION_ID": f'  {ATTRIBUTION["AGENT_SESSION_ID"]}  ',
    }
    domains = client.post("/api/v1/domains", json=padded_attribution)
    md = client.post("/api/v1/md", json={
        **padded_attribution,
        "ids": ["alpha@ConfigObject@DEMO_OBJ"],
    })
    assert domains.status_code == 200
    assert md.status_code == 200

    from app.service import get_service
    from app.telemetry.recorder import flush

    assert flush()
    db = get_service().db
    rows = [dict(row) for row in db.execute(
        "SELECT endpoint, level, caller, user, operator, session_id, params "
        "FROM telemetry WHERE endpoint IN ('/domains', '/md') ORDER BY rowid"
    ).fetchall()]

    assert len(rows) == 4
    assert {(row["endpoint"], row["level"]) for row in rows} == {
        ("/domains", "tool"),
        ("/domains", "object"),
        ("/md", "tool"),
        ("/md", "object"),
    }
    assert all(row["caller"] == "skill" for row in rows)
    assert all(row["user"] == "admin" for row in rows)
    assert all(row["operator"] == ATTRIBUTION["AGENT_USERNAME"] for row in rows)
    assert all(row["session_id"] == ATTRIBUTION["AGENT_SESSION_ID"] for row in rows)
    tool_params = {
        row["endpoint"]: json.loads(row["params"])
        for row in rows if row["level"] == "tool"
    }
    assert tool_params["/domains"] == {}
    assert tool_params["/md"] == {
        "ids": ["alpha@ConfigObject@DEMO_OBJ"],
        "version": None,
    }

    from app.repos.telemetry_repo import aggregate_stats

    stats = aggregate_stats(db)
    assert stats["total"] == 2
    assert stats["by_operator"] == {ATTRIBUTION["AGENT_USERNAME"]: 2}
    assert stats["by_session"] == 1


# ---------------- /files（2026-09-29，与 MCP search_files 同构） ----------------

def test_files_query_with_obj_link(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    r = client.post("/api/v1/files", json={**ATTRIBUTION, "query": "ADD DEMO"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["total"] == 2  # 20.15.2 + 20.16.0 两份文件
    by_ver = {f["version"]: f for f in out["files"]}
    assert by_ver["20.15.2"]["obj_id"] == "alpha@MMLCommand@ADD DEMO"


def test_files_ls_and_validation_errors(tmp_data_dir, monkeypatch):
    _seed(tmp_data_dir, monkeypatch)
    r = client.post("/api/v1/files", json={**ATTRIBUTION, "path": "Command"})
    assert r.status_code == 200, r.text
    assert [f["name"] for f in r.json()["files"]] == ["alpha"]  # ls：目录行
    r2 = client.post("/api/v1/files", json=ATTRIBUTION)  # 无过滤
    assert r2.status_code == 422
    assert r2.json()["error"]["code"] == "INVALID_ARGUMENT"
    r3 = client.post("/api/v1/files",
                     json={**ATTRIBUTION, "path": "NoSuchDir"})
    assert r3.status_code == 422
    assert r3.json()["error"]["code"] == "INVALID_FILTER"
