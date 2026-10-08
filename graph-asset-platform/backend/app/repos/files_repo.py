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
from ..store import normalize_relpath
from .graph_search_repo import normalize_search_text

_CHUNK = 5000


def _norm(rel: str) -> str:
    """入口路径规范化：反斜杠→正斜杠、折叠空段并拒绝点段。"""
    return normalize_relpath(rel)


def _stat_row(rel: str, st, is_dir: int) -> tuple:
    """stat → (name, ext, is_dir, size, mtime) 行值（upsert_from_disk 与
    rebuild_all 共用）。目录 ext 恒 ''、size 恒 0（schema 注释）：Path.suffix
    对 "20.15.2" 这类含点目录名会给 '2'，会污染下游 ext 过滤。"""
    if is_dir:
        return (Path(rel).name, "", is_dir, 0, st.st_mtime)
    suffix = Path(rel).suffix.lower()
    return (Path(rel).name, suffix.lstrip(".") if suffix else "",
            is_dir, st.st_size, st.st_mtime)


def upsert_entry(conn: sqlite3.Connection, *, path: str, name: str, ext: str,
                 is_dir: int, size: int, mtime: float) -> None:
    """UPSERT 单行（值由调用方给定）+ 同步 FTS（先按 map rowid 删旧行；map
    缺失回退 path 删——否则 upsert 造出重复 FTS 行，EXCEPT 对账查不出）。"""
    rid = conn.execute(
        "SELECT fts_rowid FROM files_fts_map WHERE path=?", (path,)).fetchone()
    if rid is not None:
        conn.execute("DELETE FROM files_fts WHERE rowid=?", (rid[0],))
    else:
        conn.execute("DELETE FROM files_fts WHERE path=?", (path,))
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
    """磁盘 stat 单路径 → UPSERT；磁盘不存在 → 删行（自愈语义，调用方 commit）。
    点文件/点目录不入册（与 rebuild_all/store.list_children 口径一致）——已在
    册的点文件（历史脏数据）同样被清。"""
    rel = _norm(rel)
    if any(part.startswith(".") for part in rel.split("/")):
        remove_path(conn, rel)
        return
    try:
        p = win_long(store.abspath(rel))
        if not p.exists():
            remove_path(conn, rel)
            return
        is_dir = 1 if p.is_dir() else 0
        st = p.stat()
        name, ext, _d, size, mtime = _stat_row(rel, st, is_dir)
        upsert_entry(conn, path=rel, name=name, ext=ext,
                     is_dir=is_dir, size=size, mtime=mtime)
    except (OSError, ValueError):
        # OSError 一律按不存在处理（Windows AV 短暂锁文件会误删活行，
        # 下次写/兜底重建自愈——正确性优先的取舍）
        remove_path(conn, rel)  # stat 失败/路径非法 → 按不存在处理


def _in_marks(paths: list) -> str:
    """占位符列表 → "(?,?,…)" IN 子句形（调用方绑同序参数）。"""
    return "(" + ",".join("?" * len(paths)) + ")"


def upsert_many_from_disk(conn: sqlite3.Connection, store, rels,
                          chunk: int = 250) -> dict:
    """批量自愈同步（gate apply/cancel/revert 用）：分块提交 + map 预取 +
    miss 集合单次 DELETE（N 次全扫变 1 次），沿用 reindex_paths 的分块纪律
    （不长时间饿死 jobs/telemetry 独立连接）。返回 {"upserted": n, "removed": n}
    （按分类计数，removed 含本就无行的 miss）。

    语义与逐路径 ``upsert_from_disk`` 一致（存在→stat 入册，不存在/点文件→
    删行）；块内自管提交（容错版 ``_commit``），异常 rollback 后原样抛。
    to_remove 的 ``path IN`` 批删是本函数唯一按 path 扫 FTS 的点（单次/块）；
    upsert 侧旧 FTS 行优先按预取 rowid 删；map 缺失则按 path 批删
    历史脏行，避免追加重复 FTS 记录。
    """
    from ..service import _commit

    unique = list(dict.fromkeys(_norm(r) for r in rels))    # 去重保序
    upserted = removed = 0
    try:
        for i in range(0, len(unique), chunk):
            block = unique[i:i + chunk]
            # map 预取：命中行按 rowid O(1) 删旧 FTS，miss 走 path 批删
            rid_map = {r["path"]: r["fts_rowid"] for r in conn.execute(
                "SELECT path, fts_rowid FROM files_fts_map WHERE path IN "
                + _in_marks(block), block)}
            to_upsert: list = []  # (rel, name, ext, is_dir, size, mtime)
            to_remove: list = []
            for rel in block:
                if any(part.startswith(".") for part in rel.split("/")):
                    to_remove.append(rel)
                    continue
                try:
                    p = win_long(store.abspath(rel))
                    if not p.exists():
                        to_remove.append(rel)
                        continue
                    is_dir = 1 if p.is_dir() else 0
                    name, ext, _d, size, mtime = _stat_row(rel, p.stat(), is_dir)
                    to_upsert.append((rel, name, ext, is_dir, size, mtime))
                except (OSError, ValueError):
                    to_remove.append(rel)
            # 删除侧：三表各一条 path IN 批删（本函数唯一 path 全扫点，单次）
            if to_remove:
                marks = _in_marks(to_remove)
                conn.execute(f"DELETE FROM files_fts WHERE path IN {marks}",
                             to_remove)
                conn.execute(f"DELETE FROM files_fts_map WHERE path IN {marks}",
                             to_remove)
                conn.execute(f"DELETE FROM files WHERE path IN {marks}",
                             to_remove)
                removed += len(to_remove)
            # 插入侧：map 命中按 rowid O(1) 删；map 缺失可能仍有
            # 历史 FTS 行，必须按 path 批删自愈，否则会追加重复行。
            if to_upsert:
                hit_rowids = [rid_map[u[0]] for u in to_upsert if u[0] in rid_map]
                if hit_rowids:
                    conn.execute(
                        "DELETE FROM files_fts WHERE rowid IN "
                        + _in_marks(hit_rowids), hit_rowids)
                miss_paths = [u[0] for u in to_upsert if u[0] not in rid_map]
                if miss_paths:
                    conn.execute(
                        "DELETE FROM files_fts WHERE path IN "
                        + _in_marks(miss_paths), miss_paths)
                conn.executemany(
                    "INSERT INTO files(path, name, ext, is_dir, size, mtime) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET "
                    "name=excluded.name, ext=excluded.ext, "
                    "is_dir=excluded.is_dir, size=excluded.size, "
                    "mtime=excluded.mtime", to_upsert)
                conn.executemany(
                    "INSERT INTO files_fts(path, name) VALUES(?,?)",
                    [(u[0], normalize_search_text(u[1])) for u in to_upsert])
                paths = [u[0] for u in to_upsert]
                conn.execute(
                    "INSERT OR REPLACE INTO files_fts_map(path, fts_rowid) "
                    "SELECT path, rowid FROM files_fts WHERE path IN "
                    + _in_marks(paths), paths)
                upserted += len(to_upsert)
            _commit(conn)
    except Exception:
        conn.rollback()
        raise
    return {"upserted": upserted, "removed": removed}


def upsert_parents_from_disk(conn: sqlite3.Connection, store, rels) -> int:
    """从文件路径集合推导父目录集合（去重，排除根 ''），委托
    ``upsert_many_from_disk`` 批量入册（分块提交 + map 预取 + miss 集合单次批删，
    消除逐目录单行 map-miss 回退扫）。

    gate apply 新建目录（如 Feature 层特性目录）由此入册，否则 path 直接子项
    浏览看不到新特性。返回去重后的父目录数（含各级祖先目录；提交/回滚语义
    继承批量函数：分块自管提交，异常 rollback 后原样抛）。
    """
    parents: set = set()
    for rel in rels:
        parts = _norm(rel).split("/")
        for i in range(1, len(parts)):
            parents.add("/".join(parts[:i]))
    if parents:
        upsert_many_from_disk(conn, store, sorted(parents))
    return len(parents)


def remove_path(conn: sqlite3.Connection, rel: str) -> None:
    """删单行 + FTS（map 命中走 rowid；缺失回退 path 删，正确性网底）。"""
    _remove_raw(conn, _norm(rel))


def _remove_raw(conn: sqlite3.Connection, path: str) -> int:
    """按**存储原始键**删行 + FTS（不过 _norm）。返回 files 实删行数。

    remove_prefix 的区间查询拿到的是存储原键——委托 remove_path 会先规范化，
    尾斜杠等非规范脏行会被转走键而漏删（永久残留）。"""
    rid = conn.execute(
        "SELECT fts_rowid FROM files_fts_map WHERE path=?", (path,)).fetchone()
    if rid is not None:
        conn.execute("DELETE FROM files_fts WHERE rowid=?", (rid[0],))
        conn.execute("DELETE FROM files_fts_map WHERE path=?", (path,))
    else:
        conn.execute("DELETE FROM files_fts WHERE path=?", (path,))
    cur = conn.execute("DELETE FROM files WHERE path=?", (path,))
    return cur.rowcount or 0


def remove_prefix(conn: sqlite3.Connection, prefix: str) -> int:
    """删 prefix 目录行自身 + 其下全部行（返回 files 实删行数）。'/' 的下一码位
    是 '0'：半开区间覆盖 prefix/ 下任意 Unicode 文件名（同 service
    .reindex_prefixes 的技巧）。区间命中的行按存储原始键删（非规范脏行同清）；
    prefix 自身走 _norm 后的规范键。"""
    prefix = _norm(prefix)
    low, high = prefix + "/", prefix + "0"
    paths = [r[0] for r in conn.execute(
        "SELECT path FROM files WHERE path>=? AND path<?", (low, high))]
    paths.append(prefix)
    n = 0
    for p in paths:
        n += _remove_raw(conn, p)
    return n


def upsert_tree(conn: sqlite3.Connection, store, rel: str) -> None:
    """rel 自身（文件或目录行）+ 子树全量 UPSERT（回收站还原后重建册用）。

    先 remove_prefix 清子树旧行再从磁盘重灌：rglob 只枚举磁盘存在项，
    行在册、盘上无对应文件的历史脏行（点文件/幽灵行）不经此清理会永久残留。"""
    rel = _norm(rel)
    root = win_long(store.abspath(rel))
    if not root.exists():
        remove_prefix(conn, rel)
        return
    remove_prefix(conn, rel)
    upsert_from_disk(conn, store, rel)
    if not root.is_dir():
        return
    base = win_long(store.root.resolve())  # 与枚举根同为 win_long 前缀
    for p in root.rglob("*"):
        rel_parts = p.relative_to(base).parts
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
        rel = "/".join(rel_parts)
        name, ext, _d, size, mtime = _stat_row(rel, st, is_dir)
        batch.append((rel, name, ext, is_dir, size, mtime,
                      normalize_search_text(name)))
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
    """对账：files 与 files_fts 行集合双向一致（缺行/多行/重复行都可查出——
    EXCEPT 是集合语义查不出重复，COUNT 感知补位）。"""
    if conn.execute(
            "SELECT 1 FROM files_fts GROUP BY path HAVING COUNT(*)>1 LIMIT 1"
    ).fetchone():
        return False
    miss = conn.execute(
        "SELECT COUNT(*) FROM (SELECT path FROM files "
        "EXCEPT SELECT path FROM files_fts)").fetchone()[0]
    if miss:
        return False
    extra = conn.execute(
        "SELECT COUNT(*) FROM (SELECT path FROM files_fts "
        "EXCEPT SELECT path FROM files)").fetchone()[0]
    return not extra
