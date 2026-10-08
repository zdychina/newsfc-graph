"""perf 冒烟（GAP_PERF=1 才跑）：合成 10 万对象，宽词 + 两字词断言 < 2s；search_files
罕见两字词 + path 游标遍历同测（T6/T9 评审要求：断言基于计时而非 EQP 文本）。

宽 ≥3 字词最坏路径 = 引擎内 bm25 全量打分（T9 只消了百万行回 Python 的搬运）；
若内网真实量下仍超标，已知下一手段：metadata_only 档 / 放宽 ORDER BY。2 字词走
body_like 档 LIKE（高频词 LIMIT 早停），罕见词全语料扫——两类分别覆盖。

files 三表**不走 rebuild_all**（它扫磁盘，盘上只有空 assets 会建出 0 行）：SQL 批插
（files_repo._insert_batch 同构）——files 直插、规范化 name 入 files_fts、map 用
rowid 区间一条 SQL 回填。语料设计：每 1000 个文件 1 个名字含罕见两字词「额管」
（无法 LIMIT 早停 → 全语料 LIKE 扫描最坏路径，命中恰 100 条），正文与其余文件名
同含高频词（配置/配额/参数说明）。
"""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("GAP_PERF") != "1",
                               reason="perf smoke: set GAP_PERF=1")

N = 100_000
RARE_EVERY = 1_000           # 每 1000 个文件 1 个名字含「额管」→ 罕见两字词命中 100


def _rel_path(i: int) -> str:
    stem = (f"额管核查{i:06d}" if i % RARE_EVERY == RARE_EVERY - 1
            else f"配置命令{i:06d}")
    return f"c/{stem}.md"


@pytest.fixture(scope="module")
def big_corpus(tmp_path_factory):
    """10 万合成对象一次性构建（模块内共享；全跳过时不建）。不写盘上 md（搜索
    只读 DB 派生表），绕过 Service.__init__（其 assets bootstrap 会去扫真实盘）。"""
    import app.service as svc_mod
    from app.store import Store
    import app.db as dbmod
    from app.registry import Registry
    from app.index import Index
    data = tmp_path_factory.mktemp("perfdata") / "platform-data"
    assets = data / "assets"
    assets.mkdir(parents=True)
    s = svc_mod.Service.__new__(svc_mod.Service)
    s.store = Store(assets)
    s.db = dbmod.get_db(data / "t.db")
    dbmod.init_schema(s.db)
    s.registry = Registry.load_default()
    s.index = Index.load_from_db(s.db, s.registry)
    s.files_building = False
    s.fts_rebuilding = False
    rows = []
    for i in range(N):
        oid = f"UDG@MMLCommand@CMD {i:06d}"
        body = f"配置命令 {oid} 的参数说明，涉及计费组与配额管理。第{i}条。"
        rows.append((oid, "20.15.2", "MMLCommand", "Command", "nf", _rel_path(i),
                     oid, "{}", body, body, 0.0))
    s.db.executemany(
        "INSERT INTO objects(id, version, type, layer, scope, source_path, name,"
        " frontmatter_json, body_md, raw_md, mtime) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    s.db.commit()
    from app.repos import graph_search_repo, object_latest_repo
    graph_search_repo.rebuild_from_objects(s.db)
    object_latest_repo.rebuild(s.db)
    yield s
    svc_mod._service = None  # 防 10 万合成库单例泄漏给后续测试模块


@pytest.fixture
def big(big_corpus, monkeypatch):
    """按测试注入合成 service：conftest 的 autouse 空 service 每测重建
    ``_service``（函数级后于本模块级 fixture 生效），故此处再 monkeypatch 回来
    （test_file_query 同款模式）。"""
    import app.service as svc_mod
    monkeypatch.setattr(svc_mod, "_service", big_corpus)
    return big_corpus


def _seed_files_ledger(s) -> None:
    """files 三表 SQL 批插（见模块 docstring：rebuild_all 扫盘会建出 0 行）。"""
    from app.repos.graph_search_repo import normalize_search_text
    conn = s.db
    if conn.execute("SELECT 1 FROM files LIMIT 1").fetchone():
        return
    paths = [_rel_path(i) for i in range(N)]
    names = [p.rsplit("/", 1)[-1] for p in paths]
    conn.executemany(
        "INSERT INTO files(path, name, ext, is_dir, size, mtime) VALUES(?,?,?,?,?,?)",
        [(p, n, "drawio" if i % RARE_EVERY == RARE_EVERY - 1 else "md",
          0, 1024, 1.0) for i, (p, n) in enumerate(zip(paths, names))])
    max_rid = conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM files_fts").fetchone()[0]
    conn.executemany(
        "INSERT INTO files_fts(path, name) VALUES(?,?)",
        [(p, normalize_search_text(n)) for p, n in zip(paths, names)])
    conn.execute(
        "INSERT INTO files_fts_map(path, fts_rowid) "
        "SELECT path, rowid FROM files_fts WHERE rowid>?", (max_rid,))
    conn.commit()


def _timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def test_broad_term_under_2s(big):
    from app.graph_query.search import search_graph_core
    out, dt = _timed(lambda: search_graph_core(terms=["配置"]))
    assert dt < 2.0, f"宽词耗时 {dt:.2f}s"
    assert out["total"] > 0


def test_broad_three_char_term_under_2s(big):
    """≥3 字宽词：正文 FTS trigram 短语 + 引擎内 bm25 全量打分 ORDER BY LIMIT
    2000（T9 后剩余最坏路径——2 字词走 LIKE 档覆盖不到这条）。"""
    from app.graph_query.search import search_graph_core
    out, dt = _timed(lambda: search_graph_core(terms=["参数说明"]))
    assert dt < 2.0, f"≥3 字宽词耗时 {dt:.2f}s"
    assert out["total"] > 0


def test_rare_multi_metadata_terms_use_fts_under_2s(big):
    """三个罕见元数据词走 MATCH；防每 term 的 ESCAPE LIKE 全表扫描回归。"""
    from app.graph_query.search import search_graph_core
    traced = []
    big.db.set_trace_callback(traced.append)
    try:
        out, dt = _timed(lambda: search_graph_core(
            terms=["CMD 099999", "CMD 088888", "CMD 077777"], match="any"))
    finally:
        big.db.set_trace_callback(None)
    assert dt < 2.0, f"罕见多 term 耗时 {dt:.2f}s"
    assert out["total"] == 3
    metadata_like = [sql.lower() for sql in traced
                     if "metadata_text like" in sql.lower()]
    assert metadata_like and all(" match " in sql for sql in metadata_like)


def test_two_char_term_under_2s(big):
    from app.graph_query.search import search_graph_core
    out, dt = _timed(lambda: search_graph_core(terms=["配额"]))
    assert dt < 2.0, f"两字词耗时 {dt:.2f}s"
    assert out["total"] > 0


def test_search_files_rare_two_char_and_path_traversal(big):
    from app.file_query import search_files_core
    from app.repos import files_repo
    _seed_files_ledger(big)
    assert files_repo.integrity_ok(big.db)
    assert big.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == N
    out, dt = _timed(lambda: search_files_core(query="额管"))
    assert out["total"] == N // RARE_EVERY and dt < 2.0, \
        f"罕见两字词耗时 {dt:.2f}s（total={out['total']}）"

    def _walk():
        after, seen = None, 0
        while True:
            o = search_files_core(ext="md", limit=500, after=after)
            seen += len(o["files"])
            if not o["has_more"]:
                return seen
            after = o["next_cursor"]
    n, dt2 = _timed(_walk)
    expected_md = N - N // RARE_EVERY
    assert n == expected_md and dt2 < 5.0, f"游标遍历 {n} 条 {dt2:.2f}s"


def test_search_files_rare_ext_uses_composite_index_under_2s(big):
    from app.file_query import search_files_core
    _seed_files_ledger(big)
    out, dt = _timed(lambda: search_files_core(ext="drawio"))
    assert out["total"] == N // RARE_EVERY
    assert dt < 2.0, f"稀有 ext 耗时 {dt:.2f}s"
    plan = big.db.execute(
        "EXPLAIN QUERY PLAN SELECT path FROM files "
        "WHERE ext=? AND path>? ORDER BY path LIMIT ?",
        ("drawio", "", 100),
    ).fetchall()
    assert any("idx_files_ext_path" in r[3] for r in plan)
