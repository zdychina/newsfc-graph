"""ImportJob 注册表测试（异步导入后台任务的活状态载体）。"""
import sqlite3
import threading
import time

import pytest

from app.jobs import ImportJob, create_job, get_job, list_jobs, update_job


def test_job_lifecycle():
    j = create_job()
    assert isinstance(j, ImportJob)
    assert j.status == "processing"
    assert get_job(j.job_id).status == "processing"
    update_job(j.job_id, status="done", added=5, updated=1)
    j2 = get_job(j.job_id)
    assert j2.status == "done"
    assert j2.added == 5
    assert j2.updated == 1
    assert j2.finished_at > 0
    assert any(x.job_id == j.job_id for x in list_jobs())


def test_update_unknown_job_is_noop():
    update_job("nonexistent", status="done")  # 不应抛
    assert get_job("nonexistent") is None


def test_failed_sets_error_and_finished_at():
    j = create_job()
    update_job(j.job_id, status="failed", error="boom")
    j2 = get_job(j.job_id)
    assert j2.status == "failed"
    assert j2.error == "boom"
    assert j2.finished_at > 0


def test_summary_round_trips():
    j = create_job()
    s = j.summary()
    assert s["status"] == "processing"
    assert s["job_id"] == j.job_id
    assert "started_at" in s


def test_concurrent_job_reads_and_writes_are_durable(capsys):
    """extract/mine 真实并行时，jobs 共享连接不得竞态丢状态。"""
    from app import jobs

    first = create_job(kind="product_doc_extract", nf="UNC", version="20.16.2")
    second = create_job(kind="product_doc_mine", nf="UNC", version="20.11.2")
    barrier = threading.Barrier(3)
    errors = []

    def writer(job):
        try:
            barrier.wait()
            for value in range(1, 1001):
                update_job(job.job_id, added=value)
            update_job(job.job_id, status="done")
        except BaseException as exc:  # noqa: BLE001 -- 测试必须捕获线程逃逸
            errors.append(exc)

    def reader():
        try:
            barrier.wait()
            for _ in range(1000):
                list_jobs()
                get_job(first.job_id)
                jobs.pending_for("product_doc_mine")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(job,)) for job in (first, second)]
    threads.append(threading.Thread(target=reader))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    jobs._registry.clear()  # 强制只从 DB 恢复，不被内存态掩盖
    assert get_job(first.job_id).status == "done"
    assert get_job(first.job_id).added == 1000
    assert get_job(second.job_id).status == "done"
    assert get_job(second.job_id).added == 1000
    assert "持久化失败" not in capsys.readouterr().err


def test_permanent_persist_failure_does_not_publish_memory_state(monkeypatch):
    """DB 持续不可写时 fail closed：显式报错，内存/DB 都保留旧态。"""
    from app import jobs

    job = create_job(kind="product_doc_extract", nf="UNC", version="20.16.2")
    real_conn = jobs._conn

    class AlwaysLocked:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            return None

    monkeypatch.setattr(jobs, "_conn", AlwaysLocked())
    monkeypatch.setattr(jobs, "_PERSIST_RETRY_DELAYS", (0, 0), raising=False)
    with pytest.raises(jobs.JobPersistenceError):
        update_job(job.job_id, status="done", added=7)

    assert get_job(job.job_id).status == "processing"
    assert get_job(job.job_id).added == 0
    row = real_conn.execute(
        "SELECT status, added FROM import_jobs WHERE job_id=?", (job.job_id,)
    ).fetchone()
    assert tuple(row) == ("processing", 0)


def test_transient_external_writer_lock_is_retried():
    """短时 SQLite 单写者竞争只应延迟状态写，不应丢写。"""
    from app import jobs

    job = create_job(kind="product_doc_extract", nf="UNC", version="20.16.2")
    db_file = jobs._conn.execute("PRAGMA database_list").fetchone()["file"]
    locker = sqlite3.connect(db_file)
    locker.execute("BEGIN IMMEDIATE")
    locker.execute("UPDATE import_jobs SET error='held' WHERE job_id=?", (job.job_id,))
    started = threading.Event()
    errors = []

    def finish_job():
        started.set()
        try:
            update_job(job.job_id, status="done", added=7)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=finish_job)
    thread.start()
    assert started.wait(timeout=2)
    time.sleep(0.05)
    assert thread.is_alive()
    locker.rollback()
    thread.join(timeout=10)
    locker.close()

    assert errors == [] and not thread.is_alive()
    jobs._registry.clear()
    restored = get_job(job.job_id)
    assert restored.status == "done" and restored.added == 7


def test_updated_and_skipped_survive_registry_reload():
    """ImportJob 所有计数字段都必须真正持久化。"""
    from app import jobs

    job = create_job()
    update_job(job.job_id, status="done", added=5, updated=3, skipped=4)
    jobs._registry.clear()
    restored = get_job(job.job_id)
    assert (restored.added, restored.updated, restored.skipped) == (5, 3, 4)


def test_persist_no_transaction_tolerated_as_success(monkeypatch):
    """commit 被并发写者抢先提交（no transaction）→ 按成功处理：不抛、不重试、
    正常发布 _registry（service._commit 同款竞态，2026-09-29 修复锁定）。"""
    from app import jobs

    class StolenCommit:
        def __init__(self):
            self.commits = 0

        def execute(self, *_args, **_kwargs):
            return None

        def commit(self):
            self.commits += 1
            raise sqlite3.OperationalError(
                "cannot commit - no transaction is active")

        def rollback(self):
            return None

    fake = StolenCommit()
    monkeypatch.setattr(jobs, "_conn", fake)
    try:
        j = create_job(kind="product_doc_mine", nf="AMF", version="20.15.2")
        assert fake.commits == 1  # 单次即按成功返回（不走 busy 重试）
        published = get_job(j.job_id)  # registry 命中，不触 DB
        assert published is not None and published.status == "processing"
    finally:
        jobs._registry.pop(j.job_id, None)


def test_persist_busy_commit_still_raises_after_retries(monkeypatch):
    """其他 OperationalError（locked）不受无事务容忍影响：重试耗尽仍显式抛
    JobPersistenceError（fail closed 不降级）。"""
    from app import jobs

    job = create_job(kind="product_doc_mine", nf="UDG", version="20.15.2")

    class LockedCommit:
        def execute(self, *_args, **_kwargs):
            return None

        def commit(self):
            raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            return None

    monkeypatch.setattr(jobs, "_conn", LockedCommit())
    monkeypatch.setattr(jobs, "_PERSIST_RETRY_DELAYS", (0, 0), raising=False)
    try:
        with pytest.raises(jobs.JobPersistenceError):
            update_job(job.job_id, status="done")
    finally:
        jobs._registry.pop(job.job_id, None)
