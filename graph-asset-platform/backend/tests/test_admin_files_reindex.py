"""POST /admin/files-reindex：全量重建 files 户口册（admin 权限，兜底外部直拷漂移）。"""
from fastapi.testclient import TestClient

from app.main import app
from tests.test_api_objects import _setup, CMD_EDGES

client = TestClient(app)


def test_files_reindex_rebuilds(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, {"cmd.md": CMD_EDGES})
    from app import db as dbmod
    db = dbmod.get_shared_db()
    db.execute("DELETE FROM files")
    db.commit()
    assert db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
    r = client.post("/api/v1/admin/files-reindex")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["files"] > 0
