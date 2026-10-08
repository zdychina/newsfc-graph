"""fs router 测试：资产目录浏览器 + 指定目录上传（target_dir 驱动）。

conftest 的 ``_skip_auth_for_pure_graph_tests`` 把 authenticate mock 成 admin，故
请求无需带 KEY；fs 写操作的 upload 权限矩阵由 test_auth.py 覆盖（/fs/mkdir 403）。

真实 platform-data/assets 布局不规则（Command 用 layer 名、Task 层 3 子 type 用 type
名、Feature 子目录），故 fs 写操作**不用 classify()**，改 target_dir 驱动：
- upload/move：调用方指定 target_dir，文件名 = frontmatter.id。
- PUT 编辑写回原路径（不归位），仅校验 id 未变。
- rename：原目录 + new_id.md，全库重写 ``[[old]]→[[new]]``。
"""
import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.frontmatter_rw import rewrite_frontmatter, validate_md
from app.index import Index
from app.main import app
from app.md_parser import parse_md
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
BD = (
    "---\n"
    "id: BusinessDomain@demo\n"
    "type: BusinessDomain\n"
    "domain: demo\n"
    "---\n"
    "# Demo Domain\n"
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


# ---------- Store ----------

def test_store_list_children(tmp_data_dir):
    store = Store(tmp_data_dir)
    store.write("Command/alpha/20.15.2/a.md", "x")
    store.write("Command/alpha/20.16.0/b.md", "y")
    assert "Command" in [c["name"] for c in store.list_children("")]
    assert [c["name"] for c in store.list_children("Command")] == ["alpha"]
    assert [c["name"] for c in store.list_children("Command/alpha")] == ["20.15.2", "20.16.0"]
    leaf = store.list_children("Command/alpha/20.15.2")
    assert leaf[0]["name"] == "a.md" and leaf[0]["is_dir"] is False
    assert store.list_children("not/exists") == []


def test_store_delete_move_rmtree_makedirs(tmp_data_dir):
    store = Store(tmp_data_dir)
    store.write("a/b.md", "x")
    assert store.delete("a/b.md") is True
    assert store.delete("a/b.md") is False
    store.write("x.md", "1")
    store.move("x.md", "sub/y.md")
    assert store.exists("sub/y.md") and not store.exists("x.md")
    store.write("dir/f.md", "z")
    assert store.rmtree("dir") is True
    store.makedirs("empty/dir")
    assert store.abspath("empty/dir").is_dir()


def test_store_cleanup_empty_dirs(tmp_data_dir):
    store = Store(tmp_data_dir)
    store.write("Command/alpha/20.15.2/a.md", "x")
    store.delete("Command/alpha/20.15.2/a.md")
    store.cleanup_empty_dirs("Command/alpha/20.15.2/a.md")
    assert not store.abspath("Command").exists()  # 全空 → 删到根


def test_store_path_traversal_rejected(tmp_data_dir):
    store = Store(tmp_data_dir)
    with pytest.raises(ValueError):
        store.read("/etc/passwd")
    with pytest.raises(ValueError):
        store.delete("../escape.md")
    with pytest.raises(ValueError):
        store.move("a.md", "../escape.md")


@pytest.mark.parametrize("path", [
    "Command/bad:name.md", "Command/NUL.md", "Command/trailing. ",
    "Command/control\x01.md",
])
def test_store_rejects_nonportable_windows_path_aliases(tmp_data_dir, path):
    store = Store(tmp_data_dir)
    with pytest.raises(ValueError):
        store.write(path, "x")


# ---------- frontmatter_rw ----------

def test_rewrite_frontmatter_overrides():
    out = rewrite_frontmatter(CMD, {"nf": "beta", "version": "20.16.0"})
    fm, _b, _e = parse_md(out)
    assert fm["nf"] == "beta" and fm["version"] == "20.16.0"
    assert fm["id"] == "alpha@MMLCommand@ADD DEMO"
    assert "ADD DEMO" in out


def test_rewrite_frontmatter_delete_field():
    out = rewrite_frontmatter(CMD, {"name": None})
    assert "name" not in parse_md(out)[0]


def test_validate_md_ok_and_infer():
    reg = Registry.load_default()
    assert validate_md(CMD, reg) == ("alpha@MMLCommand@ADD DEMO", "MMLCommand")
    md = "---\nid: alpha@MMLCommand@FOO\nnf: alpha\nversion: 1\n---\n# FOO\n"
    assert validate_md(md, reg) == ("alpha@MMLCommand@FOO", "MMLCommand")


def test_validate_md_rejects():
    reg = Registry.load_default()
    with pytest.raises(ValueError):
        validate_md("---\ntype: MMLCommand\nnf: a\nversion: 1\n---\n", reg)
    with pytest.raises(ValueError):
        validate_md("---\nid: x@Bogus@y\ntype: Bogus\n---\n", reg)


# ---------- fs router：GET ----------

def test_fs_children_and_read(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    with TestClient(app) as c:
        root = c.get("/api/v1/fs/children").json()
        assert any(x["name"] == "Command" for x in root)
        r = c.get("/api/v1/fs/file", params={"path": p})
        assert r.status_code == 200 and "ADD DEMO" in r.text
        assert c.get("/api/v1/fs/file", params={"path": "nope.md"}).status_code == 404


# ---------- PUT 编辑（写回原路径）----------

def test_fs_put_writes_back_in_place(tmp_data_dir, monkeypatch):
    """编辑改正文/version 写回原路径，不移动文件。"""
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    edited = CMD.replace("version: 20.15.2", "version: 20.16.0").replace("# ADD DEMO", "# CHANGED")
    with TestClient(app) as c:
        r = c.put("/api/v1/fs/file", params={"path": p}, json={"content": edited})
        assert r.status_code == 200, r.text
        assert r.json()["path"] == p  # 写回原路径，未移动
    # 文件仍在原位，内容已改
    assert s.store.exists(p)
    content = s.store.read(p)
    assert "version: 20.16.0" in content and "# CHANGED" in content


def test_fs_put_rejects_id_change(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    edited = CMD.replace("alpha@MMLCommand@ADD DEMO", "alpha@MMLCommand@CHANGED")
    with TestClient(app) as c:
        r = c.put("/api/v1/fs/file", params={"path": p}, json={"content": edited})
        assert r.status_code == 400


def test_fs_put_new_file_registers_parent_directories(tmp_data_dir, monkeypatch):
    """PUT 可新建文件；新建的多级父目录必须与文件同步入册。"""
    s = _setup(tmp_data_dir, monkeypatch)
    path = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    with TestClient(app) as c:
        response = c.put(
            "/api/v1/fs/file", params={"path": path}, json={"content": CMD})
    assert response.status_code == 200, response.text
    rows = {row["path"] for row in s.db.execute(
        "SELECT path FROM files ORDER BY path")}
    assert rows == {
        "Command", "Command/alpha", "Command/alpha/20.15.2", path}


# ---------- DELETE / mkdir ----------

def test_fs_delete_cleans_empty_dirs(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    with TestClient(app) as c:
        assert c.delete("/api/v1/fs/file", params={"path": p}).status_code == 200
    assert not s.store.exists(p)
    assert not s.store.abspath("Command").exists()
    assert s.db.execute(
        "SELECT COUNT(*) FROM files WHERE path IN (?,?,?)",
        ("Command", "Command/alpha", "Command/alpha/20.15.2"),
    ).fetchone()[0] == 0


def test_fs_delete_dir_recursive(tmp_data_dir, monkeypatch):
    """删目录：递归删子树 + 清理其下所有 md 的索引节点。"""
    s = _setup(tmp_data_dir, monkeypatch)
    p1 = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    p2 = "Command/alpha/20.16.0/alpha@MMLCommand@ADD DEMO2.md"
    cmd2 = CMD.replace("ADD DEMO\n", "ADD DEMO2\n").replace("20.15.2\n", "20.16.0\n")
    s.store.write(p1, CMD)
    s.store.write(p2, cmd2)
    s.rebuild()
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO", "20.15.2") is not None
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO2", "20.16.0") is not None
    with TestClient(app) as c:
        r = c.delete("/api/v1/fs/file", params={"path": "Command/alpha"})
        assert r.status_code == 200, r.text
    assert not s.store.exists("Command/alpha")
    assert not s.store.exists(p1) and not s.store.exists(p2)
    # 子 md 的索引节点全部清理
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO", "20.15.2") is None
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO2", "20.16.0") is None


def test_fs_delete_root_rejected(tmp_data_dir, monkeypatch):
    """空 path（assets 根）拒绝递归删除。"""
    _setup(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        assert c.delete("/api/v1/fs/file", params={"path": ""}).status_code == 400


# ---------- 软删除 / 回收站 ----------

def test_fs_soft_delete_file_to_trash_and_restore(tmp_data_dir, monkeypatch):
    """删文件→软删进回收站（assets 消失+unindex）；还原→回原位+reindex+trash 行删。"""
    from app.repos import trash_repo
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO", "20.15.2") is not None
    with TestClient(app) as c:
        r = c.delete("/api/v1/fs/file", params={"path": p})
        assert r.status_code == 200, r.text
        tid = r.json()["trash_id"]
        assert r.json()["md_count"] == 1
    assert not s.store.exists(p)
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO", "20.15.2") is None
    item = trash_repo.get(s.db, tid)
    assert item is not None and item["original_path"] == p and item["is_dir"] is False
    assert (s.store.trash / tid / p).exists()  # 内容完好躺在回收站
    with TestClient(app) as c:
        r2 = c.post("/api/v1/fs/trash/restore", json={"id": tid})
        assert r2.status_code == 200, r2.text
    assert s.store.exists(p)
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO", "20.15.2") is not None
    assert trash_repo.get(s.db, tid) is None


def test_fs_restore_single_file_registers_recreated_parent_dirs(
        tmp_data_dir, monkeypatch):
    """单文件是目录中唯一内容时，删除会清空父目录；还原须把重建的目录也入册。"""
    from app.file_query import search_files_core

    s = _setup(tmp_data_dir, monkeypatch)
    parent = "Command/alpha/20.15.2"
    path = f"{parent}/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(path, CMD)
    s.rebuild()

    with TestClient(app) as c:
        deleted = c.delete("/api/v1/fs/file", params={"path": path})
        assert deleted.status_code == 200, deleted.text
        restored = c.post(
            "/api/v1/fs/trash/restore",
            json={"id": deleted.json()["trash_id"]},
        )
        assert restored.status_code == 200, restored.text

    parents = {row["path"] for row in s.db.execute(
        "SELECT path FROM files WHERE is_dir=1")}
    assert {"Command", "Command/alpha", parent} <= parents
    listed = search_files_core(path=parent)
    assert [item["path"] for item in listed["files"]] == [path]


@pytest.mark.parametrize("request_path", [
    r"Command\alpha\20.15.2\alpha@MMLCommand@ADD DEMO.md",
    "Command/alpha/./20.15.2/alpha@MMLCommand@ADD DEMO.md",
    "Command/alpha/temp/../20.15.2/alpha@MMLCommand@ADD DEMO.md",
    r"C:\outside\alpha@MMLCommand@ADD DEMO.md",
])
def test_fs_write_path_variants_are_rejected_or_use_one_canonical_identity(
        tmp_data_dir, monkeypatch, request_path):
    """同一物理文件不得因 Windows 分隔符或点段产生 files/objects 双重身份。"""
    from app.file_query import search_files_core

    s = _setup(tmp_data_dir, monkeypatch)
    canonical = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    with TestClient(app) as c:
        response = c.put(
            "/api/v1/fs/file", params={"path": request_path},
            json={"content": CMD},
        )

    # API 可以选择严格拒绝非规范输入；一旦接受，就必须只留下规范路径身份。
    if 400 <= response.status_code < 500:
        assert s.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0
        assert s.db.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 0
        return
    assert response.status_code == 200, response.text
    file_paths = [row["path"] for row in s.db.execute(
        "SELECT path FROM files WHERE is_dir=0")]
    object_paths = [row["source_path"] for row in s.db.execute(
        "SELECT source_path FROM objects")]
    assert file_paths == [canonical]
    assert object_paths == [canonical]

    hit = search_files_core(query="ADD DEMO")["files"]
    assert [(item["path"], item["obj_id"], item["version"])
            for item in hit] == [
        (canonical, "alpha@MMLCommand@ADD DEMO", "20.15.2")]


def test_fs_soft_delete_dir_recursive_to_trash(tmp_data_dir, monkeypatch):
    """删目录→整树软删进回收站（md_count=子md数）；还原→子 md 全部 reindex。"""
    from app.repos import trash_repo
    s = _setup(tmp_data_dir, monkeypatch)
    p1 = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    p2 = "Command/alpha/20.16.0/alpha@MMLCommand@ADD DEMO2.md"
    cmd2 = CMD.replace("ADD DEMO\n", "ADD DEMO2\n").replace("20.15.2\n", "20.16.0\n")
    s.store.write(p1, CMD)
    s.store.write(p2, cmd2)
    s.rebuild()
    with TestClient(app) as c:
        r = c.delete("/api/v1/fs/file", params={"path": "Command/alpha"})
        assert r.status_code == 200, r.text
        tid = r.json()["trash_id"]
        assert r.json()["md_count"] == 2
    assert not s.store.exists("Command/alpha")
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO", "20.15.2") is None
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO2", "20.16.0") is None
    assert trash_repo.get(s.db, tid)["is_dir"] is True
    # 还原整树
    with TestClient(app) as c:
        assert c.post("/api/v1/fs/trash/restore", json={"id": tid}).status_code == 200
    assert s.store.exists(p1) and s.store.exists(p2)
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO", "20.15.2") is not None
    assert s.index.resolve_node("alpha@MMLCommand@ADD DEMO2", "20.16.0") is not None


def test_fs_trash_restore_conflict_keeps_item(tmp_data_dir, monkeypatch):
    """原路径被新文件占用 → 还原 409，条目保留在回收站。"""
    from app.repos import trash_repo
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    with TestClient(app) as c:
        tid = c.delete("/api/v1/fs/file", params={"path": p}).json()["trash_id"]
    s.store.write(p, CMD)  # 原路径被新内容占用
    with TestClient(app) as c:
        r = c.post("/api/v1/fs/trash/restore", json={"id": tid})
        assert r.status_code == 409
    assert trash_repo.get(s.db, tid) is not None  # 条目仍在回收站


def test_fs_trash_purge_admin_only(tmp_data_dir, monkeypatch):
    """永久删除仅 admin：assets 用户删得进回收站但 purge 403；admin purge 物理消失。"""
    from app.repos import trash_repo
    from app.users import store as users_store
    from app.middleware import auth as auth_mod
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    am = {"username": "am", "key": "k_am", "can_frontend": True, "can_assets": True}
    ad = {"username": "ad", "key": "k_ad", "can_frontend": True, "is_admin": True}
    users_store.add_user(am)
    users_store.add_user(ad)
    monkeypatch.setattr(auth_mod, "authenticate", lambda key: {"k_am": am, "k_ad": ad}.get(key))
    with TestClient(app) as c:
        tid = c.delete("/api/v1/fs/file", params={"path": p}, headers={"X-API-Key": "k_am"}).json()["trash_id"]
        assert c.delete(f"/api/v1/fs/trash/{tid}", headers={"X-API-Key": "k_am"}).status_code == 403
        assert c.delete(f"/api/v1/fs/trash/{tid}", headers={"X-API-Key": "k_ad"}).status_code == 200
    assert trash_repo.get(s.db, tid) is None
    assert not (s.store.trash / tid).exists()  # 物理消失


def test_fs_trash_empty(tmp_data_dir, monkeypatch):
    """清空回收站（admin）→ 表空 + .trash 目录空。"""
    from app.repos import trash_repo
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    with TestClient(app) as c:
        assert c.delete("/api/v1/fs/file", params={"path": p}).status_code == 200
        r = c.delete("/api/v1/fs/trash")
        assert r.status_code == 200 and r.json()["purged"] == 1
    assert trash_repo.count(s.db) == 0
    assert not any(s.store.trash.iterdir())


def test_fs_mkdir(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/v1/fs/mkdir", json={"path": "Feature/newnf/newver"})
        assert r.status_code == 200
    assert svc.get_service().store.abspath("Feature/newnf/newver").is_dir()
    rows = {r["path"] for r in s.db.execute(
        "SELECT path FROM files WHERE path IN (?,?,?)",
        ("Feature", "Feature/newnf", "Feature/newnf/newver"),
    )}
    assert rows == {"Feature", "Feature/newnf", "Feature/newnf/newver"}


@pytest.mark.parametrize("path", ["", "/", "\\"])
def test_fs_mkdir_rejects_assets_root(path, tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        r = c.post("/api/v1/fs/mkdir", json={"path": path})
    assert r.status_code == 400
    assert s.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


# ---------- move（target_dir 驱动）----------

def test_fs_move_to_target_dir(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    with TestClient(app) as c:
        r = c.post("/api/v1/fs/move", json={"src": p, "target_dir": "Command/alpha/20.16.0"})
        assert r.status_code == 200, r.text
        new_path = r.json()["new_path"]
    assert new_path == "Command/alpha/20.16.0/alpha@MMLCommand@ADD DEMO.md"
    assert s.store.exists(new_path)
    assert not s.store.exists(p)
    rows = {r["path"] for r in s.db.execute(
        "SELECT path FROM files WHERE path IN (?,?,?,?)",
        ("Command", "Command/alpha", "Command/alpha/20.15.2",
         "Command/alpha/20.16.0"),
    )}
    assert rows == {"Command", "Command/alpha", "Command/alpha/20.16.0"}


def test_fs_move_with_fm_override(tmp_data_dir, monkeypatch):
    """move 同时覆盖 frontmatter（保持 fm 与目录一致）。"""
    s = _setup(tmp_data_dir, monkeypatch)
    p = "Command/alpha/20.15.2/alpha@MMLCommand@ADD DEMO.md"
    s.store.write(p, CMD)
    s.rebuild()
    with TestClient(app) as c:
        r = c.post("/api/v1/fs/move", json={
            "src": p, "target_dir": "Command/alpha/20.16.0",
            "nf": "alpha", "version": "20.16.0",
        })
        assert r.status_code == 200
    fm, _b, _e = parse_md(s.store.read("Command/alpha/20.16.0/alpha@MMLCommand@ADD DEMO.md"))
    assert fm["version"] == "20.16.0"


# ---------- rename（改 id，原目录 + 新文件名）----------

def test_fs_rename_dry_run_and_apply(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    a_md = (CMD.replace("alpha@MMLCommand@ADD DEMO", "alpha@MMLCommand@AAA")
                .replace("name: DEMO", "name: AAA").replace("# ADD DEMO", "# AAA"))
    b_md = ("---\nid: alpha@MMLCommand@BBB\ntype: MMLCommand\nnf: alpha\nversion: 20.15.2\n"
            "---\n参见 [[alpha@MMLCommand@AAA]]\n")
    pa = "Command/alpha/20.15.2/alpha@MMLCommand@AAA.md"
    pb = "Command/alpha/20.15.2/alpha@MMLCommand@BBB.md"
    s.store.write(pa, a_md)
    s.store.write(pb, b_md)
    s.rebuild()
    # 模拟历史漂移：文件在册，父目录行丢失。rename 应随目标补齐。
    from app.repos import files_repo
    files_repo.remove_path(s.db, "Command/alpha/20.15.2")
    s.db.commit()
    with TestClient(app) as c:
        d = c.post("/api/v1/fs/rename",
                   json={"path": pa, "new_id": "alpha@MMLCommand@AAA2", "dry_run": True}).json()
        assert d["dry_run"] is True and d["affected"] == 1
        # new_path 在原目录（不跨目录）
        assert d["new_path"] == "Command/alpha/20.15.2/alpha@MMLCommand@AAA2.md"
        r = c.post("/api/v1/fs/rename",
                   json={"path": pa, "new_id": "alpha@MMLCommand@AAA2", "dry_run": False})
        assert r.status_code == 200, r.text
    # BBB 的 wikilink 已重写
    assert "[[alpha@MMLCommand@AAA2]]" in s.store.read(pb)
    assert "[[alpha@MMLCommand@AAA]]" not in s.store.read(pb)
    # AAA 改名（原目录 + 新文件名）
    new_pa = "Command/alpha/20.15.2/alpha@MMLCommand@AAA2.md"
    assert s.store.exists(new_pa) and not s.store.exists(pa)
    assert parse_md(s.store.read(new_pa))[0]["id"] == "alpha@MMLCommand@AAA2"
    # files 户口册：改名后新行在、旧行不在（rename 挂钩；rebuild 已灌两行）
    assert s.db.execute(
        "SELECT 1 FROM files WHERE path=?", (new_pa,)).fetchone() is not None
    assert s.db.execute(
        "SELECT 1 FROM files WHERE path=?", (pa,)).fetchone() is None
    # 使现有册目缺目录行，rename 也应补齐目标父目录。
    assert s.db.execute(
        "SELECT 1 FROM files WHERE path=?", ("Command/alpha/20.15.2",)
    ).fetchone() is not None


@pytest.mark.parametrize("new_id", [
    "../escape", "", "foo", "alpha@Feature@AAA", "beta@MMLCommand@AAA",
])
def test_fs_rename_rejects_invalid_target_before_rewriting_references(
        tmp_data_dir, monkeypatch, new_id):
    s = _setup(tmp_data_dir, monkeypatch)
    src = "Command/alpha/20.15.2/alpha@MMLCommand@AAA.md"
    ref = "Command/alpha/20.15.2/alpha@MMLCommand@BBB.md"
    src_md = CMD.replace("alpha@MMLCommand@ADD DEMO",
                         "alpha@MMLCommand@AAA")
    original_ref = ("---\nid: alpha@MMLCommand@BBB\ntype: MMLCommand\n---\n"
                    "[[alpha@MMLCommand@AAA]]\n")
    s.store.write(src, src_md)
    s.store.write(ref, original_ref)
    s.rebuild()

    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.post("/api/v1/fs/rename", json={
            "path": src, "new_id": new_id, "dry_run": False})

    assert response.status_code == 400
    assert s.store.exists(src)
    assert s.store.read(ref) == original_ref


def test_fs_rename_rejects_existing_target_without_overwrite(
        tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    src = "Command/alpha/20.15.2/alpha@MMLCommand@AAA.md"
    target = "Command/alpha/20.15.2/alpha@MMLCommand@BBB.md"
    src_md = CMD.replace("alpha@MMLCommand@ADD DEMO",
                         "alpha@MMLCommand@AAA")
    target_md = CMD.replace("alpha@MMLCommand@ADD DEMO",
                            "alpha@MMLCommand@BBB")
    s.store.write(src, src_md)
    s.store.write(target, target_md)
    s.rebuild()

    with TestClient(app) as c:
        response = c.post("/api/v1/fs/rename", json={
            "path": src, "new_id": "alpha@MMLCommand@BBB", "dry_run": False})

    assert response.status_code == 409
    assert s.store.read(src) == src_md
    assert s.store.read(target) == target_md


# ---------- upload（target_dir 驱动）----------

def test_fs_upload_to_target_dir(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    md = ("---\nid: alpha@MMLCommand@UPLOAD\ntype: MMLCommand\nnf: old\nversion: 1\n"
          "name: U\n---\n# hi\n")
    with TestClient(app) as c:
        r = c.post("/api/v1/fs/upload",
                   data={"target_dir": "Command/alpha/20.99.99"},
                   files={"files": ("u.md", md, "text/markdown")})
        assert r.status_code == 200, r.text
        assert r.json()["added"] == 1
    # 落到 target_dir/id.md（target_dir 权威，不经 classify）
    p = "Command/alpha/20.99.99/alpha@MMLCommand@UPLOAD.md"
    assert s.store.exists(p)
    # files 户口册：上传文件行在册（upload 挂钩）
    assert s.db.execute(
        "SELECT 1 FROM files WHERE path=?", (p,)).fetchone() is not None
    rows = {row["path"] for row in s.db.execute(
        "SELECT path FROM files WHERE is_dir=1")}
    assert rows == {"Command", "Command/alpha", "Command/alpha/20.99.99"}


def test_fs_upload_overrides_fm(tmp_data_dir, monkeypatch):
    """提供 nf/version → 覆盖 frontmatter（位置权威）。"""
    s = _setup(tmp_data_dir, monkeypatch)
    md = ("---\nid: alpha@MMLCommand@UPLOAD\ntype: MMLCommand\nnf: old\nversion: 1\n"
          "name: U\n---\n# hi\n")
    with TestClient(app) as c:
        r = c.post("/api/v1/fs/upload",
                   data={"target_dir": "Command/alpha/20.99.99", "nf": "alpha", "version": "20.99.99"},
                   files={"files": ("u.md", md, "text/markdown")})
        assert r.status_code == 200 and r.json()["added"] == 1
    fm, _b, _e = parse_md(s.store.read("Command/alpha/20.99.99/alpha@MMLCommand@UPLOAD.md"))
    assert fm["nf"] == "alpha" and fm["version"] == "20.99.99"


def test_fs_upload_accepts_zip(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    md = "---\nid: alpha@MMLCommand@Z1\ntype: MMLCommand\nnf: x\nversion: y\nname: Z\n---\n# z\n"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("z1.md", md)
    buf.seek(0)
    with TestClient(app) as c:
        r = c.post("/api/v1/fs/upload",
                   data={"target_dir": "Command/alpha/20.15.2"},
                   files={"files": ("bundle.zip", buf.read(), "application/zip")})
        assert r.status_code == 200 and r.json()["added"] == 1
    assert s.store.exists("Command/alpha/20.15.2/alpha@MMLCommand@Z1.md")


def test_fs_upload_rejects_missing_id(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    md = "---\ntype: MMLCommand\nnf: x\nversion: y\n---\nno id\n"
    with TestClient(app) as c:
        body = c.post("/api/v1/fs/upload",
                      data={"target_dir": "Command/alpha/20.15.2"},
                      files={"files": ("bad.md", md, "text/markdown")}).json()
    assert body["skipped"] == 1 and body["added"] == 0


def test_fs_upload_rejects_path_like_id_as_bad_request(tmp_data_dir, monkeypatch):
    s = _setup(tmp_data_dir, monkeypatch)
    bad = CMD.replace("alpha@MMLCommand@ADD DEMO", "../escape")
    with TestClient(app, raise_server_exceptions=False) as c:
        response = c.post(
            "/api/v1/fs/upload", data={"target_dir": "Command/alpha/20.15.2"},
            files={"files": ("bad.md", bad, "text/markdown")},
        )
    assert response.status_code == 400
    assert s.db.execute("SELECT COUNT(*) FROM objects").fetchone()[0] == 0


def test_fs_upload_wrong_layer_skipped(tmp_data_dir, monkeypatch):
    """选 Command 层但 md 是 AtomTask → warning 跳过（不写错位），提示应属于 AtomTask 层。"""
    _setup(tmp_data_dir, monkeypatch)
    md = "---\nid: alpha@AtomTask@X\ntype: AtomTask\nnf: alpha\n---\n# x\n"
    with TestClient(app) as c:
        body = c.post("/api/v1/fs/upload",
                      data={"target_dir": "Command/alpha/20.15.2"},
                      files={"files": ("x.md", md, "text/markdown")}).json()
    assert body["skipped"] == 1 and body["added"] == 0
    assert any("应上传到" in w and "AtomTask" in w for w in body["warnings"])


# ---------- 端到端连贯流程 ----------

def test_e2e_user_journey(tmp_data_dir, monkeypatch):
    """上传 → 浏览 → 编辑(写回) → 移动 → 重命名(引用重写) → 删除。"""
    s = _setup(tmp_data_dir, monkeypatch)
    a_md = (CMD.replace("alpha@MMLCommand@ADD DEMO", "alpha@MMLCommand@AAA")
                .replace("name: DEMO", "name: AAA").replace("# ADD DEMO", "# AAA"))
    b_md = ("---\nid: alpha@MMLCommand@BBB\ntype: MMLCommand\nnf: alpha\nversion: 20.15.2\n"
            "name: BBB\n---\n参见 [[alpha@MMLCommand@AAA]]\n")
    s.store.write("Command/alpha/20.15.2/alpha@MMLCommand@AAA.md", a_md)
    s.store.write("Command/alpha/20.15.2/alpha@MMLCommand@BBB.md", b_md)
    s.rebuild()

    with TestClient(app) as c:
        # 1) 上传新 md 到 Command/alpha/20.99.99（覆盖 fm）
        new_md = ("---\nid: alpha@MMLCommand@UPLOAD\ntype: MMLCommand\nnf: old\nversion: 1\n"
                  "name: U\n---\n# hi\n")
        r = c.post("/api/v1/fs/upload",
                   data={"target_dir": "Command/alpha/20.99.99", "nf": "alpha", "version": "20.99.99"},
                   files={"files": ("u.md", new_md, "text/markdown")})
        assert r.status_code == 200 and r.json()["added"] == 1

        # 2) 浏览：列 20.99.99 能看到刚上传文件
        children = c.get("/api/v1/fs/children", params={"path": "Command/alpha/20.99.99"}).json()
        assert any(ch["name"] == "alpha@MMLCommand@UPLOAD.md" for ch in children)

        # 3) 读：fm 被覆盖
        p = "Command/alpha/20.99.99/alpha@MMLCommand@UPLOAD.md"
        content = c.get("/api/v1/fs/file", params={"path": p}).text
        assert "version: 20.99.99" in content and "nf: alpha" in content

        # 4) 编辑写回原路径（改正文，不移动）
        edited = content.replace("# hi", "# edited")
        r = c.put("/api/v1/fs/file", params={"path": p}, json={"content": edited})
        assert r.status_code == 200 and r.json()["path"] == p
        assert "# edited" in s.store.read(p)

        # 5) 移动到 20.101.0
        r = c.post("/api/v1/fs/move", json={"src": p, "target_dir": "Command/alpha/20.101.0"})
        assert r.status_code == 200
        p2 = r.json()["new_path"]
        assert p2 == "Command/alpha/20.101.0/alpha@MMLCommand@UPLOAD.md"
        assert not s.store.exists(p)

        # 6) 重命名 AAA → AAA2（BBB 引用 AAA 被重写）
        pa = "Command/alpha/20.15.2/alpha@MMLCommand@AAA.md"
        c.post("/api/v1/fs/rename", json={"path": pa, "new_id": "alpha@MMLCommand@AAA2", "dry_run": False})
        b_text = s.store.read("Command/alpha/20.15.2/alpha@MMLCommand@BBB.md")
        assert "[[alpha@MMLCommand@AAA2]]" in b_text
        assert s.store.exists("Command/alpha/20.15.2/alpha@MMLCommand@AAA2.md")

        # 7) 删除 BBB
        pb = "Command/alpha/20.15.2/alpha@MMLCommand@BBB.md"
        assert c.delete("/api/v1/fs/file", params={"path": pb}).status_code == 200
        assert not s.store.exists(pb)


# ---------- 写端点同步 files 户口册 ----------

def test_fs_write_endpoints_sync_files(tmp_data_dir, monkeypatch):
    """/fs 七个写端点各自同步 files 户口册（mkdir/put/move/delete/restore；upload/rename 由专项覆盖）。"""
    s = _setup(tmp_data_dir, monkeypatch)
    with TestClient(app) as c:
        # mkdir → 目录行
        r = c.post("/api/v1/fs/mkdir", json={"path": "Command/alpha/20.9.9"})
        assert r.status_code == 200, r.text
        row = s.db.execute(
            "SELECT is_dir FROM files WHERE path='Command/alpha/20.9.9'").fetchone()
        assert row is not None and row["is_dir"] == 1
        # put_file → 文件行
        r = c.put("/api/v1/fs/file",
                  params={"path": "Command/alpha/20.9.9/t.md"},
                  json={"content": CMD})
        assert r.status_code == 200, r.text
        assert s.db.execute(
            "SELECT name FROM files WHERE path='Command/alpha/20.9.9/t.md'"
        ).fetchone()["name"] == "t.md"
        # move → 旧行消失、新行出现（move 后文件名 = id）
        r = c.post("/api/v1/fs/move",
                   json={"src": "Command/alpha/20.9.9/t.md",
                         "target_dir": "Command/alpha/20.9.9/sub"})
        assert r.status_code == 200, r.text
        assert s.db.execute(
            "SELECT 1 FROM files WHERE path='Command/alpha/20.9.9/t.md'"
        ).fetchone() is None
        assert s.db.execute(
            "SELECT 1 FROM files WHERE path='Command/alpha/20.9.9/sub/"
            "alpha@MMLCommand@ADD DEMO.md'").fetchone() is not None
        # delete 目录 → 前缀行全清
        r = c.delete("/api/v1/fs/file", params={"path": "Command/alpha/20.9.9"})
        assert r.status_code == 200, r.text
        assert s.db.execute(
            "SELECT COUNT(*) FROM files WHERE path LIKE 'Command/alpha/20.9.9%'"
        ).fetchone()[0] == 0
        # 回收站还原 → 行回来
        r = c.post("/api/v1/fs/trash/restore", json={"id": r.json()["trash_id"]})
        assert r.status_code == 200, r.text
        assert s.db.execute(
            "SELECT COUNT(*) FROM files WHERE path LIKE 'Command/alpha/20.9.9%'"
        ).fetchone()[0] > 0
