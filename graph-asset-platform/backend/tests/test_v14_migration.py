"""v13 -> v14 文件户口册迁移的真实启动回归。"""

from pathlib import Path

import app.db as dbmod
import app.service as svc


_OLD_ID = "UDG@MMLCommand@ADD URR"
_OLD_REL = "Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md"
_OLD_MD = """---
id: UDG@MMLCommand@ADD URR
type: MMLCommand
name: ADD URR
version: 20.15.2
---

# ADD URR

存量 v13 对象正文。
"""


class _ImmediateThread:
    """让 Service 启动任务在测试中确定性执行，不等待真实后台线程。"""

    def __init__(self, *, target, daemon=False, **_kwargs):
        self._target = target
        self.daemon = daemon

    def start(self):
        self._target()


def _make_v13_snapshot(db_path: Path) -> None:
    """用现行 DDL 造基线，再移除仅 v14 才有的表，模拟真实升级前状态。"""
    conn = dbmod.get_db(db_path)
    dbmod.init_schema(conn)
    conn.execute(
        "INSERT INTO objects(id, version, type, layer, scope, nf, domain, scenario, "
        "source_path, name, frontmatter_json, body_md, raw_md, mtime) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (_OLD_ID, "20.15.2", "MMLCommand", "命令层", "nf", "UDG", "", "",
         _OLD_REL, "ADD URR", "{}", "存量 v13 对象正文。", _OLD_MD, 123.0),
    )
    for table in ("files_fts_map", "files_fts", "files"):
        conn.execute(f"DROP TABLE {table}")
    conn.execute("DELETE FROM meta WHERE key='files_bootstrapped'")
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', '13')")
    conn.commit()
    conn.close()


def test_v13_database_startup_migrates_and_bootstraps_file_catalog(
        tmp_data_dir, monkeypatch):
    """一键部署后的首次启动保留旧对象，并自动建表、建册、写完成标记。"""
    legacy_db = tmp_data_dir.parent / "legacy-v13.db"
    _make_v13_snapshot(legacy_db)

    asset = tmp_data_dir / Path(*_OLD_REL.split("/"))
    asset.parent.mkdir(parents=True, exist_ok=True)
    asset.write_text(_OLD_MD, encoding="utf-8")

    monkeypatch.setattr(dbmod, "DB_PATH", legacy_db)
    monkeypatch.setattr(dbmod, "_shared", None)
    monkeypatch.setattr(svc, "ASSETS_DIR", tmp_data_dir)
    monkeypatch.setattr(svc.Service, "_fts_reconcile_async", lambda self: None)
    monkeypatch.setattr(svc.Service, "_sync_mtime_async", lambda self: None)
    monkeypatch.setattr(svc.threading, "Thread", _ImmediateThread)

    service = svc.Service()

    old = service.db.execute(
        "SELECT id, version, source_path, raw_md FROM objects WHERE id=?",
        (_OLD_ID,),
    ).fetchone()
    assert dict(old) == {
        "id": _OLD_ID,
        "version": "20.15.2",
        "source_path": _OLD_REL,
        "raw_md": _OLD_MD,
    }

    tables = {row[0] for row in service.db.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"files", "files_fts", "files_fts_map"} <= tables
    assert service.db.execute(
        "SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "14"
    assert service.db.execute(
        "SELECT value FROM meta WHERE key='files_bootstrapped'").fetchone()[0] == "1"
    assert service.files_building is False

    rows = [dict(row) for row in service.db.execute(
        "SELECT path, name, ext, is_dir, size FROM files ORDER BY path")]
    assert [row["path"] for row in rows] == [
        "Command",
        "Command/UDG",
        "Command/UDG/20.15.2",
        _OLD_REL,
    ]
    file_row = rows[-1]
    assert file_row["name"] == "UDG@MMLCommand@ADD URR.md"
    assert file_row["ext"] == "md"
    assert file_row["is_dir"] == 0
    # Windows write_text 可能做 LF -> CRLF 转换；户口册应忠实记录磁盘实际字节数。
    assert file_row["size"] == asset.stat().st_size

    from app.repos import files_repo
    assert files_repo.integrity_ok(service.db)
    assert service.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 4
    assert service.db.execute("SELECT COUNT(*) FROM files_fts").fetchone()[0] == 4
    assert service.db.execute("SELECT COUNT(*) FROM files_fts_map").fetchone()[0] == 4
