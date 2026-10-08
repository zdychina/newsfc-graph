"""鉴权中间件测试（v2 用户体系）：KEY 反查用户 + 权限 + login 豁免 + 取消旁路 + 打点①。
每 test 加 tmp_data_dir 避免 lifespan 建真实索引。"""
import json
from fastapi.testclient import TestClient


def _seed(tmp_path, monkeypatch, users):
    # users 已迁 SQLite；seed 到 DB（_shared 由 conftest _empty_service 指向 tmp）
    from app.users import store as users_store
    for u in users:
        users_store.add_user(u)


ADMIN = {"username": "admin", "key": "gap_admin", "can_frontend": True, "can_upload": True, "can_test": True, "can_skill": True, "is_admin": True}
FE = {"username": "fe", "key": "gap_fe", "can_frontend": True}
SK = {"username": "sk", "key": "gap_sk", "can_skill": True}


def test_no_key_401(tmp_path, monkeypatch, tmp_data_dir):
    _seed(tmp_path, monkeypatch, [ADMIN])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/names").status_code == 401


def test_wrong_key_401(tmp_path, monkeypatch, tmp_data_dir):
    _seed(tmp_path, monkeypatch, [ADMIN])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/names", headers={"X-API-Key": "wrong"}).status_code == 401


def test_login_exempt_from_auth(tmp_path, monkeypatch, tmp_data_dir):
    """login 豁免鉴权：空 users 也能调 login（凭证错 → 401，但不是「未带KEY」401）。"""
    _seed(tmp_path, monkeypatch, [])
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/users/login", json={"username": "x", "key": "y"})
        assert r.status_code == 401


def test_empty_users_401_all(tmp_path, monkeypatch, tmp_data_dir):
    _seed(tmp_path, monkeypatch, [])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/names", headers={"X-API-Key": "any"}).status_code == 401


def test_skill_user_rest_denied_frontend_endpoints(tmp_path, monkeypatch, tmp_data_dir):
    """SKILL 账号（无 can_frontend）在 REST 侧仅剩 MCP 通道——前端接口 403。

    （原 POST /md、POST /domains 已删：Agent 访问统一走 MCP /mcp，
    鉴权测试见 test_mcp.py。）
    """
    _seed(tmp_path, monkeypatch, [SK])
    from app.main import app
    with TestClient(app) as c:
        h = {"X-API-Key": "gap_sk"}
        assert c.get("/api/v1/objects", headers=h).status_code == 403
        # /files（2026-09-29）：skill 权限放行 → 路由层校验空 body 拒 422 envelope
        r_files = c.post("/api/v1/files", headers=h, json={})
        assert r_files.status_code == 422
        assert r_files.json()["error"]["code"] == "INVALID_ARGUMENT"


def test_frontend_user_can_objects(tmp_path, monkeypatch, tmp_data_dir):
    _seed(tmp_path, monkeypatch, [FE])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/objects", headers={"X-API-Key": "gap_fe"}).status_code == 200


def test_admin_can_users(tmp_path, monkeypatch, tmp_data_dir):
    _seed(tmp_path, monkeypatch, [ADMIN])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/users", headers={"X-API-Key": "gap_admin"}).status_code == 200


def test_frontend_user_cannot_users(tmp_path, monkeypatch, tmp_data_dir):
    _seed(tmp_path, monkeypatch, [FE])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/users", headers={"X-API-Key": "gap_fe"}).status_code == 403


# ---------------- REST 图谱双接口 401/403 envelope（2026-09-08 三工具重构 §5.2） ----------------

def test_graph_routes_401_uses_error_envelope(tmp_path, monkeypatch, tmp_data_dir):
    """未带 KEY → 401 + {"error": {GraphError}}（其余路径保持 {"detail"}）。"""
    _seed(tmp_path, monkeypatch, [ADMIN])
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/domains", json={})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHENTICATED"
        r2 = c.post("/api/v1/md", json={})
        assert r2.status_code == 401
        assert r2.json()["error"]["code"] == "UNAUTHENTICATED"
        r_search = c.post("/api/v1/search", json={"terms": ["x"]})
        assert r_search.status_code == 401
        assert r_search.json()["error"]["code"] == "UNAUTHENTICATED"
        # 非图谱路径不受影响（前端 api.ts 兼容）
        r3 = c.get("/api/v1/names")
        assert r3.status_code == 401
        assert "detail" in r3.json()


def test_graph_routes_403_uses_error_envelope(tmp_path, monkeypatch, tmp_data_dir):
    """无 skill 权限 → 403 envelope；can_skill/can_frontend 放行到路由层。"""
    _seed(tmp_path, monkeypatch, [
        {"username": "am", "key": "gap_am", "can_assets": True},  # 无 skill/frontend
        SK,
    ])
    from app.main import app
    with TestClient(app) as c:
        r = c.post("/api/v1/md", headers={"X-API-Key": "gap_am"}, json={})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "PERMISSION_DENIED"
        # can_skill → 放行到路由层（空 body → 业务 422 INVALID_ARGUMENT envelope）
        r2 = c.post("/api/v1/md", headers={"X-API-Key": "gap_sk"}, json={})
        assert r2.status_code == 422
        assert r2.json()["error"]["code"] == "INVALID_ARGUMENT"
        r_search = c.post("/api/v1/search", headers={"X-API-Key": "gap_sk"}, json={})
        assert r_search.status_code == 422
        assert r_search.json()["error"]["code"] == "INVALID_ARGUMENT"
        # /files（2026-09-29）同款：无 skill 权限 → 403 envelope；can_skill 放行 → 422
        r_files_403 = c.post("/api/v1/files", headers={"X-API-Key": "gap_am"}, json={})
        assert r_files_403.status_code == 403
        assert r_files_403.json()["error"]["code"] == "PERMISSION_DENIED"
        r_files = c.post("/api/v1/files", headers={"X-API-Key": "gap_sk"}, json={})
        assert r_files.status_code == 422
        assert r_files.json()["error"]["code"] == "INVALID_ARGUMENT"


# ---------------- assets / upload / test 权限 ----------------

def test_frontend_without_assets_denied_fs(tmp_path, monkeypatch, tmp_data_dir):
    """can_frontend（无 can_assets）→ /fs/* 全 403（新模型：/fs 专属 can_assets）。"""
    _seed(tmp_path, monkeypatch, [FE])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/fs/children", headers={"X-API-Key": "gap_fe"}).status_code == 403
        assert c.post("/api/v1/fs/mkdir", json={"path": "x"},
                      headers={"X-API-Key": "gap_fe"}).status_code == 403


def test_assets_user_can_fs_write(tmp_path, monkeypatch, tmp_data_dir):
    """can_assets → /fs 读写通过；can_frontend+can_upload 但无 can_assets → 403。"""
    _seed(tmp_path, monkeypatch, [
        {"username": "am", "key": "gap_am", "can_assets": True},
        {"username": "up", "key": "gap_up", "can_frontend": True, "can_upload": True},
    ])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/fs/children", headers={"X-API-Key": "gap_am"}).status_code == 200
        assert c.post("/api/v1/fs/mkdir", json={"path": "x"},
                      headers={"X-API-Key": "gap_am"}).status_code != 403
        # upload 权限不再授予 /fs
        assert c.post("/api/v1/fs/mkdir", json={"path": "y"},
                      headers={"X-API-Key": "gap_up"}).status_code == 403


def test_skill_user_cannot_write_assets_nor_tests(tmp_path, monkeypatch, tmp_data_dir):
    """can_skill 用户 → /fs/mkdir 403（中间件 assets 拦截）、/tests 403（需 test）。"""
    _seed(tmp_path, monkeypatch, [SK])
    from app.main import app
    with TestClient(app) as c:
        h = {"X-API-Key": "gap_sk"}
        assert c.post("/api/v1/fs/mkdir", json={"path": "x"}, headers=h).status_code == 403
        assert c.get("/api/v1/tests/cases", headers=h).status_code == 403


# REST /domains、/md 的工号/会话归因由 JSON body 同名字段提供；契约与打点测试见
# test_skill_compat.py。鉴权中间件只负责 X-API-Key 与权限。


def test_stats_view_endpoints_frontend_perm(tmp_path, monkeypatch, tmp_data_dir):
    """三视图统计端点（/api/v1/stats/*，2026-09-01）：frontend 权限——
    无 KEY 401 / 纯 skill 403 / can_frontend 200（含导出，不走 /export 的 upload 分支）；
    旧 GET /api/v1/stats 同级权限不受新路由影响。"""
    _seed(tmp_path, monkeypatch, [FE, SK])
    from app.main import app
    with TestClient(app) as c:
        assert c.get("/api/v1/stats/command").status_code == 401
        assert c.get("/api/v1/stats/command",
                     headers={"X-API-Key": "gap_sk"}).status_code == 403
        for url in ("/api/v1/stats/command/summary", "/api/v1/stats/command/knowledge",
                    "/api/v1/stats/command/rules", "/api/v1/stats/feature/summary",
                    "/api/v1/stats/feature/matrix", "/api/v1/stats/business/overview",
                    "/api/v1/stats/mop", "/api/v1/stats/filters",
                    "/api/v1/stats/export?view=command&format=csv",
                    "/api/v1/stats"):
            assert c.get(url, headers={"X-API-Key": "gap_fe"}).status_code == 200, url
