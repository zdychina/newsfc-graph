"""files 户口册 repo 单元测试（spec §4.1/§4.2）。"""
import threading

import pytest

from app.repos import files_repo


@pytest.fixture
def conn_db(tmp_data_dir):
    import app.db as dbmod
    conn = dbmod.get_db(tmp_data_dir.parent / "t.db")
    dbmod.init_schema(conn)
    return conn


@pytest.fixture
def store(tmp_data_dir):
    from app.store import Store
    return Store(tmp_data_dir)


def _count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_upsert_and_remove_roundtrip(conn_db, store):
    store.write("Command/UDG/20.15.2/x.md", "hello")
    files_repo.upsert_from_disk(conn_db, store, "Command/UDG/20.15.2/x.md")
    row = conn_db.execute("SELECT * FROM files WHERE path=?",
                          ("Command/UDG/20.15.2/x.md",)).fetchone()
    assert row["name"] == "x.md" and row["ext"] == "md" and row["is_dir"] == 0
    assert row["size"] == 5
    assert _count(conn_db, "files_fts") == 1
    # FTS 名字已规范化：不同大小写可命中
    assert conn_db.execute(
        "SELECT 1 FROM files_fts WHERE files_fts MATCH 'name : \"x.md\"'").fetchone()
    files_repo.remove_path(conn_db, "Command/UDG/20.15.2/x.md")
    assert _count(conn_db, "files") == 0 and _count(conn_db, "files_fts") == 0
    assert _count(conn_db, "files_fts_map") == 0  # map 同步清（无幽灵）


def test_upsert_from_disk_missing_removes_row(conn_db, store):
    files_repo.upsert_entry(conn_db, path="gone.md", name="gone.md", ext="md",
                            is_dir=0, size=1, mtime=0.0)
    files_repo.upsert_from_disk(conn_db, store, "gone.md")  # 磁盘不存在 → 删行
    assert _count(conn_db, "files") == 0


def test_remove_prefix_removes_subtree_and_self(conn_db, store):
    store.makedirs("Command/UDG/20.15.2")
    store.write("Command/UDG/20.15.2/a.md", "a")
    store.write("Command/UDG/20.15.2/sub/b.md", "b")
    files_repo.upsert_tree(conn_db, store, "Command")
    # Command 自身 + UDG + 20.15.2 + a.md + sub + b.md = 6
    assert _count(conn_db, "files") == 6
    # 目录 ext 恒 ''（schema 注释）：点名目录（版本号 20.15.2）不得算出 '2'
    row = conn_db.execute(
        "SELECT * FROM files WHERE path='Command/UDG/20.15.2'").fetchone()
    assert row["is_dir"] == 1 and row["ext"] == ""
    files_repo.remove_prefix(conn_db, "Command/UDG")
    assert _count(conn_db, "files") == 1  # 只剩 Command/
    assert _count(conn_db, "files_fts") == 1


def test_rebuild_all_skips_dotfiles_and_includes_dirs(conn_db, store):
    store.write("Command/a.md", "a")
    store.makedirs("Command/UDG/20.15.2")  # 含点号的目录名（版本目录）
    (store.root / ".hidden").write_text("x", encoding="utf-8")
    (store.root / "Command" / ".h").write_text("y", encoding="utf-8")
    n = files_repo.rebuild_all(conn_db, store)
    assert n == 4  # Command/ + UDG/ + 20.15.2/ 目录行 + a.md；点文件全跳过
    rows = {r["path"]: r for r in conn_db.execute("SELECT * FROM files")}
    assert rows["Command"]["is_dir"] == 1
    assert rows["Command"]["ext"] == ""
    assert rows["Command/UDG/20.15.2"]["ext"] == ""  # 目录 ext 恒 ''（非 '2'）


def test_reupsert_map_hit_keeps_single_fts_row(conn_db, store):
    store.write("a.md", "a")
    files_repo.upsert_from_disk(conn_db, store, "a.md")
    files_repo.upsert_from_disk(conn_db, store, "a.md")  # map 命中 → rowid 删旧行
    assert _count(conn_db, "files") == 1
    assert _count(conn_db, "files_fts") == 1
    assert _count(conn_db, "files_fts_map") == 1


def test_reupsert_map_miss_fallback_no_ghost(conn_db, store):
    store.write("a.md", "a")
    files_repo.upsert_from_disk(conn_db, store, "a.md")
    conn_db.execute("DELETE FROM files_fts_map WHERE path='a.md'")  # 模拟 map 行丢失
    conn_db.commit()
    files_repo.upsert_from_disk(conn_db, store, "a.md")  # map-miss → path 回退删
    assert _count(conn_db, "files_fts") == 1  # 不留重复（幽灵）行
    assert files_repo.integrity_ok(conn_db)
    # 重复感知对账：EXCEPT 集合语义查不出重复行，COUNT 感知须能查出
    conn_db.execute("INSERT INTO files_fts(path, name) VALUES('a.md', 'a.md')")
    conn_db.commit()
    assert not files_repo.integrity_ok(conn_db)


def test_upsert_from_disk_rejects_and_heals_dotfiles(conn_db, store):
    store.write("a/.secret.md", "s")
    files_repo.upsert_from_disk(conn_db, store, "a/.secret.md")  # 单路径入口拦截
    assert _count(conn_db, "files") == 0
    # 自愈：已在册的点文件（历史脏数据）同样被清，FTS/map 无残留
    files_repo.upsert_entry(conn_db, path="a/.secret.md", name=".secret.md",
                            ext="md", is_dir=0, size=1, mtime=0.0)
    files_repo.upsert_from_disk(conn_db, store, "a/.secret.md")
    assert _count(conn_db, "files") == 0
    assert _count(conn_db, "files_fts") == 0
    assert _count(conn_db, "files_fts_map") == 0


def test_integrity_ok_detects_drift(conn_db, store):
    store.write("a.md", "a")
    files_repo.rebuild_all(conn_db, store)
    assert files_repo.integrity_ok(conn_db)
    conn_db.execute("DELETE FROM files WHERE path='a.md'")  # 制造漂移
    conn_db.commit()
    assert not files_repo.integrity_ok(conn_db)


def _bare_service(tmp_data_dir):
    """__new__ 装配（同 test_fs._setup 形态），返回 service。"""
    import app.service as svc_mod
    from app.store import Store
    import app.db as dbmod
    from app.registry import Registry
    s = svc_mod.Service.__new__(svc_mod.Service)
    s.store = Store(tmp_data_dir)
    s.db = dbmod.get_db(tmp_data_dir.parent / "t.db")
    dbmod.init_schema(s.db)
    s.registry = Registry.load_default()
    return s


def test_rebuild_populates_files(tmp_data_dir):
    s = _bare_service(tmp_data_dir)
    s.store.write("Command/a.md", "a")
    s.rebuild()  # 应连带重建 files 户口册
    assert s.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2
    assert s.index is not None  # Index.load_from_db 走通
    # rebuild 统一走 rebuild_files：连带写完成标记（下次启动不重跑 bootstrap）
    assert s.db.execute(
        "SELECT value FROM meta WHERE key='files_bootstrapped'").fetchone() is not None


def test_files_bootstrap_async(tmp_data_dir):
    s = _bare_service(tmp_data_dir)
    s.store.write("Feature/x/概述.md", "f")
    s.files_building = True
    s._files_bootstrap_async()  # 同步直调（后台线程跑的就是这个函数体）
    assert s.files_building is False
    # Feature + x + 概述.md = 3 行
    assert s.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 3
    # 成功 → 同锁内写完成标记（__init__ 门控据此跳过；被杀留半截册无标记会重跑）
    assert s.db.execute(
        "SELECT value FROM meta WHERE key='files_bootstrapped'"
    ).fetchone()[0] == "1"


def test_mtime_sync_canonicalizes_legacy_backslash_source_without_deleting_object(
        tmp_data_dir):
    """存量反斜杠 source_path 自愈后，不得被 deleted 阶段再次删掉。"""
    s = _bare_service(tmp_data_dir)
    rel = "Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md"
    md = ("---\nid: UDG@MMLCommand@ADD URR\ntype: MMLCommand\n"
          "nf: UDG\nversion: 20.15.2\n---\nbody\n")
    s.store.write(rel, md)
    s.reindex_path(rel)
    legacy = rel.replace("/", "\\")
    s.db.execute("UPDATE objects SET source_path=? WHERE id=?",
                 (legacy, "UDG@MMLCommand@ADD URR"))
    s.db.commit()

    changed, deleted = s._scan_mtime_changes()
    assert rel in changed and legacy in deleted
    s._sync_mtime()

    rows = s.db.execute(
        "SELECT source_path FROM objects WHERE id=?",
        ("UDG@MMLCommand@ADD URR",)).fetchall()
    assert [row["source_path"] for row in rows] == [rel]


def test_bootstrap_reruns_when_table_nonempty_without_marker(tmp_data_dir):
    """门控行为锁定：表非空但无完成标记（进程被杀留半截册）→ bootstrap 重跑。"""
    s = _bare_service(tmp_data_dir)
    s.store.write("a.md", "x")
    from app.repos import files_repo
    files_repo.rebuild_all(s.db, s.store)          # 表非空
    s.db.execute("DELETE FROM meta WHERE key='files_bootstrapped'")
    s.db.commit()
    before = s.db.execute("SELECT mtime FROM files WHERE path='a.md'").fetchone()[0]
    s.store.write("b.md", "y")                     # 磁盘多了文件
    s._files_bootstrap_async()                     # 无标记 → 应重跑
    assert s.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 2  # a.md+b.md（根无目录行）
    assert s.db.execute(
        "SELECT value FROM meta WHERE key='files_bootstrapped'").fetchone() is not None
    assert before is not None


def test_files_bootstrap_async_failure_clears_no_marker(tmp_data_dir, monkeypatch):
    """失败路径：清残册三表 + 无标记 + flag 复位（下次启动自动重试/admin 兜底）。"""
    s = _bare_service(tmp_data_dir)
    s.store.write("a.md", "x")
    from app.repos import files_repo
    files_repo.rebuild_all(s.db, s.store)          # 半截册（表非空）
    files_repo.upsert_entry(s.db, path="ghost.md", name="ghost.md", ext="md",
                            is_dir=0, size=1, mtime=0.0)
    s.db.commit()

    def _boom(conn, store, chunk=None):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(files_repo, "rebuild_all", _boom)
    s.files_building = True
    s._files_bootstrap_async()                     # 失败不抛（后台线程绝不抛）
    # 失败后无完成标记，search_files 必须继续表示“未就绪”。
    assert s.files_building is True
    for t in ("files", "files_fts", "files_fts_map"):
        assert s.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0
    assert s.db.execute(
        "SELECT value FROM meta WHERE key='files_bootstrapped'").fetchone() is None


def test_rebuild_files_failure_invalidates_marker_and_partial_catalog(
        tmp_data_dir, monkeypatch):
    """admin/full rebuild 分块中途失败：旧 marker 与已提交半册一并清理。"""
    import app.service as svc_mod
    from app.file_query import search_files_core

    s = _bare_service(tmp_data_dir)
    s.files_building = False
    s.db.execute(
        "INSERT INTO meta(key, value) VALUES('files_bootstrapped', '1')")
    s.db.commit()

    def _partial_then_boom(conn, store):
        files_repo.upsert_entry(
            conn, path="partial.md", name="partial.md", ext="md",
            is_dir=0, size=1, mtime=0.0)
        conn.commit()  # 模拟 rebuild_all 分块已提交
        raise RuntimeError("scan failed")

    monkeypatch.setattr(files_repo, "rebuild_all", _partial_then_boom)
    monkeypatch.setattr(svc_mod, "_service", s)

    with pytest.raises(RuntimeError, match="scan failed"):
        s.rebuild_files()

    for table in ("files", "files_fts", "files_fts_map"):
        assert _count(s.db, table) == 0
    assert s.db.execute(
        "SELECT value FROM meta WHERE key='files_bootstrapped'").fetchone() is None
    out = search_files_core(ext="md")
    assert out["files"] == []
    assert out["index_building"] is True


def test_concurrent_rebuild_keeps_building_true_until_last_finishes(
        tmp_data_dir, monkeypatch):
    """两个 rebuild 排队时，第一个完成不得提前拉低共享状态。"""
    s = _bare_service(tmp_data_dir)
    s.files_building = False
    first_started = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    release_second = threading.Event()
    first_done = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def _blocking_rebuild(conn, store):
        nonlocal calls
        with calls_lock:
            calls += 1
            current = calls
        if current == 1:
            first_started.set()
            assert release_first.wait(2)
        else:
            second_started.set()
            assert release_second.wait(2)
        return 0

    monkeypatch.setattr(files_repo, "rebuild_all", _blocking_rebuild)

    def _first():
        try:
            s.rebuild_files()
        finally:
            first_done.set()

    t1 = threading.Thread(target=_first)
    t2 = threading.Thread(target=s.rebuild_files)
    t1.start()
    assert first_started.wait(2)
    t2.start()
    release_first.set()
    assert second_started.wait(2)
    assert first_done.wait(2)
    try:
        assert s.files_building is True
    finally:
        release_second.set()
        t1.join(2)
        t2.join(2)
    assert not t1.is_alive() and not t2.is_alive()
    assert s.files_building is False


def test_upsert_tree_heals_dotfile_rows(conn_db, store):
    """trash_restore 走 upsert_tree：还原子树时清掉历史脏点文件行（行在册、盘无此文件）。"""
    store.write("a/b.md", "b")
    files_repo.rebuild_all(conn_db, store)
    files_repo.upsert_entry(conn_db, path="a/.dirty.md", name=".dirty.md",
                            ext="md", is_dir=0, size=1, mtime=0.0)  # 历史脏行
    conn_db.commit()
    files_repo.upsert_tree(conn_db, store, "a")
    assert conn_db.execute(
        "SELECT COUNT(*) FROM files WHERE path='a/.dirty.md'").fetchone()[0] == 0
    assert conn_db.execute(
        "SELECT COUNT(*) FROM files WHERE path='a/b.md'").fetchone()[0] == 1


def test_entries_normalize_backslash_and_trailing_slash(conn_db, store):
    """入口路径规范化：反斜杠/尾斜杠不得造出前缀区间外的幽灵行（mkdir 尾斜杠同族）。"""
    store.write("a/b.md", "b")
    files_repo.rebuild_all(conn_db, store)  # a + a/b.md = 2 行
    files_repo.upsert_from_disk(conn_db, store, "a/")  # 尾斜杠 → 刷新 a 行，非造 "a/" 幽灵
    assert conn_db.execute(
        "SELECT COUNT(*) FROM files WHERE path LIKE 'a%'").fetchone()[0] == 2
    files_repo.remove_path(conn_db, "a\\b.md")  # 反斜杠 → 命中正斜杠行
    assert conn_db.execute(
        "SELECT COUNT(*) FROM files WHERE path='a/b.md'").fetchone()[0] == 0


def test_upsert_many_from_disk_mixed_batch(conn_db, store):
    """批量自愈同步：新文件/已存在(map命中)/磁盘缺失/点文件 四类一批各自落点。

    map 命中重刷不得造重复 FTS 行；磁盘缺失/点文件（含历史脏行）删行；
    去重保序；map rowid 指向真实 FTS 行；三表一致。
    """
    store.write("old.md", "old")
    files_repo.upsert_from_disk(conn_db, store, "old.md")   # 已存在（map 命中）
    files_repo.upsert_entry(conn_db, path="gone.md", name="gone.md", ext="md",
                            is_dir=0, size=1, mtime=0.0)    # 磁盘缺失的册行
    store.write("a/.secret.md", "s")
    files_repo.upsert_entry(conn_db, path="a/.secret.md", name=".secret.md",
                            ext="md", is_dir=0, size=1, mtime=0.0)  # 点文件脏行
    store.write("new.md", "new")                            # 新文件

    out = files_repo.upsert_many_from_disk(
        conn_db, store,
        ["new.md", "old.md", "gone.md", "a/.secret.md", "old.md"])  # old 重复

    assert out == {"upserted": 2, "removed": 2}
    paths = {r["path"] for r in conn_db.execute("SELECT path FROM files")}
    assert paths == {"new.md", "old.md"}
    assert _count(conn_db, "files_fts") == 2              # map 命中重刷无重复行
    assert _count(conn_db, "files_fts_map") == 2
    # map 一致：rowid 必须指向真实存在的 FTS 行（否则未来按 rowid 删失灵）
    for m in conn_db.execute("SELECT fts_rowid FROM files_fts_map"):
        assert conn_db.execute(
            "SELECT COUNT(*) FROM files_fts WHERE rowid=?",
            (m["fts_rowid"],)).fetchone()[0] == 1
    assert files_repo.integrity_ok(conn_db)
    row = conn_db.execute("SELECT size FROM files WHERE path='new.md'").fetchone()
    assert row["size"] == 3


def test_upsert_many_heals_fts_row_when_map_is_missing(conn_db, store):
    """批量 upsert 遇到 FTS 行在、map 丢失时，按 path 自愈而不造重复行。"""
    store.write("a.md", "old")
    files_repo.upsert_from_disk(conn_db, store, "a.md")
    conn_db.execute("DELETE FROM files_fts_map WHERE path=?", ("a.md",))
    conn_db.commit()

    store.write("a.md", "new")
    out = files_repo.upsert_many_from_disk(conn_db, store, ["a.md"])

    assert out == {"upserted": 1, "removed": 0}
    assert conn_db.execute(
        "SELECT COUNT(*) FROM files_fts WHERE path=?", ("a.md",)
    ).fetchone()[0] == 1
    assert _count(conn_db, "files_fts_map") == 1
    assert files_repo.integrity_ok(conn_db)


def test_upsert_many_from_disk_empty_and_missing_only(conn_db, store):
    """空集合零副作用；全部 miss（盘无文件）时册清空且不报错。"""
    assert files_repo.upsert_many_from_disk(conn_db, store, []) == \
        {"upserted": 0, "removed": 0}
    files_repo.upsert_entry(conn_db, path="x.md", name="x.md", ext="md",
                            is_dir=0, size=1, mtime=0.0)
    out = files_repo.upsert_many_from_disk(conn_db, store, ["x.md"])
    assert out == {"upserted": 0, "removed": 1}
    assert _count(conn_db, "files") == 0
    assert files_repo.integrity_ok(conn_db)


def test_upsert_parents_from_disk_registers_ancestors(conn_db, store):
    """文件路径集合推导父目录：全部祖先目录入册（a 与 a/b），根 '' 不入册，
    文件自身不入册；去重后返回目录数。"""
    store.write("a/b/c.md", "c")
    store.write("top.md", "t")
    n = files_repo.upsert_parents_from_disk(
        conn_db, store, ["a/b/c.md", "top.md", "a/b/c.md"])
    assert n == 2                                           # a + a/b（去重，根排除）
    rows = {r["path"]: r for r in conn_db.execute("SELECT * FROM files")}
    assert set(rows) == {"a", "a/b"}                        # c.md/top.md 非父目录
    for d in ("a", "a/b"):
        assert rows[d]["is_dir"] == 1 and rows[d]["ext"] == ""
        assert rows[d]["size"] == 0
    assert files_repo.integrity_ok(conn_db)


def test_remove_prefix_deletes_raw_noncanonical_keys(conn_db, store):
    files_repo.upsert_entry(conn_db, path="a/", name="a", ext="", is_dir=1,
                            size=0, mtime=0.0)  # 非规范脏行（尾斜杠）
    store.write("a/b.md", "b")
    files_repo.upsert_from_disk(conn_db, store, "a/b.md")
    n = files_repo.remove_prefix(conn_db, "a")
    assert n == 2
    assert conn_db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0


def test_parents_routed_through_batch_no_ghost(conn_db, store):
    store.makedirs("x/yyy")
    store.write("x/yyy/f.md", "f")
    files_repo.rebuild_all(conn_db, store)
    files_repo.upsert_parents_from_disk(conn_db, store, ["x/yyy/new.md"])
    # 新目录 x/yyy 已在册（祖先），无重复 FTS 行（批量路径不走单行回退）。
    # 目录名须 ≥3 字符：files_fts 是 trigram 分词，1-2 字符名无 trigram 不可 MATCH。
    assert conn_db.execute(
        "SELECT COUNT(*) FROM files_fts WHERE files_fts MATCH 'name : \"yyy\"'"
    ).fetchone()[0] == 1
