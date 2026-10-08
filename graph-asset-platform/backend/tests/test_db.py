"""DB 持久化层测试：migrate 正确性 + load_from_db 一致 + 增量 UPSERT + mtime 同步 +
version 应用层排序（验证 SQLite 改造的核心正确性）。

复用 _setup 模式（tmp store + tmp DB + load_from_db 空）。
"""
import json
import os
import time

from app.index import Index
from app.registry import Registry
from app.store import Store
import app.service as svc

CMD = (
    "---\n"
    "id: alpha@MMLCommand@ADD DEMO\n"
    "type: MMLCommand\n"
    "nf: alpha\n"
    "version: 20.15.2\n"
    "name: DEMO\n"
    "---\n"
    "# ADD DEMO\n"
)


def _setup(tmp_data_dir, monkeypatch):
    import app.db as dbmod
    s = svc.Service.__new__(svc.Service)
    s.store = Store(tmp_data_dir)
    s.registry = Registry.load_default()
    s.db = dbmod.get_db(tmp_data_dir.parent / "test.db")
    dbmod.init_schema(s.db)
    monkeypatch.setattr(dbmod, "_shared", s.db, raising=False)
    s.index = Index.load_from_db(s.db, s.registry)
    monkeypatch.setattr(svc, "_service", s)
    return s


def test_build_and_load_roundtrip(tmp_data_dir, monkeypatch):
    """build_index_db → load_from_db：versions 聚合 + latest（应用层排序）。"""
    from app.migrate import build_index_db
    s = _setup(tmp_data_dir, monkeypatch)
    s.store.write("Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md", CMD)
    s.store.write("Command/alpha/20.16.0/alpha@MMLCommand@ADD DEMO.md",
                  CMD.replace("20.15.2", "20.16.0"))
    stats = build_index_db(s.db, s.store, s.registry)
    assert stats["objects"] == 2
    idx = Index.load_from_db(s.db, s.registry)
    assert idx.versions_of("alpha@MMLCommand@ADD DEMO") == ["20.15.2", "20.16.0"]
    assert idx.latest_version_of_id("alpha@MMLCommand@ADD DEMO") == "20.16.0"


def test_reindex_path_incremental(tmp_data_dir, monkeypatch):
    """store.write + reindex_path → DB+内存同步（无需全量 rebuild）。"""
    from app.repos import objects_repo
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.reindex_path(p)
    assert objects_repo.get_mtime(s.db, p) is not None
    s.reload_index()
    assert s.index.node("alpha@MMLCommand@ADD DEMO", "20.15.2") is not None


def test_unindex_path(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.reindex_path(p)
    s.store.delete(p)
    s.unindex_path(p)
    s.reload_index()
    assert s.index.node("alpha@MMLCommand@ADD DEMO", "20.15.2") is None


def test_mtime_sync_detects_external_change(tmp_data_dir, monkeypatch):
    """_sync_mtime：build 后外部改 md（version 变 + mtime 推进）→ 启动检测重 parse。"""
    from app.migrate import build_index_db
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    build_index_db(s.db, s.store, s.registry)
    # 外部改 md（version 变）+ 显式推进 mtime（避免同秒不触发）
    s.store.write(p, CMD.replace("20.15.2", "20.99.0"))
    abspath = s.store.abspath(p)
    os.utime(abspath, (time.time() + 10, time.time() + 10))
    s._sync_mtime()
    s.reload_index()
    assert s.index.node("alpha@MMLCommand@ADD DEMO", "20.99.0") is not None
    assert s.index.node("alpha@MMLCommand@ADD DEMO", "20.15.2") is None  # 旧版本被替换


def test_migrate_users(tmp_data_dir, monkeypatch):
    """users.json → users 表。"""
    s = _setup(tmp_data_dir, monkeypatch)
    users_file = tmp_data_dir.parent / "users.json"
    monkeypatch.setattr("app.config.USERS_FILE", users_file)
    users_file.write_text(json.dumps({"users": [
        {"username": "admin", "key": "k1", "is_admin": True},
    ]}), encoding="utf-8")
    from app.migrate import migrate_users
    assert migrate_users(s.db) == 1
    from app.repos import users_repo
    assert users_repo.find_by_name(s.db, "admin") is not None


def test_migrate_telemetry(tmp_data_dir, monkeypatch):
    """jsonl → telemetry 表（保留 level）。"""
    s = _setup(tmp_data_dir, monkeypatch)
    obj_f = tmp_data_dir.parent / "objects.jsonl"
    req_f = tmp_data_dir.parent / "requests.jsonl"
    monkeypatch.setattr("app.config.TELEMETRY_OBJECTS_FILE", obj_f)
    monkeypatch.setattr("app.config.TELEMETRY_REQUESTS_FILE", req_f)
    obj_f.write_text(json.dumps({"ts": "2026-08-11T10:00:00+00:00", "level": "object",
                                 "caller": "skill", "endpoint": "/md", "id": "X@1",
                                 "type": "Feature", "user": "sk"}) + "\n", encoding="utf-8")
    req_f.write_text(json.dumps({"ts": "2026-08-11T10:00:00+00:00", "level": "request",
                                 "caller": "web", "endpoint": "/api/v1/stats", "user": "fe"}) + "\n",
                     encoding="utf-8")
    from app.migrate import migrate_telemetry
    assert migrate_telemetry(s.db) == 2
    assert s.db.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == 2


def test_version_application_sort(tmp_data_dir, monkeypatch):
    """DB version 字符串 → load_from_db → 应用层 _ver_key 排序（非 SQL 字符串序）。

    20.2.0 vs 20.10.0：SQL 字符串序 "20.2.0" > "20.10.0"（'2'>'1'）；
    应用层 _ver_key 段 2 比 2<10 → 20.10.0 最新。
    """
    from app.migrate import build_index_db
    s = _setup(tmp_data_dir, monkeypatch)
    for v in ["20.2.0", "20.10.0"]:
        s.store.write(f"Command/alpha/{v}/alpha@MMLCommand@ADD DEMO.md",
                      CMD.replace("20.15.2", v))
    build_index_db(s.db, s.store, s.registry)
    idx = Index.load_from_db(s.db, s.registry)
    assert idx.latest_version_of_id("alpha@MMLCommand@ADD DEMO") == "20.10.0"


def test_files_rare_ext_cursor_query_uses_composite_index(tmp_data_dir, monkeypatch):
    """稀有扩展名 + path 游标应一次走 (ext, path)，不能先扫全 path。"""
    s = _setup(tmp_data_dir, monkeypatch)
    indexes = {r[1] for r in s.db.execute("PRAGMA index_list(files)").fetchall()}
    assert "idx_files_ext_path" in indexes

    plan = s.db.execute(
        "EXPLAIN QUERY PLAN SELECT path FROM files "
        "WHERE ext=? AND path>? ORDER BY path LIMIT ?",
        ("drawio", "Feature/", 100),
    ).fetchall()
    detail = " ".join(r[3].lower() for r in plan)
    assert "idx_files_ext_path" in detail
    assert "ext=? and path>?" in detail
