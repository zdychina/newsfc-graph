"""单例 Service：持有 store / registry / db / index。

启动从 SQLite 加载内存 Index（毫秒~百毫秒级，替代全量 parse md 的 380s）；写操作
增量 UPSERT DB + reload 内存（毫秒级，替代全量 rebuild）。DB 不存在则 migrate 全量
建库（首次启动一次性慢）。

测试隔离：``Service.__new__`` 绕过 ``__init__``，手动建（含 db）指向 tmp 目录。
"""
import threading
from typing import Optional

from .config import ASSETS_DIR
from .db import get_shared_db
from .index import Index
from .registry import Registry
from .store import Store, normalize_relpath

# 模块级写锁：写盘 + DB UPSERT + reload 内存必须串行化（单例跨线程共享）。
import_lock = threading.Lock()


def _commit(db) -> None:
    """提交；容忍「事务已被并发写者抢先提交」。

    共享连接存在少量不持 import_lock 的写者（如 users 管理操作）：它们的
    commit 会连带提交本线程开着的事务，本线程随后 commit 报
    ``cannot commit - no transaction is active``。此时语句均已执行且已落库
    （被抢先提交即已持久化），跳过本次 commit 是安全的；其他错误原样抛。
    （打点已改独立连接根治高频来源，2026-08-25；此处为残余写者兜底。）
    """
    import sqlite3
    try:
        db.commit()
    except sqlite3.OperationalError as e:
        if "no transaction" in str(e).lower():
            print(f"[service] commit 跳过（事务已被并发写者提交）: {e}", flush=True)
            return
        raise


class Service:
    _files_rebuild_state_lock = threading.Lock()

    def __init__(self):
        self.store = Store(ASSETS_DIR)
        self.registry = Registry.load_default()
        self.db = get_shared_db()
        # 首次迁移（DB 空表）：objects 全量 parse；users/telemetry 从旧文件导入
        from .migrate import build_index_db, migrate_users, migrate_telemetry
        first_time = self._table_empty("objects")
        if first_time:
            build_index_db(self.db, self.store, self.registry)
        if self._table_empty("users"):
            migrate_users(self.db)
        if first_time:
            migrate_telemetry(self.db)  # jsonl 历史数据一次性导入
        self.index = Index.load_from_db(self.db, self.registry)
        # FTS 对账状态：True=重建中（search_md 应明确报错而非静默残缺，审查 C3）
        self.fts_rebuilding = False
        # mtime 校验后台异步（21178 文件 stat 在 Windows ~数十秒，不阻塞启动；完成后 reload）
        if not first_time:
            import threading as _t
            _t.Thread(target=self._fts_reconcile_async, daemon=True).start()
            _t.Thread(target=self._sync_mtime_async, daemon=True).start()
        # files 户口册首启 bootstrap（v14）：无 files_bootstrapped 完成标记 → 后台
        # 建册。标记只在 rebuild 成功后同锁写入：表空、或进程被杀留半截册（表非空
        # 但无标记）都会重跑。flag 先置位再起线程（spec：防构造与线程启动之间的
        # 请求窗口看到空表 + false）。assets 为空时建出 0 行册并写标记，等价于
        # spec 的「assets 非空」守卫（无害偏差）。
        self._files_rebuild_active = 0
        self.files_building = False
        if not self.db.execute(
            "SELECT value FROM meta WHERE key='files_bootstrapped'"
        ).fetchone():
            self.files_building = True
            threading.Thread(target=self._files_bootstrap_async,
                             daemon=True).start()

    def _fts_reconcile_async(self) -> None:
        """后台对账三张派生表：md_fts（legacy）+ graph_search_fts + object_latest。
        任一不一致 → 持写锁全量重建（v12 起统一搜索索引同口径维护）。"""
        from .repos import fts_repo, graph_search_repo, object_latest_repo
        try:
            if (fts_repo.integrity_ok(self.db)
                    and graph_search_repo.integrity_ok(self.db)
                    and object_latest_repo.integrity_ok(self.db)):
                return
            self.fts_rebuilding = True
            try:
                with import_lock:
                    # rebuild 分块提交（2026-08-26）：块间释放 WAL 写锁，避免
                    # telemetry/jobs 独立连接被全量重建饿死（database is locked）
                    n1 = fts_repo.rebuild_from_objects(self.db)
                    n2 = graph_search_repo.rebuild_from_objects(self.db)
                    n3 = object_latest_repo.rebuild(self.db)
                print(f"[startup] 派生索引与 objects 不一致，已后台重建 "
                      f"md_fts={n1} / graph_search={n2} / object_latest={n3}",
                      flush=True)
            finally:
                self.fts_rebuilding = False
        except Exception:  # noqa: BLE001 后台线程绝不抛
            self.fts_rebuilding = False

    def _sync_mtime_async(self) -> None:
        """后台 mtime 校验：stat 扫描不取锁（慢但不阻塞写），reindex/reload 取锁（短）。"""
        try:
            changed, deleted = self._scan_mtime_changes()
            if changed or deleted:
                with import_lock:
                    for rel in changed:
                        self.reindex_path(rel)
                    for rel in deleted:
                        self.unindex_path(rel)
                    self.reload_index()
        except Exception:  # noqa: BLE001 后台线程绝不抛
            pass

    def _files_bootstrap_async(self) -> None:
        """后台一次性建 files 册（无完成标记时；百万级为分钟级，不阻塞启动）。
        成功走 ``rebuild_files``（锁内建册 + 写 files_bootstrapped 标记 + 期间置
        files_building）。失败清理由 ``rebuild_files`` 统一保证；本后台
        入口只记录异常，不向线程外抛出。"""
        try:
            n = self.rebuild_files()
            print(f"[startup] files 户口册首启建册 {n} 行", flush=True)
        except Exception as e:  # noqa: BLE001 后台线程绝不抛
            print(f"[startup] files 建册失败（已清残册，下次启动自动重试；admin 可经 /admin/files-reindex 手动触发）: {e!r}",
                  flush=True)

    def _table_empty(self, name: str) -> bool:
        return self.db.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] == 0

    def _scan_mtime_changes(self) -> tuple:
        """扫所有 md 的 stat 比对 DB mtime，返回 (changed_rels, deleted_rels)。

        只读：**一次 SELECT** 拿全部 (source_path, mtime) 建 dict（避免 per-file SELECT
        长时间占用连接、阻塞写操作的 reload/打点）。stat 仍 O(n) 但不持 db 连接。
        """
        disk_md = set(self.store.list_md())
        db_mtime = {r["source_path"]: r["m"] for r in self.db.execute(
            "SELECT source_path, MAX(mtime) AS m FROM objects GROUP BY source_path"
        ).fetchall()}
        changed = []
        for rel in disk_md:
            try:
                d = self.store.abspath(rel).stat().st_mtime
            except OSError:
                continue
            m = db_mtime.get(rel)
            if m is None or abs(d - m) > 0.001:
                changed.append(rel)
        deleted = [p for p in db_mtime if p not in disk_md]
        return changed, deleted

    def _sync_mtime(self) -> None:
        """同步 mtime：reindex 变化 + unindex 删除。**不取锁**——调用方负责（测试或 _sync_mtime_async）。"""
        changed, deleted = self._scan_mtime_changes()
        for rel in changed:
            self.reindex_path(rel)
        for rel in deleted:
            self.unindex_path(rel)

    def reload_index(self) -> None:
        """从 DB 重载内存 Index（写操作末尾调用）。"""
        self.index = Index.load_from_db(self.db, self.registry)
        # catalog 目录值缓存失效（函数级 import 防环：catalog 顶层 import 了
        # 本模块的 get_service）。写路径末尾统一走这里 → 目录值随之失效。
        from .graph_query import catalog as _catalog
        _catalog.invalidate()

    def reindex_path(self, rel: str, *, commit: bool = True) -> None:
        """单文件 parse → UPSERT DB（objects/edges/双 FTS/object_latest）。

        id/type/version 变了的旧节点按 source_path 清除（``objects_repo.delete_by_source``）；
        FTS 旧 (id,version) 行同源删除（不留幽灵命中，审查 C1）；object_latest
        按 ``old_ids ∪ new_ids`` 刷新（v12 统一搜索，§12.1）——刷新在未提交事务
        内执行，不产生额外 commit（reindex_paths 的分块节奏不受影响）。
        """
        rel = normalize_relpath(rel, allow_root=False)
        from .edges import parse_edges
        from .logical_id import split_id
        from .md_parser import parse_md
        from .repos import (edges_repo, fts_repo, graph_search_repo,
                            object_latest_repo, objects_repo)
        # 删旧节点 + 其边 + 双 FTS 旧行（source_path 维度，稳）
        old_pairs = list(objects_repo.delete_by_source(self.db, rel))
        old_keys = {(oid, "" if over is None else over) for oid, over in old_pairs}
        for oid, over in old_pairs:
            edges_repo.delete_for_node(self.db, oid, over)
        fts_repo.delete_many(self.db, old_pairs)
        graph_search_repo.delete_many(self.db, old_pairs)
        affected_ids = {oid for oid, _ in old_pairs}
        # 重新 parse + 入库
        try:
            text = self.store.read(rel)
            fm, body, edge_sec = parse_md(text)
        except Exception:
            object_latest_repo.refresh(self.db, affected_ids)  # 改名/删除场景
            if commit:
                _commit(self.db)
            return
        id_ = fm.get("id")
        typ = fm.get("type")
        if not id_ or not typ or not self.registry.known(typ):
            object_latest_repo.refresh(self.db, affected_ids)
            if commit:
                _commit(self.db)
            return
        try:
            nf, _t, _l = split_id(id_)
        except ValueError:
            object_latest_repo.refresh(self.db, affected_ids)
            if commit:
                _commit(self.db)
            return
        version = fm.get("version")
        normalized_version = "" if version is None else version
        # 同源旧键已在上面删除。仅当另一个 source_path 已占用新键时，才需要
        # 清理它的旧 FTS；全新键不能调用 legacy UNINDEXED 删除，否则每个新
        # 文件都会退化成一次 FTS 全表扫描。
        new_key_existed = self.db.execute(
            "SELECT 1 FROM objects WHERE id=? AND version=? LIMIT 1",
            (id_, normalized_version),
        ).fetchone() is not None
        entry = self.registry.get(typ) or {}
        mtime = self.store.abspath(rel).stat().st_mtime
        edges = list(parse_edges(edge_sec, from_id=id_, from_version=version))
        objects_repo.upsert(
            self.db,
            id=id_, version=version, type=typ,
            layer=entry.get("layer"), scope=entry.get("scope"),
            nf=nf, domain=fm.get("domain"), scenario=fm.get("scenario"),
            source_path=rel, name=fm.get("name"), frontmatter=fm,
            body_md=body, raw_md=text, mtime=mtime,
        )
        edges_repo.replace_for_node(self.db, id_, version, edges)
        # 同源旧键已由 delete_many 删掉，直接 insert-only；若文件改成了
        # 另一个已存键，才单独清理该键，避免 FTS 重复。
        if (id_, normalized_version) not in old_keys and new_key_existed:
            fts_repo.delete(self.db, id_, version)
            graph_search_repo.delete(self.db, id_, version)
        fts_repo.insert(self.db, obj_id=id_, version=version, body=body)
        graph_search_repo.insert(
            self.db, obj_id=id_, version=version, name=fm.get("name"),
            name_zh=fm.get("name_zh"), body_md=body)
        affected_ids.add(id_)
        object_latest_repo.refresh(self.db, affected_ids)
        if commit:
            _commit(self.db)

    def unindex_path(self, rel: str, *, commit: bool = True) -> None:
        """删该 source_path 的 DB 节点 + 边 + 双 FTS 行 + latest 降级（md 被删时）。

        删除当前最新版 → latest 自动降级到次新；删除最后版本 → latest 行消失。
        """
        from .repos import (edges_repo, fts_repo, graph_search_repo,
                            object_latest_repo, objects_repo)
        old_pairs = list(objects_repo.delete_by_source(self.db, rel))
        for oid, over in old_pairs:
            edges_repo.delete_for_node(self.db, oid, over)
        fts_repo.delete_many(self.db, old_pairs)
        graph_search_repo.delete_many(self.db, old_pairs)
        object_latest_repo.refresh(self.db, {oid for oid, _ in old_pairs})
        if commit:
            _commit(self.db)

    def reindex_paths(self, paths, *, chunk_size: int = 250) -> dict:
        """精确对账一组资产路径：存在的 md 重建，已删除的 md 解索引。

        每 ``chunk_size`` 个文件提交一次，既避免逐文件事务，也不长时间
        饿死 jobs/telemetry 的独立 SQLite 写者。
        """
        unique = sorted({normalize_relpath(str(path), allow_root=False)
                         for path in paths
                         if str(path).lower().endswith(".md")})
        if not unique:
            return {"indexed": 0, "removed": 0}
        indexed = removed = pending = 0
        try:
            for rel in unique:
                if self.store.exists(rel):
                    self.reindex_path(rel, commit=False)
                    indexed += 1
                else:
                    self.unindex_path(rel, commit=False)
                    removed += 1
                pending += 1
                if pending >= chunk_size:
                    _commit(self.db)
                    pending = 0
            if pending:
                _commit(self.db)
        except Exception:
            self.db.rollback()
            raise
        self.reload_index()
        return {"indexed": indexed, "removed": removed}

    def reindex_prefixes(self, prefixes: list) -> dict:
        """**按前缀增量索引**（与 reindex_path 同一套 DB/锁/解析语义，目录级）。

        挖掘（自动抽取）/批量覆盖共用；替代全量 rebuild——耗时与**变更量**成正比，
        与库总规模无关（百万级 md 下全量重建不可行）：
        - 只对 mtime 变化或 DB 尚未登记的 md 执行 reindex_path
        - DB 中前缀下已不在磁盘的 source_path → unindex_path（force 清理/删除）
        - 最后 reload_index

        prefixes 如 ``["Command/UDG/20.15.2", "Feature/UDG/20.15.2"]``；
        **调用方须持 import_lock**（与 fs 写端点一致）。返回 {"indexed", "removed"}。
        """
        normalized = sorted({normalize_relpath(p, allow_root=False)
                             for p in prefixes if p and p.strip(" / ")})
        if not normalized:
            return {"indexed": 0, "removed": 0}
        disk = sorted({rel for prefix in normalized
                       for rel in self.store.list_md_under(prefix)})
        disk_set = set(disk)
        db_mtime = {}
        for prefix in normalized:
            # '/' 的下一个码位是 '0'：该半开区间可覆盖 prefix/ 下的任意
            # Unicode 文件名，不受 ``\uffff`` 无法覆盖非 BMP 字符的限制。
            low, high = prefix + "/", prefix + "0"
            rows = self.db.execute(
                "SELECT source_path, MAX(mtime) AS m FROM objects "
                "WHERE source_path>=? AND source_path<? GROUP BY source_path",
                (low, high),
            ).fetchall()
            db_mtime.update({row["source_path"]: row["m"] for row in rows})
        changed = []
        for rel in disk:
            try:
                current = self.store.abspath(rel).stat().st_mtime
            except OSError:
                continue
            previous = db_mtime.get(rel)
            if previous is None or abs(current - previous) > 0.001:
                changed.append(rel)
        deleted = [rel for rel in db_mtime if rel not in disk_set]
        return self.reindex_paths([*changed, *deleted])

    def rebuild(self) -> None:
        """全量 reindex 兜底：扫 md 重建 DB + 内存 + files 户口册（手动触发，慢）。

        files 部分走 ``rebuild_files``（自带锁 + 标记 + files_building），故与
        build_index_db 分两段持锁——import_lock 非重入锁，rebuild_files 不得在
        本方法已持锁时调用。"""
        from .migrate import build_index_db
        with import_lock:
            build_index_db(self.db, self.store, self.registry)
            self.index = Index.load_from_db(self.db, self.registry)
            from .graph_query import catalog as _catalog
            _catalog.invalidate()  # 全量重建后目录值必变——锁内末尾失效
        self.rebuild_files()

    def rebuild_files(self) -> int:
        """全量重建 files 册（admin 端点 / rebuild / bootstrap 三处统一）：
        锁内 rebuild_all + 写 files_bootstrapped 标记，期间置 files_building。
        返回入册行数（文件+目录）。自带 import_lock——调用方不得已持锁（非重入）。"""
        from .repos import files_repo
        n = 0
        succeeded = False
        with self._files_rebuild_state_lock:
            self._files_rebuild_active = \
                getattr(self, "_files_rebuild_active", 0) + 1
            self.files_building = True
        try:
            with import_lock:
                try:
                    # 先持久化撤销完成标记：即使进程在分块重建
                    # 中被杀，下次启动也会自动重试，不会误认半册已就绪。
                    self.db.execute(
                        "DELETE FROM meta WHERE key=?", ("files_bootstrapped",))
                    _commit(self.db)
                    n = files_repo.rebuild_all(self.db, self.store)
                    self.db.execute(
                        "INSERT INTO meta(key, value) VALUES(?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        ("files_bootstrapped", "1"))
                    _commit(self.db)
                    succeeded = True
                except Exception:
                    # rebuild_all 分块提交，因此 rollback 不足以清除半册。
                    # 失败时显式清理三表与 marker，保证不暴露静默残缺结果。
                    self.db.rollback()
                    self.db.execute("DELETE FROM files")
                    self.db.execute("DELETE FROM files_fts")
                    self.db.execute("DELETE FROM files_fts_map")
                    self.db.execute(
                        "DELETE FROM meta WHERE key=?", ("files_bootstrapped",))
                    _commit(self.db)
                    raise
            return n
        finally:
            with self._files_rebuild_state_lock:
                active = max(
                    0, getattr(self, "_files_rebuild_active", 1) - 1)
                self._files_rebuild_active = active
                # 还有排队/执行中的 rebuild，或本次失败无 marker，
                # 都不得向 search_files 宣告索引已就绪。
                self.files_building = active > 0 or not succeeded

    # ---------- 正文全文搜索（MCP search_md 的 service 层实现） ----------

    def search_md(self, q: str, layer=None, type=None, nf=None, version=None,
                  limit: int = 20, offset: int = 0) -> dict:
        """FTS5 trigram 正文搜索：相关度排序 + 高亮片段 + 元数据/版本过滤。

        版本语义：不传 version → 只保留每个 id 的**最新现存版本**命中行（最新版
        不含关键词则该 id 不出现——FTS 按行命中，非"分组后错位取最新"，审查 C6）；
        传 version → 锁定该版本。q<3 字符走 LIKE 路径（trigram 索引加速，无相关度）。

        返回 {total, hits: [{id, type, name, version, score, snippet}]}；
        hits 不含正文全文——召回后调 get_md 取完整 md。
        """
        from .repos import fts_repo
        q = (q or "").strip()
        if not q:
            raise ValueError("查询词不能为空")
        if getattr(self, "fts_rebuilding", False):  # __new__ 绕过 __init__ 的测试实例无此属性
            raise RuntimeError("全文索引重建中，请稍后重试")

        # 命中行（FTS 层，无元数据过滤）→ 内存索引过滤（node 存在 + 元数据 + 版本语义）
        if len(q) >= 3:
            rows = fts_repo.search_match(self.db, q)
            for r in rows:
                r.pop("body", None)
        else:
            raw = fts_repo.search_like(self.db, q)
            rows = [self._like_row_with_snippet(r, q) for r in raw]

        idx = self.index
        # layer 语义与 list_objects 一致：UI 层名（中文）→ 类型集合；type 优先（层内收窄）
        types: Optional[set] = None
        if type:
            types = {type}
        elif layer:
            from .ui_layers import UI_LAYER_TYPES
            types = set(UI_LAYER_TYPES.get(layer, []))
        filtered = []
        for r in rows:
            ver = r["version"] or None
            obj = idx.node(r["obj_id"], ver)
            if obj is None:
                continue  # DB 有行内存无节点（刚删除未 reload）——跳过
            if types is not None and obj.type not in types:
                continue
            if nf and obj.nf != nf:
                continue
            if version is not None:
                if ver != version:
                    continue
            else:
                latest = idx.latest_version_of_id(r["obj_id"])
                if ver != latest:
                    continue
            filtered.append({
                "id": r["obj_id"], "type": obj.type, "name": obj.frontmatter.get("name"),
                "version": ver, "score": r.get("score"), "snippet": r["snippet"],
            })
        total = len(filtered)
        start = max(0, offset)
        return {"total": total, "hits": filtered[start:start + max(1, limit)]}

    @staticmethod
    def _like_row_with_snippet(r: dict, q: str) -> dict:
        """LIKE 路径手工造 snippet（FTS5 snippet() 仅对 MATCH 有效）：48 字符窗口高亮。"""
        body = r.get("body") or ""
        pos = body.find(q)
        if pos < 0:
            snip = body[:48]
        else:
            half = max(0, pos - 20)
            seg = body[half:pos + len(q) + 28]
            snip = ("…" if half > 0 else "") + seg.replace(q, f"【{q}】", 1) + "…"
        return {"obj_id": r["obj_id"], "version": r["version"], "snippet": snip}


_service: Optional[Service] = None


def get_service() -> Service:
    """延迟初始化的全局单例（lifespan 启动时预热）。"""
    global _service
    if _service is None:
        _service = Service()
    return _service
