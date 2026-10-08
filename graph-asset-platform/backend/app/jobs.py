"""ImportJob：异步导入任务的状态载体（内存缓存 + platform.db 持久化）。

v3（2026-08-19，两步流水线）：
- kind：``product_doc_extract``（解压转换）/ ``product_doc_mine``（抽取任务）；
  两类各自全局单任务互斥、允许并行（用户决策）。
- ``child_pids``：构建子进程 PID 记录——后端被硬杀后孤儿进程继续写资产目录，
  ``sweep_interrupted()`` 启动时按 PID 树终止（评审清单 D16）。
- **独立 SQLite 连接**（评审清单 D7）：后台线程高频落库不再与共享连接跨线程混写；
  WAL 多连接 + busy_timeout。测试通过 monkeypatch ``jobs._conn`` 注入 tmp 连接。
- 历史持久化 / 删除（非 processing）/ 重启清账语义见 v2 注释，不变。

v4（2026-08-26，抽取任务化+入图闸门）：
- status 扩展 ``awaiting``（沙箱构建完成、等用户在闸门三选——**非终态**，
  ``sweep_interrupted`` 只清 processing 不动它，跨重启存活）与 ``cancelled``
  （闸门撤销，终态）。awaiting 期间 kind 互斥已释放。
- 确认/回退为**后台异步执行**（2026-08-27 用户反馈改版）：端点置
  status=processing + result.stage=applying/reverting 即返，BackgroundTask 执行；
  重启对账见 ``gate.reconcile_interrupted``（applying→awaiting 可重试、reverting→done）。
- job.nf/version 对抽取任务存**目标**网元/版本；包身份在 result.bundle_nf/version。
- **全流程串行**（用户决策 2026-08-27）：``pending_for``——存在 processing/awaiting
  即拒绝新抽取（替代先前同目标局部守卫）。
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

from .db import DB_PATH

_KINDS = ("import", "product_doc_extract", "product_doc_mine")


@dataclass
class ImportJob:
    job_id: str
    status: str = "processing"      # processing | awaiting | done | failed | cancelled
    added: int = 0
    updated: int = 0
    skipped: int = 0
    warnings: list = field(default_factory=list)
    error: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    kind: str = "import"
    nf: str = ""
    version: str = ""
    steps: list = field(default_factory=list)      # [{name, status, detail}]
    result: dict = field(default_factory=dict)     # 产物计数（命名避开 summary() 方法）
    child_pids: list = field(default_factory=list)  # 运行中子进程 PID（D16 sweep 终止用）

    def summary(self) -> dict:
        return asdict(self)


_registry: dict[str, ImportJob] = {}
# sqlite3.Connection 即使 check_same_thread=False 也不能被多线程同时调用。
# registry 与 jobs 专用连接共用一把可重入锁，保证“持久化成功后才发布
# 内存快照”，也防止 SELECT/commit 之间的连接竞态。
_lock = threading.RLock()

# 各类任务的全局互斥锁（检查→登记 原子化，修 TOCTOU；评审清单 D6）
_mutex_locks = {k: threading.Lock() for k in _KINDS}


# ---------- 独立连接（D7） ----------

_conn: "sqlite3.Connection | None" = None


class JobPersistenceError(RuntimeError):
    """任务状态无法可靠持久化。

    关键状态不得再静默降级为“仅内存成功”；调用方必须中止后续清理，
    保留 sandbox/manifest 供重试或对账。
    """


# busy_timeout 已在 SQLite 内等待 1s；这里再做有界退避，吸收 WAL 写者
# 刚好连续抢锁的短时饥饿。测试可 monkeypatch 为 (0, 0)。
_PERSIST_RETRY_DELAYS = (0.05, 0.1, 0.2, 0.4, 0.8)

_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS import_jobs(
  job_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
  nf TEXT DEFAULT '', version TEXT DEFAULT '',
  status TEXT NOT NULL, added INTEGER DEFAULT 0,
  updated INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  steps TEXT DEFAULT '[]', result TEXT DEFAULT '{}', warnings TEXT DEFAULT '[]',
  error TEXT DEFAULT '', started_at REAL NOT NULL, finished_at REAL DEFAULT 0,
  child_pids TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS idx_import_jobs_started ON import_jobs(started_at);
"""


def _db() -> sqlite3.Connection:
    """jobs 专用连接（与 service/telemetry 共享连接隔离）；测试注入 ``jobs._conn``。"""
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
            _conn.row_factory = sqlite3.Row
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA synchronous=NORMAL")  # 与 db.get_db 同策略（WAL 免逐条 fsync）
            _conn.execute("PRAGMA busy_timeout=1000")
            _conn.executescript(_TABLE_SQL)
            columns = {row[1] for row in _conn.execute("PRAGMA table_info(import_jobs)")}
            if "updated" not in columns:
                _conn.execute("ALTER TABLE import_jobs ADD COLUMN updated INTEGER DEFAULT 0")
            if "skipped" not in columns:
                _conn.execute("ALTER TABLE import_jobs ADD COLUMN skipped INTEGER DEFAULT 0")
            _conn.commit()
        return _conn


def _is_busy(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in msg or "busy" in msg
    )


def _is_no_transaction(exc: BaseException) -> bool:
    # 该错误仅可能来自 commit()（execute 自带提交检查），此时语句已被并发写者
    # 连带提交落库；共享连接被回滚抢占的理论丢写场景由生产独立连接排除。
    return isinstance(exc, sqlite3.OperationalError) and (
        "no transaction" in str(exc).lower())


def _rollback_quietly(db) -> None:
    try:
        db.rollback()
    except Exception:  # noqa: BLE001 -- 保留原始持久化错误
        pass


def _persist(j: ImportJob) -> None:
    """落库（UPSERT）；locked/busy 有界重试，失败显式抛错。"""
    with _lock:
        db = _db()
        attempts = len(_PERSIST_RETRY_DELAYS) + 1
        for attempt in range(attempts):
            try:
                db.execute(
                    "INSERT INTO import_jobs(job_id,kind,nf,version,status,added,updated,skipped,"
                    "steps,result,warnings,error,started_at,finished_at,child_pids) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(job_id) DO UPDATE SET "
                    "kind=excluded.kind,nf=excluded.nf,version=excluded.version,"
                    "status=excluded.status,added=excluded.added,updated=excluded.updated,"
                    "skipped=excluded.skipped,steps=excluded.steps,result=excluded.result,"
                    "warnings=excluded.warnings,error=excluded.error,"
                    "started_at=excluded.started_at,finished_at=excluded.finished_at,"
                    "child_pids=excluded.child_pids",
                    (j.job_id, j.kind, j.nf, j.version, j.status, j.added,
                     j.updated, j.skipped,
                     json.dumps(j.steps, ensure_ascii=False),
                     json.dumps(j.result, ensure_ascii=False),
                     json.dumps(j.warnings, ensure_ascii=False),
                     j.error, j.started_at, j.finished_at,
                     json.dumps(j.child_pids)))
                db.commit()
                return
            except Exception as exc:  # sqlite 极端竞态曾逃逸 SystemError
                if _is_no_transaction(exc):
                    # 并发写者已抢先提交本连接事务（service._commit 同款竞态，
                    # service.py:22 有完整论证）：语句已执行且已落库，按成功处理。
                    # 测试共连接场景的窗口；生产独立连接不受影响。
                    return
                _rollback_quietly(db)
                if _is_busy(exc) and attempt < attempts - 1:
                    time.sleep(_PERSIST_RETRY_DELAYS[attempt])
                    continue
                print(
                    f"[jobs] CRITICAL 持久化失败 job={j.job_id} "
                    f"status={j.status} attempts={attempt + 1}: "
                    f"{type(exc).__name__}: {exc}",
                    file=sys.stderr,
                )
                raise JobPersistenceError(
                    f"任务 {j.job_id} 状态持久化失败: {exc}"
                ) from exc


def _from_row(r: sqlite3.Row) -> ImportJob:
    keys = set(r.keys())
    return ImportJob(
        job_id=r["job_id"], kind=r["kind"], nf=r["nf"] or "", version=r["version"] or "",
        status=r["status"], added=r["added"] or 0,
        updated=(r["updated"] or 0) if "updated" in keys else 0,
        skipped=(r["skipped"] or 0) if "skipped" in keys else 0,
        steps=json.loads(r["steps"] or "[]"),
        result=json.loads(r["result"] or "{}"),
        warnings=json.loads(r["warnings"] or "[]"),
        error=r["error"] or "", started_at=r["started_at"], finished_at=r["finished_at"] or 0.0,
        child_pids=json.loads(r["child_pids"] or "[]"),
    )


def _clone(j: ImportJob) -> ImportJob:
    return ImportJob(**asdict(j))


def acquire_mutex(kind: str) -> bool:
    """取该类任务的全局锁（检查→登记 原子段入口）。取不到立即返回 False。"""
    lk = _mutex_locks.get(kind)
    return lk.acquire(blocking=False) if lk else True


def release_mutex(kind: str) -> None:
    lk = _mutex_locks.get(kind)
    if lk:
        try:
            lk.release()
        except RuntimeError:
            pass


def create_job(kind: str = "import", nf: str = "", version: str = "") -> ImportJob:
    """新建 job：DB commit 成功后才发布到内存 registry。"""
    j = ImportJob(job_id=uuid.uuid4().hex[:12], kind=kind, nf=nf, version=version)
    with _lock:
        _persist(j)
        _registry[j.job_id] = j
        return _clone(j)


def update_job(jid: str, **kw) -> None:
    """更新任务字段并落库。**DB-only 任务（重启后）先载入再更新**——awaiting 跨
    重启存活，闸门确认/回退必须仍能推进其状态（2026-08-27 修复：先前对 DB-only
    任务静默 no-op，导致重启后确认入库而任务永远停在 awaiting）。"""
    with _lock:
        j = _registry.get(jid)
        if j is None:
            try:
                row = _db().execute(
                    "SELECT * FROM import_jobs WHERE job_id=?", (jid,)).fetchone()
            except sqlite3.Error:
                return
            if row is None:
                return
            j = _from_row(row)
        next_values = asdict(j)
        for k, v in kw.items():
            if k in next_values:
                next_values[k] = v
        if kw.get("status") in ("done", "failed", "cancelled"):
            next_values["finished_at"] = time.time()
        next_job = ImportJob(**next_values)
        _persist(next_job)
        _registry[jid] = next_job


def get_job(jid: str) -> Optional[ImportJob]:
    with _lock:
        j = _registry.get(jid)
        if j is not None:
            return _clone(j)
        try:
            row = _db().execute(
                "SELECT * FROM import_jobs WHERE job_id=?", (jid,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        loaded = _from_row(row)
        _registry[jid] = loaded
        return _clone(loaded)


def list_jobs(limit: int = 100) -> list:
    with _lock:
        try:
            rows = _db().execute(
                "SELECT * FROM import_jobs ORDER BY started_at DESC LIMIT ?", (limit,)
            ).fetchall()
        except sqlite3.Error:
            return [_clone(j) for j in sorted(
                _registry.values(), key=lambda item: item.started_at, reverse=True
            )[:limit]]
        out = []
        for row in rows:
            loaded = _from_row(row)
            _registry[loaded.job_id] = loaded
            out.append(_clone(loaded))
        return out


def delete_job(jid: str) -> bool:
    """删除历史任务（**仅非 processing/awaiting**——进行中或待闸门确认不可删，
    用户决策；awaiting 需先在闸门三选落地）。"""
    with _lock:
        j = _registry.get(jid)
        if j is not None and j.status in ("processing", "awaiting"):
            return False
        try:
            db = _db()
            row = db.execute("SELECT status FROM import_jobs WHERE job_id=?", (jid,)).fetchone()
            if row and row["status"] in ("processing", "awaiting"):
                return False
            cur = db.execute("DELETE FROM import_jobs WHERE job_id=?", (jid,))
            db.commit()
        except sqlite3.Error:
            return False
        if cur.rowcount == 0 and j is None:
            return False
        _registry.pop(jid, None)
        return True


def has_processing(kind: str) -> Optional[ImportJob]:
    with _lock:
        for j in _registry.values():
            if j.kind == kind and j.status == "processing":
                return _clone(j)
        try:
            row = _db().execute(
                "SELECT * FROM import_jobs WHERE kind=? AND status='processing' "
                "ORDER BY started_at DESC LIMIT 1", (kind,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        loaded = _from_row(row)
        _registry[loaded.job_id] = loaded
        return _clone(loaded)


def pending_for(kind: str) -> Optional[ImportJob]:
    """该 kind 是否存在未完结任务（processing[含入库/回退执行中]或 awaiting）——
    抽取全流程**串行守卫**（用户决策 2026-08-27：上一任务未入库完结不发起新抽取，
    替代先前仅同目标的局部守卫）。"""
    with _lock:
        for j in _registry.values():
            if j.kind == kind and j.status in ("processing", "awaiting"):
                return _clone(j)
        try:
            row = _db().execute(
                "SELECT * FROM import_jobs WHERE kind=? AND status IN ('processing','awaiting') "
                "ORDER BY started_at DESC LIMIT 1", (kind,)).fetchone()
        except sqlite3.Error:
            return None
        if row is None:
            return None
        loaded = _from_row(row)
        _registry[loaded.job_id] = loaded
        return _clone(loaded)


def recent_done(kind: str, limit: int = 200) -> list:
    """最近完成（含已回退标记）的任务，finished_at 倒序——抽取任务重跑检索用
    （如 feature 任务找同目标最近一次成功 cmd 任务记录的源目录）。"""
    with _lock:
        try:
            rows = _db().execute(
                "SELECT * FROM import_jobs WHERE kind=? AND status='done' "
                "ORDER BY finished_at DESC LIMIT ?", (kind, limit)).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            loaded = _from_row(row)
            _registry[loaded.job_id] = loaded
            out.append(_clone(loaded))
        return out


def find_jobs(kind: str = "", statuses: tuple[str, ...] = (), limit: int = 10000) -> list:
    """按 kind/status 读取持久化快照，供启动/在线对账使用。"""
    clauses, params = [], []
    if kind:
        clauses.append("kind=?")
        params.append(kind)
    if statuses:
        clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
        params.extend(statuses)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _lock:
        try:
            rows = _db().execute(
                f"SELECT * FROM import_jobs{where} ORDER BY started_at DESC LIMIT ?",
                (*params, limit),
            ).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            loaded = _from_row(row)
            _registry[loaded.job_id] = loaded
            out.append(_clone(loaded))
        return out


def mutex_locked(kind: str) -> bool:
    lock = _mutex_locks.get(kind)
    return lock.locked() if lock else False


def _kill_pid_tree(pid: int) -> None:
    """尽力终止进程树（Windows taskkill /T；POSIX SIGKILL）。"""
    if not isinstance(pid, int) or pid <= 0:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                           capture_output=True, timeout=10)
        else:
            os.kill(pid, signal.SIGKILL)
    except Exception:  # noqa: BLE001 —— 尽力而为，失败不阻断清账
        pass


def sweep_interrupted(kinds: tuple[str, ...] = ()) -> int:
    """启动清账：①终止上个进程遗留的子进程树（D16）→ ②processing 标记 failed。
    幂等；返回清账条数。"""
    with _lock:
        try:
            db = _db()
            where = "status='processing'"
            params: list = []
            if kinds:
                where += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
                params.extend(kinds)
            rows = db.execute(
                f"SELECT job_id, child_pids FROM import_jobs WHERE {where}", params
            ).fetchall()
            for row in rows:
                for pid in json.loads(row["child_pids"] or "[]"):
                    _kill_pid_tree(int(pid))
            now = time.time()
            cur = db.execute(
                f"UPDATE import_jobs SET status='failed', finished_at=?, child_pids='[]', "
                "error=COALESCE(NULLIF(error,''), '后端重启或后台线程中断；"
                "已写入的资产保留，可覆盖重建续跑') "
                f"WHERE {where}", (now, *params))
            db.commit()
            n = cur.rowcount
            for row in rows:
                cached = _registry.get(row["job_id"])
                if cached is not None:
                    values = asdict(cached)
                    values.update(status="failed", finished_at=now, child_pids=[])
                    if not values["error"]:
                        values["error"] = "后端重启或后台线程中断；已写入的资产保留，可覆盖重建续跑"
                    _registry[row["job_id"]] = ImportJob(**values)
            return n
        except sqlite3.Error:
            return 0
