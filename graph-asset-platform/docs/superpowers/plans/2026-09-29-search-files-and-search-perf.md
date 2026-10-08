# search_files 文件搜索工具 + search_graph 超时治理 实现计划

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增文件维度 MCP 工具 `search_files`（文件名搜索 + 目录浏览，游标分页，扛百万级）+ REST `POST /api/v1/files`；同时治理 search_graph 在内网大数据量下的超时（有界候选池重构）。

**Architecture:** Track A（Chunk 1-2）给 assets/ 建 SQLite "文件户口册"（v14 三表：files / files_fts / files_fts_map），所有平台写路径增量维护 + 首启异步 bootstrap + admin 兜底重建；查询核心 `file_query.search_files_core` 由 MCP 工具与 REST 端点共享（同构对账扩为四对）。Track B（Chunk 3）重构 `graph_query/search.py`：每 term 每来源只取 top-2000 进 Python 合并池、total 封顶、SEARCH_TOO_BROAD 退场、两字词走 trigram 加速 LIKE（meta 表开关可降级 metadata_only）。

**Tech Stack:** Python 3 / FastAPI / SQLite（FTS5 trigram）/ FastMCP / pytest。

**Spec:** `graph-asset-platform/docs/superpowers/specs/2026-09-29-search-files-and-search-perf-design.md`（决策 D1~D7 已拍板；get_md/get_domains 参数与机制**冻结**，只改描述词——见 Chunk 3 Task 12）。

> **2026-09-30 复审勘误：** 下文 Task 9 保留原实施记录；最终公开契约以 spec/接口
> 文档为准：`diagnostics.term_counts` 继续返回整数，新增 `term_stats.{hit,capped}`；
> ≥3 字符元数据改走 FTS，并以 exact → prefix → broad 三段去重候选保证排序承诺。

**工作目录：** 所有命令在 `graph-asset-platform/backend/` 下执行（pytest、uvicorn）。

**⚠️ GIT 陷阱（README）：** 仓库里 `三层图谱构建规范/scripts/product_doc_md_exporter_optimized.py` 长期处于暂存态。**必须**路径限定提交：`git add graph-asset-platform/<具体文件> && git commit -m "..." -- graph-asset-platform/`，绝不用 `git add -A` / `git add .` / `git commit -am`。

**测试基线：** 动手前先跑 `python -m pytest -q` 确认全绿（当前 386+）；每个 Task 结束时保持全绿。

**测试写法总则（本计划所有新测试遵守）：**
- 平台测试惯用 `Service.__new__` 绕过 `__init__` 手工装配（见 `tests/test_fs.py:46` 的 `_setup` / `tests/test_mcp.py:58` 的 `_setup` / `tests/test_api_objects.py` 的 `_setup`——**三个同名不同形**，用哪个文件就抄哪个文件的）。
- conftest 的 autouse fixture 已把 `authenticate` mock 成全权 admin（`test_auth/test_users/test_api_objects/test_telemetry/test_mcp_tools_config` 之外的模块），新测试模块无需带 KEY。
- 涉及 MCP 的用 `tests/test_mcp.py` 的 `_setup/_client/_call/_call_err` 辅助（`_call` 已返回解析后的 content JSON；`_call_err` 返回 error envelope 的原始文本串，需自行 `json.loads`）。
- REST 对账用 `tests/test_mcp_rest_parity.py` 的 `_seed/_mcp_ok/_mcp_err` + 模块级 `client = TestClient(app)` 模式。

---

## Chunk 1: 文件户口册（db v14 + files_repo + 写路径挂钩）

### Task 1: db v14 三表 + files_repo

**Files:**
- Modify: `app/db.py`（SCHEMA_VERSION 13→14 + _SCHEMA 追加三表）
- Create: `app/repos/files_repo.py`
- Test: `tests/test_files_repo.py`

- [ ] **Step 1.1: 写失败测试**

新建 `tests/test_files_repo.py`：

```python
"""files 户口册 repo 单元测试（spec §4.1/§4.2）。"""
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
    files_repo.remove_prefix(conn_db, "Command/UDG")
    assert _count(conn_db, "files") == 1  # 只剩 Command/
    assert _count(conn_db, "files_fts") == 1


def test_rebuild_all_skips_dotfiles_and_includes_dirs(conn_db, store):
    store.write("Command/a.md", "a")
    (tmp_dir_of(store) / ".hidden").write_text("x", encoding="utf-8")
    (tmp_dir_of(store) / "Command" / ".h").write_text("y", encoding="utf-8")
    n = files_repo.rebuild_all(conn_db, store)
    assert n == 2  # Command/ 目录行 + a.md；点文件全跳过
    rows = {r["path"]: r for r in conn_db.execute("SELECT * FROM files")}
    assert rows["Command"]["is_dir"] == 1
    assert rows["Command"]["ext"] == ""


def tmp_dir_of(store):
    return store.root


def test_integrity_ok_detects_drift(conn_db, store):
    store.write("a.md", "a")
    files_repo.rebuild_all(conn_db, store)
    assert files_repo.integrity_ok(conn_db)
    conn_db.execute("DELETE FROM files WHERE path='a.md'")  # 制造漂移
    conn_db.commit()
    assert not files_repo.integrity_ok(conn_db)
```

（`tmp_dir_of` 是给 dotfile 断言取 assets 根的小辅助；实现时直接内联 `store.root` 也行。）

- [ ] **Step 1.2: 跑测试确认失败**

Run: `python -m pytest tests/test_files_repo.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'app.repos.files_repo'`）

- [ ] **Step 1.3: db.py 加 v14 三表**

`app/db.py` 两处修改。第一处：

```python
SCHEMA_VERSION = "14"
```

第二处，`_SCHEMA` 字符串末尾（`idx_objects_domain_scenario` 索引之后）追加：

```sql

-- ============ 文件户口册（v14，search_files 2026-09-29 spec §4.1）============
-- assets 下所有文件 + 目录行（目录行支撑 path 模式 ls 式直接子项浏览）。
-- files_fts.name 存规范化文件名（NFKC→strip→casefold，复用 graph_search_repo
-- 的 normalize_search_text）；files.name 存原样（响应展示）。伴生 map 同
-- md_fts_map/graph_search_map 教训：按 rowid O(1) 删，防批量维护 O(N²)。
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY,          -- 相对 assets 根，正斜杠，磁盘真实大小写
  name TEXT NOT NULL,
  ext  TEXT NOT NULL DEFAULT '',  -- 小写无点；目录恒 ''
  is_dir INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0,   -- 目录恒 0
  mtime REAL NOT NULL DEFAULT 0      -- 目录不随子项变更刷新（仅展示，避免噪音）
) WITHOUT ROWID;

CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  path UNINDEXED, name, tokenize='trigram'
);

CREATE TABLE IF NOT EXISTS files_fts_map(
  path TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL
) WITHOUT ROWID;
```

- [ ] **Step 1.4: 实现 files_repo**

新建 `app/repos/files_repo.py`：

```python
"""files 户口册读写（spec 2026-09-29 §4.1/§4.2）。

- 行 = 文件或目录（is_dir）；path 相对 assets 根、正斜杠、磁盘真实大小写。
- 点文件/点目录不入册（与 store.list_children 一致）。
- 删除一律走 files_fts_map 按 rowid（同 md_fts_map 教训：按 UNINDEXED 列删是
  全 FTS 扫）。map 缺失行回退 path 全扫删（正确性网底）。
- 纯 SQL 函数不 commit（调用方事务内使用）；rebuild_all 自管分块提交。
- ⚠️ 路径计算约定（win_long 的 docstring 禁忌）：目录枚举根与 relative_to 的
  基准必须**同为 win_long 前缀或同为普通路径**——混用会 ValueError。
- integrity_ok 当前仅测试消费（启动对账走「表空→bootstrap」启发，spec §4.2）。
"""
import sqlite3
from pathlib import Path

from ..config import win_long
from .graph_search_repo import normalize_search_text

_CHUNK = 5000


def upsert_entry(conn: sqlite3.Connection, *, path: str, name: str, ext: str,
                 is_dir: int, size: int, mtime: float) -> None:
    """UPSERT 单行（值由调用方给定）+ 同步 FTS（先按 map rowid 删旧行）。"""
    rid = conn.execute(
        "SELECT fts_rowid FROM files_fts_map WHERE path=?", (path,)).fetchone()
    if rid is not None:
        conn.execute("DELETE FROM files_fts WHERE rowid=?", (rid[0],))
    conn.execute(
        "INSERT INTO files(path, name, ext, is_dir, size, mtime) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(path) DO UPDATE SET name=excluded.name, ext=excluded.ext, "
        "is_dir=excluded.is_dir, size=excluded.size, mtime=excluded.mtime",
        (path, name, ext, is_dir, size, mtime))
    cur = conn.execute(
        "INSERT INTO files_fts(path, name) VALUES(?,?)",
        (path, normalize_search_text(name)))
    conn.execute("INSERT OR REPLACE INTO files_fts_map(path, fts_rowid) VALUES(?,?)",
                 (path, cur.lastrowid))


def upsert_from_disk(conn: sqlite3.Connection, store, rel: str) -> None:
    """磁盘 stat 单路径 → UPSERT；磁盘不存在 → 删行（自愈语义，调用方 commit）。"""
    try:
        p = win_long(store.abspath(rel))
        if not p.exists():
            remove_path(conn, rel)
            return
        is_dir = 1 if p.is_dir() else 0
        st = p.stat()
        suffix = Path(rel).suffix.lower()
        upsert_entry(conn, path=rel, name=Path(rel).name,
                     ext=suffix.lstrip(".") if suffix else "",
                     is_dir=is_dir, size=0 if is_dir else st.st_size,
                     mtime=st.st_mtime)
    except (OSError, ValueError):
        remove_path(conn, rel)  # stat 失败/路径非法 → 按不存在处理


def remove_path(conn: sqlite3.Connection, rel: str) -> None:
    """删单行 + FTS（map 命中走 rowid；缺失回退 path 删，正确性网底）。"""
    rid = conn.execute(
        "SELECT fts_rowid FROM files_fts_map WHERE path=?", (rel,)).fetchone()
    if rid is not None:
        conn.execute("DELETE FROM files_fts WHERE rowid=?", (rid[0],))
        conn.execute("DELETE FROM files_fts_map WHERE path=?", (rel,))
    else:
        conn.execute("DELETE FROM files_fts WHERE path=?", (rel,))
    conn.execute("DELETE FROM files WHERE path=?", (rel,))


def remove_prefix(conn: sqlite3.Connection, prefix: str) -> int:
    """删 prefix 目录行自身 + 其下全部行。'/' 的下一码位是 '0'：半开区间覆盖
    prefix/ 下任意 Unicode 文件名（同 service.reindex_prefixes 的技巧）。"""
    low, high = prefix + "/", prefix + "0"
    paths = [r[0] for r in conn.execute(
        "SELECT path FROM files WHERE path>=? AND path<?", (low, high))]
    paths.append(prefix)
    for p in paths:
        remove_path(conn, p)
    return len(paths)


def upsert_tree(conn: sqlite3.Connection, store, rel: str) -> None:
    """rel 自身（文件或目录行）+ 子树全量 UPSERT（回收站还原后重建册用）。"""
    root = win_long(store.abspath(rel))
    if not root.exists():
        remove_path(conn, rel)
        return
    upsert_from_disk(conn, store, rel)
    if not root.is_dir():
        return
    base = win_long(store.root.resolve())  # 与枚举根同为 win_long 前缀
    for p in root.rglob("*"):
        rel_parts = p.relative_to(base).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        try:
            upsert_from_disk(conn, store, "/".join(rel_parts))
        except (OSError, ValueError):
            continue


def rebuild_all(conn: sqlite3.Connection, store, chunk: int = _CHUNK) -> int:
    """全量重建（清空重灌，分块提交——同 graph_search_repo.rebuild_from_objects
    的分块节奏，防长事务饿死独立连接）。返回行数（文件+目录）。"""
    conn.execute("DELETE FROM files")
    conn.execute("DELETE FROM files_fts")
    conn.execute("DELETE FROM files_fts_map")
    conn.commit()
    base = win_long(store.root.resolve())  # 枚举根与 relpath 基准同为前缀形式
    total = 0
    batch: list = []
    for p in base.rglob("*"):
        rel_parts = p.relative_to(base).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        is_dir = 1 if p.is_dir() else 0
        try:
            st = p.stat()
        except OSError:
            continue
        suffix = p.suffix.lower()
        batch.append(("/".join(rel_parts), p.name,
                      suffix.lstrip(".") if suffix else "",
                      is_dir, 0 if is_dir else st.st_size, st.st_mtime,
                      normalize_search_text(p.name)))
        if len(batch) >= chunk:
            _insert_batch(conn, batch)
            total += len(batch)
            batch = []
    if batch:
        _insert_batch(conn, batch)
        total += len(batch)
    return total


def _insert_batch(conn: sqlite3.Connection, batch: list) -> None:
    conn.executemany(
        "INSERT INTO files(path, name, ext, is_dir, size, mtime) VALUES(?,?,?,?,?,?)",
        [b[:6] for b in batch])
    cur_rowid = conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM files_fts").fetchone()[0]
    conn.executemany(
        "INSERT INTO files_fts(path, name) VALUES(?,?)", [(b[0], b[6]) for b in batch])
    conn.execute(
        "INSERT INTO files_fts_map(path, fts_rowid) "
        "SELECT path, rowid FROM files_fts WHERE rowid>?", (cur_rowid,))
    conn.commit()


def integrity_ok(conn: sqlite3.Connection) -> bool:
    """对账：files 与 files_fts 行集合双向一致（缺行/多行都可查出）。"""
    miss = conn.execute(
        "SELECT COUNT(*) FROM (SELECT path FROM files "
        "EXCEPT SELECT path FROM files_fts)").fetchone()[0]
    if miss:
        return False
    extra = conn.execute(
        "SELECT COUNT(*) FROM (SELECT path FROM files_fts "
        "EXCEPT SELECT path FROM files)").fetchone()[0]
    return not extra
```

- [ ] **Step 1.5: 跑测试确认通过**

Run: `python -m pytest tests/test_files_repo.py -q`
Expected: 5 passed

- [ ] **Step 1.6: 全量回归 + 提交**

Run: `python -m pytest -q`（全绿）

```bash
git add graph-asset-platform/backend/app/db.py graph-asset-platform/backend/app/repos/files_repo.py graph-asset-platform/backend/tests/test_files_repo.py
git commit -m "feat: db v14 文件户口册三表 + files_repo（upsert/remove/prefix/rebuild/integrity）" -- graph-asset-platform/
```

---

### Task 2: Service 挂钩——首启 bootstrap + rebuild 追加

**Files:**
- Modify: `app/service.py`（`__init__` 尾部 + 新方法 `_files_bootstrap_async` + `rebuild()`）
- Test: `tests/test_files_repo.py`（追加）

- [ ] **Step 2.1: 写失败测试**

`tests/test_files_repo.py` 追加：

```python
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


def test_files_bootstrap_async(tmp_data_dir):
    s = _bare_service(tmp_data_dir)
    s.store.write("Feature/x/概述.md", "f")
    s.files_building = True
    s._files_bootstrap_async()  # 同步直调（后台线程跑的就是这个函数体）
    assert s.files_building is False
    # Feature + x + 概述.md = 3 行
    assert s.db.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 3
```

- [ ] **Step 2.2: 跑测试确认失败**

Run: `python -m pytest tests/test_files_repo.py -q`
Expected: 新增 2 条 FAIL（`rebuild` 不建 files / 无 `files_building` 属性）

- [ ] **Step 2.3: 实现**

`app/service.py` 三处修改。

① `__init__` **末尾**（`_sync_mtime_async` 线程启动行之后、与 `if not first_time:` 同级）追加——注意用模块头部已 import 的 `threading`（**不要**用 `if` 块内局部 import 的 `_t`，那是块作用域，first_time=True 时会 NameError）：

```python
        # files 户口册首启 bootstrap（v14）：表空 → 后台建册。flag 先置位再起
        # 线程（spec：防构造与线程启动之间的请求窗口看到空表 + false）。
        # assets 为空时建出 0 行册，等价于 spec 的「assets 非空」守卫（无害偏差）。
        self.files_building = False
        if self._table_empty("files"):
            self.files_building = True
            threading.Thread(target=self._files_bootstrap_async,
                             daemon=True).start()
```

② 新方法（放 `_sync_mtime_async` 之后）：

```python
    def _files_bootstrap_async(self) -> None:
        """后台一次性建 files 册（表空时；百万级为分钟级，不阻塞启动）。
        失败不抛（admin 可经 /admin/files-reindex 兜底重试）。"""
        from .repos import files_repo
        try:
            with import_lock:
                n = files_repo.rebuild_all(self.db, self.store)
            print(f"[startup] files 户口册首启建册 {n} 行", flush=True)
        except Exception as e:  # noqa: BLE001 后台线程绝不抛
            print(f"[startup] files 建册失败（admin 可经 /admin/files-reindex 重试）: {e!r}",
                  flush=True)
        finally:
            self.files_building = False
```

③ `rebuild()` 改为：

```python
    def rebuild(self) -> None:
        """全量 reindex 兜底：扫 md 重建 DB + 内存 + files 户口册（手动触发，慢）。"""
        from .migrate import build_index_db
        from .repos import files_repo
        with import_lock:
            build_index_db(self.db, self.store, self.registry)
            files_repo.rebuild_all(self.db, self.store)
            self.index = Index.load_from_db(self.db, self.registry)
```

（副作用说明：`tests/test_mcp.py` 的 `_setup` 调 `s.rebuild()`，从本 Task 起 MCP 测试的 files 表自动有数据——Task 7/8 的测试依赖这一点。）

- [ ] **Step 2.4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_files_repo.py -q && python -m pytest -q`
Expected: 全部 passed

- [ ] **Step 2.5: 提交**

```bash
git add graph-asset-platform/backend/app/service.py graph-asset-platform/backend/tests/test_files_repo.py
git commit -m "feat: Service 挂钩 files 户口册——首启后台 bootstrap + rebuild 连带重建" -- graph-asset-platform/
```

---

### Task 3: fs.py 写端点挂钩

**Files:**
- Modify: `app/routers/fs.py`（put_file / upload / move / rename / delete / trash_restore / mkdir 七处）
- Test: `tests/test_fs.py`（追加）

- [ ] **Step 3.1: 写失败测试**

`tests/test_fs.py` 追加（`_setup`/`CMD` 是该文件既有符号；client 参照该文件现有端点测试的用法——若文件内已有模块级/局部 TestClient 惯例则照抄）：

```python
def test_fs_write_endpoints_sync_files(tmp_data_dir, monkeypatch):
    from fastapi.testclient import TestClient  # 若文件已 import 则不重复
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
```

- [ ] **Step 3.2: 跑测试确认失败**

Run: `python -m pytest tests/test_fs.py -k sync_files -q`
Expected: FAIL（files 表无行——挂钩未实现）

- [ ] **Step 3.3: 实现七处挂钩**

`app/routers/fs.py`：

① 头部 import 行 `from ..repos import trash_repo` 改为：

```python
from ..repos import files_repo, trash_repo
```

② `put_file`：`store.write(path, req.content)` 之后插入（`svc.reindex_path(path)` 之前；其 commit 会连带提交）：

```python
        files_repo.upsert_from_disk(svc.db, svc.store, path)
```

③ `upload`：写循环内 `store.write(target, text)` 之后插入：

```python
            files_repo.upsert_from_disk(svc.db, svc.store, target)
```

④ `move`：`store.write(target, text)` 之后插入：

```python
        files_repo.upsert_from_disk(svc.db, svc.store, target)
        files_repo.remove_path(svc.db, req.src)
```

⑤ `rename`：全库改写循环里 `store.write(rel, nt)` 之后插入：

```python
                files_repo.upsert_from_disk(svc.db, svc.store, rel)
```

以及后半段：`store.write(target, new_text)` 之后插入：

```python
        files_repo.upsert_from_disk(svc.db, svc.store, target)
```

`if target != req.path:` 分支内 `store.delete(req.path)` 之后插入：

```python
            files_repo.remove_path(svc.db, req.path)
```

⑥ `delete_file`：`for rel in md_rels: svc.unindex_path(rel)` 循环之后、`trash_id = store.soft_delete(path)` 之前插入：

```python
        files_repo.remove_prefix(svc.db, path)  # path 自身 + 子树（文件/目录通吃）
```

⑦ `trash_restore`：`store.restore_from_trash(...)` 的 try/except 之后、`for r in store.list_md():` 之前插入：

```python
        files_repo.upsert_tree(svc.db, svc.store, rel)
```

⑧ `mkdir`：函数体改为：

```python
    _require_assets(request)
    svc = get_service()
    svc.store.makedirs(req.path)
    files_repo.upsert_from_disk(svc.db, svc.store, req.path)
    svc.db.commit()
    _record(request, "/fs/mkdir", req.path)
    return {"ok": True, "path": req.path}
```

（已知边界：`store.write` 隐式创建的父目录行要等 bootstrap/admin 重登才入册——按 spec 兜底模型接受；`move`/`upload` 的目标目录若为新建目录同理。）

- [ ] **Step 3.4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_fs.py -q && python -m pytest -q`
Expected: 全部 passed

- [ ] **Step 3.5: 提交**

```bash
git add graph-asset-platform/backend/app/routers/fs.py graph-asset-platform/backend/tests/test_fs.py
git commit -m "feat: /fs 写端点同步 files 户口册（upload/put/move/rename/delete/restore/mkdir）" -- graph-asset-platform/
```

---

### Task 4: pipeline/gate.py 挂钩（apply / cancel / revert）

**Files:**
- Modify: `app/pipeline/gate.py`（三处，都在耐久完成点之前；注意该文件 repos import 是**函数级**的）
- Test: `tests/test_extract_gate.py`（追加）

- [ ] **Step 4.1: 写失败测试**

`tests/test_extract_gate.py` 追加（前置与 `test_cmd_gate_report_then_confirm_overwrite`（该文件 ~173 行起）完全同款：`env` fixture + `env.stub_cmd(extra_binary=True)`；若本地跑发现 awaiting 基线缺 live 文件，按该用例的前置补 `_seed_live` 同款调用）：

```python
def test_gate_apply_and_revert_sync_files(env, monkeypatch):
    fake = env.stub_cmd(extra_binary=True)
    j = env.start(monkeypatch, fake)
    assert j.status == "awaiting", j.error
    r = client.post(f"/api/v1/import/extract/{j.job_id}/confirm",
                    json={"action": "overwrite"})
    assert r.status_code == 200, r.text
    j2 = _get_job(j.job_id)
    assert j2["status"] == "done"
    from app import db as dbmod
    db = dbmod.get_shared_db()
    have = {row["path"] for row in db.execute("SELECT path FROM files").fetchall()}
    # 清单内全部文件（含 _build_manifest sidecar 与 assets/pic.png 二进制）入册
    for row in _rows(j.job_id):
        assert row["path"] in have
    # revert：add → 软删/物理删（行消失）；modify → 还原旧版（行保留）
    from app.pipeline import gate as gate_mod
    gate_mod.revert_job(j.job_id, deleted_by="tester")
    have2 = {row["path"] for row in db.execute("SELECT path FROM files").fetchall()}
    assert not any(p.endswith("ADD NEW.md") for p in have2)
    assert not any("_build_manifest" in p for p in have2)
    assert any(p.endswith("MOD ME.md") for p in have2)
```

- [ ] **Step 4.2: 跑测试确认失败**

Run: `python -m pytest tests/test_extract_gate.py -k sync_files -q`
Expected: FAIL（apply 后 files 表空）

- [ ] **Step 4.3: 实现三处挂钩**

`app/pipeline/gate.py`（该文件的 repos import 是**函数级**——在三个函数各自的 `from ..repos import extract_files_repo` 行追加 `files_repo`；`apply_gate` 若无函数级 repos import，在其使用处上方按同款新增）。

① apply 流（`_apply_gate_locked` 内）：`ix = svc.reindex_paths(rows_map.keys())` 之后、`# 包元信息` 注释之前插入：

```python
    # files 户口册同步（spec §4.2）：按清单行集合自愈式同步（存在→stat 入册，
    # 不存在→删行）。须在耐久完成点 update_job(done) 之前——中途崩溃时重试重跑。
    from ..repos import files_repo
    for rel_full in rows_map:
        files_repo.upsert_from_disk(svc.db, svc.store, rel_full)
    svc.db.commit()
```

② `cancel_gate`：`svc.reindex_paths(r["path"] for r in rows)` 之后（仍在 `with import_lock` 内、`jobs.update_job(job_id, status="cancelled")` 之前）插入（`files_repo` 加入该函数既有局部 import）：

```python
        for r in rows:
            files_repo.upsert_from_disk(svc.db, svc.store, r["path"])
        svc.db.commit()
```

③ `revert_job`：`out["reindex"] = svc.reindex_paths(...)` 之后（仍在 `with import_lock` 内、`jobs.update_job(job_id, status="done", ...)` 之前）插入（局部 import 同②）：

```python
        for r in rows:
            files_repo.upsert_from_disk(svc.db, svc.store, r["path"])
        svc.db.commit()
```

三处都是自愈式 upsert（add 被删/软删 → 磁盘不存在 → 自动删行；modify 还原 → stat 刷新），无需按 op 分支。

- [ ] **Step 4.4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_extract_gate.py -q && python -m pytest -q`
Expected: 全部 passed

- [ ] **Step 4.5: 提交**

```bash
git add graph-asset-platform/backend/app/pipeline/gate.py graph-asset-platform/backend/tests/test_extract_gate.py
git commit -m "feat: 抽取 gate apply/cancel/revert 同步 files 户口册（含非 md sidecar，耐久完成点前）" -- graph-asset-platform/
```

---

## Chunk 2: 查询面（search_files 核心 + MCP 工具 + REST 通道）

### Task 5: admin files-reindex 兜底端点

**Files:**
- Modify: `app/routers/admin.py`
- Test: `tests/test_admin_files_reindex.py`（新建，定此名不再二选一）

- [ ] **Step 5.1: 写失败测试**

新建 `tests/test_admin_files_reindex.py`：

```python
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
```

（conftest 对本模块启用 admin 伪装；skill-only/无权限 403 的鉴权矩阵由 `test_auth.py` 既有用例模式覆盖，本 Task 不重复。）

- [ ] **Step 5.2: 跑测试确认失败**

Run: `python -m pytest tests/test_admin_files_reindex.py -q`
Expected: FAIL（404）

- [ ] **Step 5.3: 实现**

`app/routers/admin.py`：头部 import 改为 `from ..service import get_service, import_lock`，并追加端点：

```python
@router.post("/admin/files-reindex")
def files_reindex(request: Request):
    """全量重建 files 户口册（兜底：外部直拷磁盘后漂移；正常写路径已增量维护）。"""
    _require_admin(request)
    svc = get_service()
    from ..repos import files_repo
    with import_lock:
        n = files_repo.rebuild_all(svc.db, svc.store)
    return {"ok": True, "files": n}
```

- [ ] **Step 5.4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_admin_files_reindex.py -q && python -m pytest -q`
Expected: 全部 passed

- [ ] **Step 5.5: 提交**

```bash
git add graph-asset-platform/backend/app/routers/admin.py graph-asset-platform/backend/tests/test_admin_files_reindex.py
git commit -m "feat: POST /admin/files-reindex 兜底重建 files 户口册" -- graph-asset-platform/
```

---

### Task 6: contracts 扩展 + file_query.search_files_core

**Files:**
- Modify: `app/graph_query/contracts.py`（RestFilesRequest / FileHit / SearchFilesResponse）
- Create: `app/file_query.py`
- Test: `tests/test_file_query.py`

- [ ] **Step 6.1: 写失败测试**

新建 `tests/test_file_query.py`：

```python
"""search_files_core 单元测试（spec §4.3：find/ls 语义 + 游标全量 + 封顶计数）。"""
import pytest

from app.file_query import search_files_core
from app.graph_query.contracts import GraphQueryError

MD = ("---\nid: UDG@MMLCommand@ADD URR\ntype: MMLCommand\nnf: UDG\n"
      "version: 20.15.2\nname: ADD URR\n---\nbody\n")


@pytest.fixture
def populated(tmp_data_dir, monkeypatch):
    import app.service as svc_mod
    from app.repos import files_repo
    s = _bare_service(tmp_data_dir)  # 文件底部定义（下划线开头防误收集为测试）
    monkeypatch.setattr(svc_mod, "_service", s)
    s.store.write("Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md", MD)
    s.store.write("Command/UDG/20.16.0/UDG@MMLCommand@ADD URR.md",
                  MD.replace("20.15.2", "20.16.0"))
    s.store.write("Feature/UDG/F1/概述.md",
                  "---\nid: UDG@Feature@F1\ntype: Feature\nnf: UDG\n---\nf")
    s.store.write_bytes("Feature/UDG/F1/assets/x.png", b"\x89PNG")
    s.reindex_path("Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md")
    s.reindex_path("Command/UDG/20.16.0/UDG@MMLCommand@ADD URR.md")
    s.reindex_path("Feature/UDG/F1/概述.md")
    files_repo.rebuild_all(s.db, s.store)
    return s


def _bare_service(tmp_data_dir):
    import app.service as svc_mod
    from app.store import Store
    import app.db as dbmod
    from app.registry import Registry
    from app.index import Index
    s = svc_mod.Service.__new__(svc_mod.Service)
    s.store = Store(tmp_data_dir)
    s.db = dbmod.get_db(tmp_data_dir.parent / "t.db")
    dbmod.init_schema(s.db)
    s.registry = Registry.load_default()
    s.index = Index.load_from_db(s.db, s.registry)
    s.files_building = False
    return s


def test_query_matches_filename_normalized(populated):
    out = search_files_core(query="add urr")
    assert out["total"] == 2
    assert "Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md" in \
        [f["path"] for f in out["files"]]


def test_query_returns_obj_id_and_version(populated):
    out = search_files_core(query="ADD URR", ext="md")
    by_ver = {f["version"]: f for f in out["files"]}
    assert by_ver["20.15.2"]["obj_id"] == "UDG@MMLCommand@ADD URR"
    # 旧版本目录文件回带自己的 version（get_md 精确读该文件内容的前提）


def test_two_char_query_like_path(populated):
    """2 字符走 trigram 加速 LIKE（spec D6 修订：前缀短语实证否决）。"""
    out = search_files_core(query="概述")
    assert [f["path"] for f in out["files"]] == ["Feature/UDG/F1/概述.md"]


def test_path_direct_children_ls_semantics(populated):
    out = search_files_core(path="Command/UDG")
    assert {f["path"]: f["is_dir"] for f in out["files"]} == {
        "Command/UDG/20.15.2": True, "Command/UDG/20.16.0": True}


def test_path_recursive_files_only(populated):
    out = search_files_core(path="Feature/UDG", recursive=True)
    assert [f["path"] for f in out["files"]] == [
        "Feature/UDG/F1/assets/x.png", "Feature/UDG/F1/概述.md"]  # 字典序+仅文件


def test_cursor_full_traversal(populated):
    seen, after = [], None
    for _ in range(20):
        out = search_files_core(ext="md", limit=1, after=after)
        seen.extend(f["path"] for f in out["files"])
        if not out["has_more"]:
            break
        after = out["next_cursor"]
    assert len(seen) == 3 and len(set(seen)) == 3  # 全量无重复无遗漏


def test_query_matches_dirname_too(populated):
    out = search_files_core(query="F1")
    assert [(f["name"], f["is_dir"]) for f in out["files"]] == [("F1", True)]


def test_short_query_rejected(populated):
    with pytest.raises(GraphQueryError) as ei:
        search_files_core(query="a")
    assert ei.value.error.code == "INVALID_ARGUMENT"


def test_no_filter_rejected(populated):
    with pytest.raises(GraphQueryError) as ei:
        search_files_core()
    assert ei.value.error.code == "INVALID_ARGUMENT"


def test_bad_path_structured_error(populated):
    with pytest.raises(GraphQueryError) as ei:
        search_files_core(path="NoSuchDir")
    assert ei.value.error.code == "INVALID_FILTER"
    assert ei.value.error.details.get("field") == "path"


def test_glob_metachar_dir(populated):
    populated.store.makedirs("Command/weird[1]")
    from app.repos import files_repo
    files_repo.upsert_from_disk(populated.db, populated.store, "Command/weird[1]")
    out = search_files_core(path="Command/weird[1]")  # '[' 已转义，不炸不漏
    assert out["files"] == [] and out["total"] == 0


def test_index_building_flag(populated):
    populated.files_building = True
    try:
        out = search_files_core(query="ADD URR")
        assert out["index_building"] is True
    finally:
        populated.files_building = False
```

- [ ] **Step 6.2: 跑测试确认失败**

Run: `python -m pytest tests/test_file_query.py -q`
Expected: FAIL（`No module named 'app.file_query'`）

- [ ] **Step 6.3: contracts.py 扩展**

`app/graph_query/contracts.py` 末尾追加：

```python
# ---------- search_files 输入/输出（spec 2026-09-29 §4.3） ----------

class RestFilesRequest(RestDomainsRequest):
    """REST /files 请求体（与 MCP search_files 同契约）。query/path/ext 至少
    一个（core 校验）；limit 1~500；after=游标（上一页 next_cursor）。"""

    query: Optional[str] = None
    path: Optional[str] = None
    ext: Optional[str] = None
    recursive: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    after: Optional[str] = None


class FileHit(BaseModel):
    """文件行：目录行 is_dir=true、obj_id/version 为 null（键恒在——MCP 走
    pydantic 补 null、REST 直接返回 dict，两边 wire 必须逐字节同构）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    name: str
    ext: str
    is_dir: bool
    size: int
    mtime: Optional[str] = None
    obj_id: Optional[str] = None
    version: Optional[str] = None


class SearchFilesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[FileHit]
    total: int
    total_is_bounded: bool
    has_more: bool
    next_cursor: Optional[str]
    index_building: bool
    applied_filters: dict
```

- [ ] **Step 6.4: 实现 file_query.py**

新建 `app/file_query.py`：

```python
"""search_files 核心（spec 2026-09-29 §4.3）——文件名搜索 / 目录浏览（find/ls 语义）。

与 graph_query 平行的共享核心：MCP search_files 与 REST POST /files 调用同一
``search_files_core``，错误/护栏不分叉。不搜内容（内容搜索是 search_graph 职责）。

游标=上一页最后一条 path（keyset，path ASC 确定性排序）：深翻页 O(1)、不受并发
增删的页错位影响。total 精确到 ``TOTAL_CAP``（10000），超过置 ``total_is_bounded``。

2 字符 query 走 trigram **加速 LIKE**（SQLite >= 3.45 对 2 字符 LIKE 模式给出索引
计划，3.45.3 实证；低版本为 name 语料扫描——语义正确、速度尽力）。⚠️ 前缀短语
``MATCH '"xx"*'`` 已实证否决（FTS5 trigram MATCH 需 >=3 字符，spec D6 修订）。
"""
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from .graph_query.contracts import INVALID_ARGUMENT, INVALID_FILTER, err
from .repos.graph_search_repo import normalize_search_text
from .service import get_service

TOTAL_CAP = 10_000
MAX_QUERY_LEN = 80


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _glob_escape(prefix: str) -> str:
    """GLOB 元字符转义（[ ] * ? → 字符类）。"""
    out = []
    for ch in prefix:
        out.append(f"[{ch}]" if ch in "[]*?" else ch)
    return "".join(out)


def _fts_phrase(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'


def search_files_core(*, query: Optional[str] = None, path: Optional[str] = None,
                      ext: Optional[str] = None, recursive: bool = False,
                      limit: int = 100, after: Optional[str] = None) -> dict:
    """→ SearchFilesResponse 同构 dict（MCP 包模型 / REST 直返，两边 wire 一致）。"""
    svc = get_service()
    conn = svc.db

    # ---- 校验 ----
    norm_query = normalize_search_text(query) if query else ""
    if query is not None and not norm_query:
        raise err(INVALID_ARGUMENT, "query 不能为空白")
    if norm_query and len(norm_query) < 2:
        raise err(INVALID_ARGUMENT,
                  "文件名搜索词规范化后至少 2 个字符（1 字符无法走索引且无意义）")
    if len(norm_query) > MAX_QUERY_LEN:
        raise err(INVALID_ARGUMENT, f"query 规范化后最长 {MAX_QUERY_LEN} 字符")
    ext_n = (ext or "").strip().lstrip(".").lower() or None
    path_n = (path or "").strip().strip("/") or None
    if not (norm_query or path_n or ext_n):
        raise err(INVALID_ARGUMENT,
                  "query / path / ext 至少给一个：query=按文件名搜；path=列目录；"
                  "组合=交集")
    if path_n is not None:
        ok = conn.execute("SELECT 1 FROM files WHERE path=? AND is_dir=1",
                          (path_n,)).fetchone()
        if ok is None:
            raise err(INVALID_FILTER,
                      f"path 不存在或不是目录: {path_n}（首启建册期间可能未建全，"
                      f"稍后重试或联系管理员执行 files-reindex）",
                      field="path", value=path_n)
    if not isinstance(limit, int) or not (1 <= limit <= 500):
        raise err(INVALID_ARGUMENT, "limit 须在 1~500")

    # ---- WHERE 组装 ----
    where: list = []
    params: list = []
    join_fts = bool(norm_query)
    if norm_query:
        if len(norm_query) >= 3:
            where.append("files_fts MATCH ?")
            params.append(f"name : {_fts_phrase(norm_query)}")
        else:  # 2 字符 → trigram 加速 LIKE（>=3.45 索引支持；低版本 name 语料扫描）
            where.append("files_fts.name LIKE ? ESCAPE '\\'")
            params.append(f"%{_like_escape(norm_query)}%")
    if path_n is not None:
        base = _glob_escape(path_n) + "/"
        where.append("files.path GLOB ?")
        params.append(base + "*")
        if recursive:
            where.append("files.is_dir = 0")  # find -type f 语义
        else:
            where.append("files.path NOT GLOB ?")  # 排除更深层 → 直接子项
            params.append(base + "*/*")
    if ext_n is not None:
        where.append("files.ext = ?")
        params.append(ext_n)
    if after:
        where.append("files.path > ?")
        params.append(after)
    where_sql = " AND ".join(where) if where else "1=1"
    from_sql = ("FROM files JOIN files_fts ON files_fts.path = files.path"
                if join_fts else "FROM files")

    # ---- 计数（封顶） ----
    cnt = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT 1 {from_sql} WHERE {where_sql} "
        f"LIMIT {TOTAL_CAP + 1})", params).fetchone()[0]

    # ---- 取页（limit+1 探 has_more） ----
    rows = conn.execute(
        f"SELECT files.path, files.name, files.ext, files.is_dir, files.size, "
        f"files.mtime, o.id AS obj_id, o.version AS o_version {from_sql} "
        f"LEFT JOIN objects o ON o.source_path = files.path "
        f"WHERE {where_sql} ORDER BY files.path LIMIT ?",
        [*params, limit + 1]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]

    files = []
    for r in rows:
        is_dir = bool(r["is_dir"])
        has_obj = (not is_dir) and r["obj_id"] is not None
        files.append({
            "path": r["path"], "name": r["name"], "ext": r["ext"],
            "is_dir": is_dir, "size": r["size"],
            "mtime": (datetime.fromtimestamp(r["mtime"], tz=timezone.utc)
                      .isoformat(timespec="seconds") if r["mtime"] else None),
            # 键恒在（目录/非对象文件为 None）——MCP/REST wire 同构
            "obj_id": r["obj_id"] if has_obj else None,
            "version": (r["o_version"] or None) if has_obj else None,
        })
    applied = {k: v for k, v in {
        "query": query, "path": path_n, "ext": ext_n,
        "recursive": True if (recursive and path_n) else None}.items()
        if v is not None}
    return {
        "files": files, "total": min(cnt, TOTAL_CAP),
        "total_is_bounded": cnt > TOTAL_CAP,
        "has_more": has_more,
        "next_cursor": rows[-1]["path"] if has_more and rows else None,
        "index_building": bool(getattr(svc, "files_building", False)),
        "applied_filters": applied,
    }
```

- [ ] **Step 6.5: 跑测试确认通过**

Run: `python -m pytest tests/test_file_query.py -q`
Expected: 全部 passed

- [ ] **Step 6.6: 全量回归 + 提交**

Run: `python -m pytest -q`

```bash
git add graph-asset-platform/backend/app/graph_query/contracts.py graph-asset-platform/backend/app/file_query.py graph-asset-platform/backend/tests/test_file_query.py
git commit -m "feat: search_files_core 共享核心 + REST/MCP 契约模型（游标分页/封顶计数/ls+find 语义）" -- graph-asset-platform/
```

---

### Task 7: MCP 工具 search_files 注册

**Files:**
- Modify: `app/mcp_server.py`（新工具 + `_PUBLIC_TOOLS`）
- Test: `tests/test_mcp.py`（追加 + 改既有 tools/list 断言）
- Test: `tests/test_mcp_tools_config.py`（模块常量 `PUBLIC_TOOLS` 扩 4 工具——该文件 6 处精确等值断言都由它派生，不加会红）

- [ ] **Step 7.1: 写失败测试**

`tests/test_mcp.py` 追加（`_setup/_client/_call/_call_err/_CTX/CMD` 均为该文件既有符号；`_setup` 走 `import_bundle + s.rebuild()`，Task 2 起 files 表自动有数据）：

```python
# ---------------- search_files（2026-09-29） ----------------

def test_mcp_search_files_query(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, files={"cmd.md": CMD, "feat.md": FEATURE})
    with _client() as c:
        out = _call(c, "search_files", {**_CTX, "query": "ADD URR"})
    assert out["total"] == 1
    hit = out["files"][0]
    assert hit["obj_id"] == "UDG@MMLCommand@ADD URR"
    assert hit["version"] == "20.15.2"
    assert out["index_building"] is False


def test_mcp_search_files_ls_mode(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, files={"cmd.md": CMD})
    with _client() as c:
        out = _call(c, "search_files", {**_CTX, "path": "Command"})
    assert [(f["name"], f["is_dir"], f["obj_id"]) for f in out["files"]] == \
        [("UDG", True, None)]


def test_mcp_search_files_errors(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch, files={"cmd.md": CMD})
    with _client() as c:
        err1 = json.loads(_call_err(c, "search_files", {**_CTX}))
        err2 = json.loads(_call_err(c, "search_files", {**_CTX, "query": "x"}))
        err3 = json.loads(_call_err(
            c, "search_files", {**_CTX, "query": "ADD URR", "typo": 1}))
    assert err1["error"]["code"] == "INVALID_ARGUMENT"   # 无过滤
    assert err2["error"]["code"] == "INVALID_ARGUMENT"   # 1 字符
    assert err3["error"]["code"] == "INVALID_ARGUMENT"   # 未知参数拦截
```

同时把既有 `test_tools_list_returns_3_public_tools`（~130 行）改为：

```python
def test_tools_list_returns_4_public_tools(tmp_data_dir, monkeypatch):
    """v13 三态迁移 + 2026-09-29 search_files：tools/list 展示 4 个公开工具。"""
    ...（前置不变）...
        names = {t["name"] for t in tools}
        assert names == {"get_domains", "get_md", "search_graph", "search_files"}
```

（函数名同步改；docstring 里的「3 个」表述一并更新。）

同时把 `tests/test_mcp_tools_config.py` 的模块常量（~28 行）改为：

```python
PUBLIC_TOOLS = {"get_domains", "get_md", "search_graph", "search_files"}
```

（`test_get_config_permissions` / `test_visibility_three_states` / `test_patch_unknown_tool_rejected` 的 6 处断言全部由它派生；search_files 默认 visible + docstring 非空，满足该文件的其余断言，无需逐条改。）

- [ ] **Step 7.2: 跑测试确认失败**

Run: `python -m pytest tests/test_mcp.py -k "search_files or tools_list" -q`
Expected: FAIL（工具不存在 / names 断言差 search_files）

- [ ] **Step 7.3: 实现工具注册**

`app/mcp_server.py`：

① import 区：`from .file_query import search_files_core`；既有 contracts import 行补 `SearchFilesResponse`。

② `_PUBLIC_TOOLS` 改为：

```python
_PUBLIC_TOOLS = {"get_domains", "get_md", "search_graph", "search_files"}
```

③ 在 `search_graph` 工具之后注册新工具（描述词=canonical；DEFAULT_INSTRUCTIONS 决策树与三工具边界改写在 Chunk 3 Task 12，本 Task 不动）：

```python
@mcp.tool()
def search_files(
    AGENT_USERNAME: Annotated[AgentUsername, Field(description=_CTX)],
    AGENT_SESSION_ID: Annotated[AgentSessionId, Field(description=_CTX_SID)],
    query: Annotated[Optional[str], Field(
        description="文件名关键词（子串，不分大小写；规范化后至少 2 字符）。"
                    "如 'ADD URR' 命中 UDG@MMLCommand@ADD URR.md")] = None,
    path: Annotated[Optional[str], Field(
        description="目录限定（相对 assets 根，如 'Command/UDG'）。默认列直接子项"
                    "（ls 语义，含子目录行）；recursive=true 时递归取子树全部文件"
                    "（find -type f 语义）。不传=全库")] = None,
    ext: Annotated[Optional[str], Field(
        description="扩展名精确过滤（小写，如 'md'/'png'）")] = None,
    recursive: Annotated[bool, Field(
        description="path 模式下递归子树（仅文件行）；默认 False=直接子项")] = False,
    limit: Annotated[int, Field(
        description="单页条数（1~500，默认 100）", ge=1, le=500)] = 100,
    after: Annotated[Optional[str], Field(
        description="游标：传上一页返回的 next_cursor 翻下一页；循环直到 "
                    "has_more=false 即拿全量")] = None,
    ctx: Context = None) -> SearchFilesResponse:
    """按文件名搜索 / 按目录浏览资产库文件（find/ls 语义，不搜内容）。

    覆盖 assets 下所有文件（含图片等非 md）。三个用法：
    - query='关键词'：全库按文件名搜（等价 find -name '*关键词*'）；
    - path='Command/UDG'：列直接子项（等价 ls）；
    - path + recursive=true：子树全量文件（等价 find <dir> -type f；全量获取用
      after 游标循环翻页直到 has_more=false）。
    按内容搜对象请用 search_graph（本工具不搜正文）。
    md 文件命中回带 obj_id+version——用 get_md(ids=[obj_id], version=version)
    读**该文件**的完整内容（不带 version 会取最新版，可能不是这个文件）；
    非 md 文件（图片等）只有元数据。
    query/path/ext 至少给一个；total 精确到 10000，超过时 total_is_bounded=true。
    index_building=true 表示首启建册未完成，结果不全可稍后重试。
    """
    user = _identity(ctx)
    params = {k: v for k, v in {"query": query, "path": path, "ext": ext,
                                "recursive": recursive, "limit": limit,
                                "after": after}.items() if v is not None}
    try:
        out = search_files_core(query=query, path=path, ext=ext,
                                recursive=recursive, limit=limit, after=after)
        _record_tool("search_files", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params,
                     result={"total": out["total"],
                             "returned": len(out["files"]),
                             "top_paths": [f["path"] for f in out["files"][:10]],
                             "has_more": out["has_more"]})
        return SearchFilesResponse(**out)
    except Exception as e:  # noqa: BLE001 失败也留痕后原样抛出
        _record_tool("search_files", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params,
                     result=_err_summary(e))
        raise
```

- [ ] **Step 7.4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_mcp.py tests/test_mcp_tools_config.py -q && python -m pytest -q`
Expected: 全部 passed（含改写后的 4 工具断言）

- [ ] **Step 7.5: 提交**

```bash
git add graph-asset-platform/backend/app/mcp_server.py graph-asset-platform/backend/tests/test_mcp.py graph-asset-platform/backend/tests/test_mcp_tools_config.py
git commit -m "feat: MCP 新工具 search_files（文件名搜索/目录浏览，游标分页，obj_id 关联 get_md）" -- graph-asset-platform/
```

---

### Task 8: REST POST /api/v1/files + 鉴权中间件

**Files:**
- Modify: `app/middleware/auth.py`（`_GRAPH_API_PATHS` 一行）
- Modify: `app/routers/skill_compat.py`（新端点）
- Test: `tests/test_skill_compat.py` + `tests/test_mcp_rest_parity.py`（各追加）+ `tests/test_auth.py`（扩两处）

- [ ] **Step 8.1: 写失败测试**

`tests/test_skill_compat.py` 追加（`client/_seed/ATTRIBUTION` 为该文件既有符号；seed 经 `_setup`→`import_bundle`→文件名=逻辑ID 归位）：

```python
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
```

`tests/test_mcp_rest_parity.py` 追加第四对（含**目录行 wire 同构**——obj_id/version 键恒在的验收点）：

```python
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
```

`tests/test_auth.py`：在 `test_skill_user_rest_denied_frontend_endpoints`（~49 行）与无 skill 权限 403 的用例（~106 行，`gap_sk` 放行分支）里各补一条 `/api/v1/files` 的同款请求断言——skill 用户带空 body POST → **422 INVALID_ARGUMENT envelope**（中间件放行、路由层参数校验拒绝，同该用例 `r2` 既有形态）；无 skill 用户 → 403 envelope。

- [ ] **Step 8.2: 跑测试确认失败**

Run: `python -m pytest tests/test_skill_compat.py -k files -q && python -m pytest tests/test_mcp_rest_parity.py -k search_files -q`
Expected: FAIL（404）

- [ ] **Step 8.3: 实现**

① `app/middleware/auth.py`：

```python
_GRAPH_API_PATHS = ("/api/v1/domains", "/api/v1/md", "/api/v1/search",
                    "/api/v1/files")
```

② `app/routers/skill_compat.py`：头部 import 加 `from ..file_query import search_files_core`，文件末尾追加：

```python
@router.post("/files")
async def search_files(request: Request):
    """文件名搜索/目录浏览（与 MCP ``search_files`` 完全同构，spec 2026-09-29）。

    请求体 = MCP 工具参数 + 归因字段（query/path/ext/recursive/limit/after +
    AGENT_USERNAME/AGENT_SESSION_ID，extra=forbid；query/path/ext 至少一个）。
    响应与 MCP content JSON 完全相同（SearchFilesResponse）；错误 envelope 同
    /search。打点：1 条 tool 行（caller=skill、endpoint=/files），无逐文件行。
    """
    tel_params: dict = {}
    req = None
    try:
        req = await _parse_body(request, gq.RestFilesRequest)
        tel_params = {k: v for k, v in {
            "query": req.query, "path": req.path, "ext": req.ext,
            "recursive": req.recursive, "limit": req.limit,
            "after": req.after}.items() if v is not None}
        out = search_files_core(query=req.query, path=req.path, ext=req.ext,
                                recursive=req.recursive, limit=req.limit,
                                after=req.after)
    except gq.GraphQueryError as e:
        _record_error("/files", request, req, e, params=tel_params)
        return _error_response(e.error)
    except Exception as e:  # noqa: BLE001
        print(f"[skill_compat] INTERNAL_ERROR /files: {e!r}", flush=True)
        _record_error("/files", request, req, e, params=tel_params)
        return _error_response(gq.GraphError(
            code=gq.INTERNAL_ERROR, message=gq.INTERNAL_ERROR_MESSAGE))
    _record_call("/files", request, req.AGENT_USERNAME, req.AGENT_SESSION_ID,
                 params=tel_params,
                 result={"total": out["total"], "returned": len(out["files"]),
                         "top_paths": [f["path"] for f in out["files"][:10]],
                         "has_more": out["has_more"]})
    return out
```

- [ ] **Step 8.4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_skill_compat.py tests/test_mcp_rest_parity.py tests/test_auth.py -q && python -m pytest -q`
Expected: 全部 passed

- [ ] **Step 8.5: 提交**

```bash
git add graph-asset-platform/backend/app/middleware/auth.py graph-asset-platform/backend/app/routers/skill_compat.py graph-asset-platform/backend/tests/test_skill_compat.py graph-asset-platform/backend/tests/test_mcp_rest_parity.py graph-asset-platform/backend/tests/test_auth.py
git commit -m "feat: REST POST /api/v1/files（与 MCP search_files 同构）+ 鉴权路径分支" -- graph-asset-platform/
```

---

## Chunk 3: Track B — search_graph 超时治理 + 描述词 + 文档

> 本 Chunk 动 search_graph 的**内部实现与部分响应语义**（spec §5），参数形状不变；
> get_md / get_domains 只在 Task 12 改描述词（机制冻结令）。

### Task 9: 有界候选池重构 + total 封顶 + term_counts 新形状 + SEARCH_TOO_BROAD 退场

**Files:**
- Modify: `app/graph_query/search.py`（常量 + 每 term 循环 + 预算删除 + 诊断组装）
- Modify: `app/graph_query/contracts.py`（SearchDiagnostics / SearchGraphResponse）
- Modify: `app/mcp_server.py`（search_graph 打点摘要 1 处）
- Modify: `app/routers/skill_compat.py`（/search 打点摘要 1 处）
- Test: `tests/test_search_graph.py`（更新既有断言 + 新增截断用例）

- [ ] **Step 9.1: 改既有测试 + 写新测试（RED）**

先在 `tests/test_search_graph.py` 里全局搜 `SEARCH_TOO_BROAD` 与 `term_counts`（命令：
`grep -n "SEARCH_TOO_BROAD\|term_counts" tests/test_search_graph.py tests/test_mcp.py tests/test_skill_compat.py tests/test_mcp_rest_parity.py`），
按下述语义改写受影响断言，并追加新用例。**注意**：该文件没有 `populated` fixture（种子是普通辅助函数 `_setup(tmp_data_dir, monkeypatch)` 在各测试体内调用）——追加块开头先补 fixture（后续 Task 10/11 的新用例共用）：

```python
@pytest.fixture
def populated(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)  # 该文件既有的种子辅助（各测试体内同款调用）
    from app.service import get_service
    return get_service()
```

新用例：

```python
def test_broad_term_returns_truncated_not_error(populated):
    """宽泛词不再 SEARCH_TOO_BROAD 报错：正常返回 + total_is_bounded。"""
    out = search_graph_core(terms=["配置"])  # 语料里高频词
    assert out["total"] >= 1
    # 池触顶时（合成小语料可能不触顶——用注入大语料的用例兜底，见 perf 测试）
    if out["total_is_bounded"]:
        assert out["total"] <= 10_000
        assert any("截断" in s for s in out["suggestions"])


def test_term_counts_new_shape(populated):
    out = search_graph_core(terms=["ADD URR", "不存在词xyz"])
    tc = out["diagnostics"]["term_counts"]
    assert tc["ADD URR"] == {"hit": True, "capped": False}
    assert tc["不存在词xyz"] == {"hit": False, "capped": False}


def test_total_is_bounded_field_present(populated):
    out = search_graph_core(terms=["ADD URR"])
    assert isinstance(out["total_is_bounded"], bool)
```

既有断言改写规则：
- `term_counts` 旧形状 `{term: int}` 的断言 → `{"hit": bool, "capped": bool}`；
- 断言 SEARCH_TOO_BROAD 抛错的用例 → 断言正常返回且（若合成语料够大）`total_is_bounded=True`；
- `total` 精确值断言若因池上限变化（合成语料 >2000 时）→ 改为 `>=` 或下调语料。

- [ ] **Step 9.2: 跑测试确认失败**

Run: `python -m pytest tests/test_search_graph.py -q`
Expected: FAIL（无 `total_is_bounded` 字段 / term_counts 形状不符 / TOO_BROAD 仍抛错）

- [ ] **Step 9.3: 实现 search.py 重构**

`app/graph_query/search.py` 四处修改。

① 常量区（`SEARCH_BUDGET = 2_000_000` 整段删除，替换为）；同时把模块 docstring 执行顺序清单第 7 条「预算检查（累计中间命中 > SEARCH_BUDGET → SEARCH_TOO_BROAD，不返回部分结果）」改为「池上限（每 term 每来源 LIMIT POOL_CAP；触顶 → total_is_bounded，不报错）」：

```python
# 候选池上限（每 term 每来源）：排序合并只在小池内做——宽泛词不再把百万行拉回
# Python（2026-09-29 超时治理，spec §5.2）。触顶 → total_is_bounded=true，
# 不再报 SEARCH_TOO_BROAD（错误码保留在 contracts 标注 deprecated-unused）。
POOL_CAP = 2_000
# total 精确计数上限：超过报 10000 + total_is_bounded（与 search_files 同口径）
TOTAL_CAP = 10_000
```

（import 行删掉 `SEARCH_TOO_BROAD`。）

② 每 term 循环重写（`for _disp, norm in norm_terms:` 整段替换；`agg/_entry` 保持，`raw_rows` 与 `row_cap` 删除）：

```python
    agg: dict = {}            # (id, version) → 累积命中
    term_stats: dict = {}     # norm_term → {"hit": bool, "capped": bool}
    any_capped = False

    def _entry(key, row):
        if key not in agg:
            agg[key] = {
                "type": row["type"], "layer": row["layer"], "nf": row["nf"],
                "domain": row["domain"], "scenario": row["scenario"],
                "name": row["name"],
                "terms": set(), "fields": set(), "meta_level": 0,
                "body_terms": set(), "rrf": 0.0,
            }
        return agg[key]

    for _disp, norm in norm_terms:
        seen_keys: set = set()
        capped = False
        # 元数据：规范化后字面包含（trigram 索引加速 LIKE），池内限量
        rows = conn.execute(
            f"SELECT {meta_sel} {from_sql} WHERE 1=1{scope_and} "
            "AND graph_search_fts.metadata_text LIKE ? ESCAPE '\\' "
            f"LIMIT {POOL_CAP}",
            [*params, f"%{_like_escape(norm)}%"]).fetchall()
        if len(rows) >= POOL_CAP:
            capped = True
        for r in rows:
            key = (r["obj_id"], r["version"])
            level, fields = _meta_level(
                norm, normalize_search_text(r["obj_id"]),
                normalize_search_text(r["name"]),
                normalize_search_text(r["name_zh"]))
            e = _entry(key, r)
            e["terms"].add(norm)
            e["fields"] |= fields
            e["meta_level"] = max(e["meta_level"], level)
            seen_keys.add(key)
        # 正文：≥3 走 FTS5 trigram 短语（ORDER BY bm25，1-based rank → RRF），池内限量
        if len(norm) >= 3:
            rows = conn.execute(
                f"SELECT {meta_sel} {from_sql} WHERE 1=1{scope_and} "
                "AND graph_search_fts MATCH ? "
                f"ORDER BY bm25(graph_search_fts) LIMIT {POOL_CAP}",
                [*params, f"body_text : {_fts_phrase(norm)}"]).fetchall()
            if len(rows) >= POOL_CAP:
                capped = True
            for rank, r in enumerate(rows, 1):
                key = (r["obj_id"], r["version"])
                e = _entry(key, r)
                e["terms"].add(norm)
                e["fields"].add("body")
                e["body_terms"].add(norm)
                e["rrf"] += 1.0 / (_RRF_K + rank)
                seen_keys.add(key)
        else:  # <3：LIKE 字面包含（Task 10 将按档分流：1字词跳正文/2字词保持此 LIKE 路径）
            rows = conn.execute(
                f"SELECT {meta_sel} {from_sql} WHERE 1=1{scope_and} "
                "AND graph_search_fts.body_text LIKE ? ESCAPE '\\' "
                f"LIMIT {POOL_CAP}",
                [*params, f"%{_like_escape(norm)}%"]).fetchall()
            if len(rows) >= POOL_CAP:
                capped = True
            for r in rows:
                key = (r["obj_id"], r["version"])
                e = _entry(key, r)
                e["terms"].add(norm)
                e["fields"].add("body")
                e["body_terms"].add(norm)
                seen_keys.add(key)
        term_stats[norm] = {"hit": bool(seen_keys), "capped": capped}
        any_capped = any_capped or capped
```

（原 `term_hits` dict 与循环末的 `if raw_rows > SEARCH_BUDGET: raise ...` 整段删除。）

③ 诊断组装段替换：

```python
    # term_counts（新形状 §5.3）：EXISTS 语义的 hit + 池触顶标记 capped
    term_counts = {disp: term_stats[norm] for disp, norm in norm_terms}
```

（原 `term_counts = {disp: len(term_hits.get(norm, ())) ...}` 行删除；`_probe_without_filters` 仍被 recovery_codes 使用——`REMOVE_OR_REPHRASE_TERM` 的判据从 `c == 0` 改为 `not term_stats[norm]["hit"]`，`USE_MATCH_ANY` 判据同理改 `st["hit"]`。）

④ 建议列表与响应组装：`suggestions` 在 `if total == 0: ... else: ...` 块里构建——**在该块之后**（不是 `total = len(entries)` 之后，那里 suggestions 还未定义，字面照插会 NameError）追加：

```python
    if any_capped:
        suggestions.append("命中量过大已按相关度截断：增加 nf/type/layer 等过滤"
                           "或减少 terms 可提升排序质量")
```

`SearchGraphResponse(...)` 构造加 `total_is_bounded=any_capped,`。

- [ ] **Step 9.4: contracts.py 模型更新**

```python
class TermCountStat(BaseModel):
    """term_counts 新形状（spec §5.3）：hit=EXISTS 探针语义；capped=池触顶。"""

    model_config = ConfigDict(extra="forbid")

    hit: bool
    capped: bool


class SearchDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term_counts: dict[str, TermCountStat]
    recovery_codes: list[str]
```

`SearchGraphResponse` 在 `total: int` 之后加：

```python
    total_is_bounded: bool = False
```

`SEARCH_TOO_BROAD = "SEARCH_TOO_BROAD"` 行注释补 `# deprecated-unused（2026-09-29 超时治理退场，仅保留枚举兼容）`。

- [ ] **Step 9.5: 两处打点摘要适配（dict 与 >0 比较会 TypeError）**

`app/mcp_server.py` search_graph 的 `_record_tool(... result={...})` 里：

```python
"matched_terms_count": sum(
    1 for c in out["diagnostics"]["term_counts"].values() if c["hit"]),
```

`app/routers/skill_compat.py` /search 的 `_record_call(... result={...})` 里同款改 `c["hit"]`。

- [ ] **Step 9.6: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_search_graph.py tests/test_mcp.py tests/test_skill_compat.py tests/test_mcp_rest_parity.py -q && python -m pytest -q`
Expected: 全部 passed（其余模块若还有 term_counts/TOO_BROAD 断言，按 Step 9.1 规则同改）

- [ ] **Step 9.7: 提交**

```bash
git add graph-asset-platform/backend/app/graph_query/search.py graph-asset-platform/backend/app/graph_query/contracts.py graph-asset-platform/backend/app/mcp_server.py graph-asset-platform/backend/app/routers/skill_compat.py graph-asset-platform/backend/tests/test_search_graph.py
git commit -m "perf: search_graph 有界候选池重构——池触顶/total封顶/term_counts新形状/SEARCH_TOO_BROAD退场" -- graph-asset-platform/
```

---

### Task 10: 短词两档（meta 表开关；2 字词=加速 LIKE，1 字词跳正文）

> spec D6 修订（两次实证，T6 代码评审补证）：2 字词正文搜索走 **正文语料 LIKE + POOL_CAP**。
> ⚠️ 两次推翻的方案：①前缀短语 `MATCH '"xx"*'` 在 3.45.3 返回空（trigram MATCH 需 ≥3 字符）；
> ②「2 字符 LIKE 走 trigram 索引」不成立——ESCAPE 子句使优化失效且 <3 字符本就无索引，
> 1M 行实测 250ms 语料级扫描（早期 EQP 文本「INDEX 0:L0」为误读，勿再引用）。因此
> 本 Task 的 LIKE 路径语义正确但**大规模下罕见两字词仍慢**，`metadata_only` 档是主要
> 缓解手段（内网验收超时即切档）；Task 9 已保留的 `<3` LIKE 分支即此路径，本 Task 只加
> 「1 字符跳正文」与「降级开关」。

**Files:**
- Modify: `app/graph_query/search.py`（开关加载 + `<3` 分支按档分流 + 1 字词跳正文）
- Modify: `app/graph_query/contracts.py`（SearchDiagnostics 加 body_skipped_short_terms）
- Test: `tests/test_search_graph.py`（追加）

- [ ] **Step 10.1: 写失败测试（RED）**

```python
def test_two_char_term_body_like_mode(populated):
    """默认 body_like 档：两字词走加速 LIKE，能命中正文（沿用既有 LIKE 行为）。"""
    out = search_graph_core(terms=["配额"])  # 种子正文含「配额管理」（按实际种子改词）
    assert any(h["id"] == "UDG@Feature@GWFD-020300" for h in out["hits"])


def test_two_char_term_metadata_only_mode(populated):
    svc = populated
    svc.db.execute(
        "INSERT INTO meta(key, value) VALUES('search_short_term_mode',"
        "'metadata_only') ON CONFLICT(key) DO UPDATE SET value='metadata_only'")
    svc.db.commit()
    import app.graph_query.search as s
    s._last_good_mode = "body_like"  # 强制下轮重读
    try:
        out = search_graph_core(terms=["配额"])
        assert all(h["id"] != "UDG@Feature@GWFD-020300" for h in out["hits"])
        assert "配额" in out["diagnostics"].get("body_skipped_short_terms", [])
    finally:
        svc.db.execute("DELETE FROM meta WHERE key='search_short_term_mode'")
        svc.db.commit()
        s._last_good_mode = "body_like"


def test_one_char_term_metadata_only(populated):
    out = search_graph_core(terms=["配"])  # 1 字符恒只搜元数据
    assert "配" in out["diagnostics"].get("body_skipped_short_terms", [])
```

（「配额/配」按该文件实际种子正文选词，id 同理。）

- [ ] **Step 10.2: 跑测试确认失败**

Run: `python -m pytest tests/test_search_graph.py -k "term_body_like or metadata_only" -q`
Expected: 新增 3 条 FAIL（无开关/无 body_skipped 字段——第一条可能已绿，属既有行为基线，保留作回归锚）

- [ ] **Step 10.3: 实现**

`app/graph_query/search.py`：

① 模块级新增（import 区之后）：

```python
# 短词两档开关（spec §5.2.4/D6 修订版，meta 表，每请求读取，读失败回退上次
# 成功值——同 mcp_server._load_config_safe 模式）：body_like=两字词正文走
# trigram 加速 LIKE（SQLite >=3.45 索引支持；<3.45 为全库正文扫描——切档保命）；
# metadata_only=两字词只搜元数据。1 字符恒只搜元数据。
SHORT_TERM_META_KEY = "search_short_term_mode"
_last_good_mode = "body_like"


def _load_short_term_mode(conn) -> str:
    global _last_good_mode
    try:
        r = conn.execute(
            "SELECT value FROM meta WHERE key=?", (SHORT_TERM_META_KEY,)).fetchone()
        v = ((r["value"] if r else "") or "body_like").strip()
        if v not in ("body_like", "metadata_only"):
            v = "body_like"
        _last_good_mode = v
        return v
    except Exception:  # noqa: BLE001 配置面故障不放宽也不炸搜索
        return _last_good_mode
```

② `search_graph_core` 里，循环之前取一次：`short_mode = _load_short_term_mode(conn)`；循环前收集器 `body_skipped: list = []`。

③ Task 9 的 `<3` 分支替换为（1 字符跳正文 + 2 字符按档分流）：

```python
        elif len(norm) == 1 or short_mode == "metadata_only":
            # 1 字符（恒跳），或 2 字符 + metadata_only 降级档（慢 SQLite 保命）
            body_skipped.append(_disp)
        else:  # 2 字符 + body_like 档：trigram 加速 LIKE（池内限量，Task 9 语义）
            rows = conn.execute(
                f"SELECT {meta_sel} {from_sql} WHERE 1=1{scope_and} "
                "AND graph_search_fts.body_text LIKE ? ESCAPE '\\' "
                f"LIMIT {POOL_CAP}",
                [*params, f"%{_like_escape(norm)}%"]).fetchall()
            if len(rows) >= POOL_CAP:
                capped = True
            for r in rows:
                key = (r["obj_id"], r["version"])
                e = _entry(key, r)
                e["terms"].add(norm)
                e["fields"].add("body")
                e["body_terms"].add(norm)
                seen_keys.add(key)
```

④ 诊断组装：`diagnostics={"term_counts": ..., "recovery_codes": ...}` 加
`"body_skipped_short_terms": body_skipped`；`contracts.SearchDiagnostics` 加
`body_skipped_short_terms: list[str] = []`。

（`MAX_SHORT_TERMS = 3` 上限保留不动——短词 LIKE 组合数仍需约束。）

- [ ] **Step 10.4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_search_graph.py -q && python -m pytest -q`

- [ ] **Step 10.5: 提交**

```bash
git add graph-asset-platform/backend/app/graph_query/search.py graph-asset-platform/backend/app/graph_query/contracts.py graph-asset-platform/backend/tests/test_search_graph.py
git commit -m "perf: 短词两档——2字词 trigram 加速 LIKE（meta 开关可降级 metadata_only），1字词只搜元数据" -- graph-asset-platform/
```

---

### Task 11: catalog 校验缓存

**Files:**
- Modify: `app/graph_query/catalog.py`（模块级缓存 + invalidate）
- Modify: `app/service.py`（`reload_index`/`rebuild` 失效钩子，函数级 import 防环）
- Test: `tests/test_search_graph.py`（追加）

- [ ] **Step 11.1: 写失败测试（RED）**

```python
def test_catalog_cached_until_invalidate(populated):
    from app.graph_query import catalog
    conn = populated.db
    v1 = catalog.versions(conn)
    conn.execute("INSERT INTO objects(id, version, type, layer, scope, "
                 "source_path, name, frontmatter_json, body_md, raw_md, mtime) "
                 "VALUES('zz@MMLCommand@NEW', '99.0.0', 'MMLCommand', 'Command',"
                 "'nf', 'x.md', 'NEW', '{}', '', '', 0.0)")
    conn.commit()
    assert catalog.versions(conn) == v1            # 未失效 → 命中缓存
    catalog.invalidate()
    assert "99.0.0" in catalog.versions(conn)      # 失效 → 重查
    catalog.invalidate()
```

```python
def test_reload_index_invalidates_catalog(populated):
    import app.graph_query.catalog as catalog
    populated.reload_index()
    assert catalog._cache == {}  # reload（写路径末尾）即失效
```

- [ ] **Step 11.2: 跑测试确认失败**

Run: `python -m pytest tests/test_search_graph.py -k catalog -q`
Expected: FAIL（无 invalidate/缓存行为）

- [ ] **Step 11.3: 实现**

`app/graph_query/catalog.py` 模块级加：

```python
# 目录值缓存（spec §5.2.5：_validate_filters 每请求最多 5 次 DISTINCT 全表扫，
# 大库下不可忽视）。键含 id(conn)——测试 monkeypatch 换连接自动失效。已知脆弱点：
# 连接被 GC 后 id 可能被新连接复用（生产单连接进程级存活，不构成实际风险——
# 此处注释留痕，出现怪异缓存命中优先查这里）。
# 写路径经 service.reload_index()/rebuild() 调 invalidate()。
_cache: dict = {}


def invalidate() -> None:
    _cache.clear()


def _memo(key, conn, fn):
    k = (key, id(conn))
    if k not in _cache:
        _cache[k] = fn()
    return _cache[k]
```

`types/nfs/versions/domains/scenarios` 五个函数体分别包进 `_memo("types", conn, ...)` 等（`layers()` 是常量表不用包）。`app/service.py` 的 `reload_index()` 与 `rebuild()` 末尾加（函数级 import 防环——catalog 顶层 import 了 service.get_service）：

```python
        from .graph_query import catalog as _catalog
        _catalog.invalidate()
```

- [ ] **Step 11.4: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_search_graph.py -q && python -m pytest -q`

- [ ] **Step 11.5: 提交**

```bash
git add graph-asset-platform/backend/app/graph_query/catalog.py graph-asset-platform/backend/app/service.py graph-asset-platform/backend/tests/test_search_graph.py
git commit -m "perf: catalog 动态校验值缓存（reload/rebuild 失效），消每请求 5 次 DISTINCT 扫" -- graph-asset-platform/
```

---

### Task 12: 描述词与决策树修订（get_md/get_domains 仅改词——机制冻结）

**Files:**
- Modify: `app/mcp_server.py`（三个工具 docstring 选段 + DEFAULT_INSTRUCTIONS）
- Test: `tests/test_mcp.py`（描述断言适配）

- [ ] **Step 12.1: 改描述（此 Task 无新测试先行——文案变更，验收=tools/list 断言与全量回归）**

① `get_domains` docstring 第二段「业务域是业务归属的顶层定位层，是任何查询的推荐第一步」改为：

```
业务域是业务归属的顶层定位层，按业务意图定位（找业务方案/场景归属）时的
第一步；按关键词找对象请用 search_graph，按文件名找文件请用 search_files。
```

② `search_graph` docstring 首段后补两句：

```
默认作用于每个 ID 的最新现存版本（version 参数可锁定旧版）。命中量过大时
正常返回按相关度截断的候选（total_is_bounded=true），加过滤词可提升质量。
```

③ `DEFAULT_INSTRUCTIONS` 整体替换：

```python
DEFAULT_INSTRUCTIONS = (
    "三层电信图谱（业务层→任务层→特性层→命令层）查询服务。使用决策树：\n"
    "1. 按业务意图找方案：get_domains 读业务域 md 的全文 [[ID]] 引用后 get_md 下钻。\n"
    "2. 按关键词找对象：search_graph 定位候选（多关键词放 terms 数组，任一命中用 "
    "match=any，全部命中用 match=all）；选定候选后必须 get_md 取完整原文——"
    "snippet 不是权威依据。\n"
    "3. 按文件名找文件 / 列目录：search_files（md 命中回带 obj_id+version，"
    "get_md 读该文件内容；非 md 文件只有元数据；全量获取用 after 游标循环翻页）。\n"
    "4. 参数字段范围：定位 MMLCommand 后 get_md，读取 CommandParameter 段。\n"
    "5. get_md 单项失败不重试整批（失败项回带 available_versions，改版本或移除该 id）。\n"
    "所有搜索与读取默认作用于每个 ID 的最新现存版本（version 参数可锁定旧版）。"
)
```

④ `get_md` docstring 不动机制描述，仅在末尾补一句版本语义：`默认读取每个 id 最新现存版本（version 可全局锁定）。`（纯文案，参数与行为零改动。）

- [ ] **Step 12.2: 全量回归（tools/list 描述断言如断言旧文案则同改）**

Run: `python -m pytest tests/test_mcp.py -q && python -m pytest -q`

- [ ] **Step 12.3: 提交**

```bash
git add graph-asset-platform/backend/app/mcp_server.py graph-asset-platform/backend/tests/test_mcp.py
git commit -m "docs: 三工具边界描述修订 + 决策树补 search_files 支（get_md/get_domains 机制冻结仅改词）" -- graph-asset-platform/
```

---

### Task 13: perf 冒烟测试（slow，环境门控）

**Files:**
- Create: `tests/test_search_perf.py`

- [ ] **Step 13.1: 写测试与实现（合成语料本身就是实现的一部分）**

```python
"""perf 冒烟（GAP_PERF=1 才跑）：合成 10 万对象，宽词 + 两字词断言 < 2s；search_files
罕见两字词 + path 游标遍历同测（T6 评审建议：断言基于计时而非 EQP 文本）。

合成门防回归（CI 可跑）；内网真实量复测属上线 checklist（spec §7）。
"""
import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("GAP_PERF") != "1",
                               reason="perf smoke: set GAP_PERF=1")

N = 100_000


@pytest.fixture(scope="module")
def big(tmp_path_factory):
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
    rows = []
    for i in range(N):
        oid = f"UDG@MMLCommand@CMD {i:06d}"
        body = f"配置命令 {oid} 的参数说明，涉及计费组与配额管理。第{i}条。"
        rows.append((oid, "20.15.2", "MMLCommand", "Command", "nf", f"c/{i}.md",
                     oid, "{}", body, body, 0.0))
    s.db.executemany(
        "INSERT INTO objects(id, version, type, layer, scope, source_path, name,"
        " frontmatter_json, body_md, raw_md, mtime) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    s.db.commit()
    from app.repos import graph_search_repo, object_latest_repo
    graph_search_repo.rebuild_from_objects(s.db)
    object_latest_repo.rebuild(s.db)
    svc_mod._service = s
    yield s
    svc_mod._service = None  # 防止 10 万合成库单例泄漏给后续测试模块


def _timed(fn):
    t0 = time.perf_counter()
    out = fn()
    return out, time.perf_counter() - t0


def test_broad_term_under_2s(big):
    from app.graph_query.search import search_graph_core
    out, dt = _timed(lambda: search_graph_core(terms=["配置"]))
    assert dt < 2.0, f"宽词耗时 {dt:.2f}s"
    assert out["total"] > 0


def test_two_char_term_under_2s(big):
    from app.graph_query.search import search_graph_core
    out, dt = _timed(lambda: search_graph_core(terms=["配额"]))
    assert dt < 2.0, f"两字词耗时 {dt:.2f}s"
    assert out["total"] > 0


def test_search_files_rare_two_char_and_path_traversal(big):
    """T6 评审建议：search_files 也进门控——罕见两字词（语料级扫描路径）+
    path 游标遍历（应索引干净）。合成 files 册（big fixture 的对象表 source_path
    就是文件清单来源——直接 INSERT files 三表或复用 files_repo 造册）。"""
    from app.file_query import search_files_core
    from app.repos import files_repo
    files_repo.rebuild_all(big.db, big.store)
    out, dt = _timed(lambda: search_files_core(query="额管"))
    assert out["total"] > 0 and dt < 2.0, f"罕见两字词耗时 {dt:.2f}s"
    def _walk():
        after, seen = None, 0
        while True:
            o = search_files_core(ext="md", limit=500, after=after)
            seen += len(o["files"])
            if not o["has_more"]:
                return seen
            after = o["next_cursor"]
    n, dt2 = _timed(_walk)
    assert n == 100_000 and dt2 < 5.0, f"游标遍历 {n} 条 {dt2:.2f}s"
```

- [ ] **Step 13.2: 跑一次验证（本机）**

Run: `GAP_PERF=1 python -m pytest tests/test_search_perf.py -q`（Git Bash 语法）
Expected: 2 passed，均 < 2s。失败模式分两种处理：①**超时（>2s 但有结果）**——本机 SQLite <3.45 时 2 字词 LIKE 无索引计划，切 `metadata_only` 档重测（`INSERT INTO meta(key,value) VALUES('search_short_term_mode','metadata_only')`）并记录两档阈值（spec D6 降级路径）；②**total==0**——种子/断言问题，修测试而非切档。
默认（不设 GAP_PERF）跑 `python -m pytest -q` 确认该文件被跳过、全量绿。

- [ ] **Step 13.3: 提交**

```bash
git add graph-asset-platform/backend/tests/test_search_perf.py
git commit -m "test: search_graph perf 冒烟（10万合成对象，宽词/两字词 <2s，GAP_PERF=1 门控）" -- graph-asset-platform/
```

---

### Task 14: 接口文档与配置指南更新

**Files:**
- Modify: `图谱平台接口文档.md`（根目录 `graph-asset-platform/` 下）
- Modify: `docs/MCP配置指南.md`
- Modify: `README.md`（工具数量表述）

- [ ] **Step 14.1: 图谱平台接口文档.md**

新增 `search_files` 章节（MCP + REST `/api/v1/files` 同构），包含：参数表（query/path/recursive/ext/limit/after + 归因字段）、响应示例（含目录行与 obj_id/version 关联语义、`get_md(ids=[obj_id], version=version)` 下钻示例、after 游标全量循环示例）、错误码表（INVALID_ARGUMENT/INVALID_FILTER/INDEX 建册中提示）。同时：

- search_graph 章节补 `total_is_bounded` 字段与 `term_counts` 新形状说明，**分别写明两处封顶口径**：search_graph=候选池 2000/term/来源；search_files=计数 10000（spec §8 item 12 要求）；
- SEARCH_TOO_BROAD 从错误码表移除或标注「已退场（deprecated-unused）」；
- 「三 POST」表述改为「四 POST」。

- [ ] **Step 14.2: docs/MCP配置指南.md 与 README.md**

- MCP配置指南：工具清单 3 → 4（search_files 条目：用途/参数/游标翻页示例）；
- README.md：`3 个公开工具` / `POST /api/v1/domains、POST /api/v1/md、POST /api/v1/search` 等表述补 `/api/v1/files` 与第 4 工具。

- [ ] **Step 14.3: 校对提交**

通读一遍改动段落（口径一致性：四工具/四 POST/双封顶口径）。

```bash
git add graph-asset-platform/图谱平台接口文档.md graph-asset-platform/docs/MCP配置指南.md graph-asset-platform/README.md
git commit -m "docs: 接口文档/配置指南/README 收录 search_files 与超时治理契约变化" -- graph-asset-platform/
```

---

## 完成后

1. 全量 `python -m pytest -q` 绿 + `GAP_PERF=1 python -m pytest tests/test_search_perf.py -q` 绿；
2. 手工冒烟（可选但推荐）：起后端 `python -m uvicorn app.main:app --port 8000`，导入样例 bundle 后用 curl 各打一遍 `POST /api/v1/files`（query / path / recursive+游标翻到 has_more=false）；
3. 内网上线 checklist（spec §7/§9）：sync.sh pack/apply → v14 自动迁移 → 首启后台建册日志确认 → **真实数据量**复测宽词/两字词 <2s（不达标切 `search_short_term_mode=metadata_only`）→ Agent 侧 MCP 配置更新工具清单。
