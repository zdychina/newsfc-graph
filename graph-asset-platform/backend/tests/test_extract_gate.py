"""抽取任务化·入图闸门全流程测试（2026-08-26）：沙箱构建 → awaiting 报告 →
confirm（覆盖/只新增）→ 产物清单/索引 → revert（sha 守卫）→ cancel/sweep。

`_run` 打桩按 argv 里的 --storage 写**沙箱**（同时验证沙箱契约：脚本只写沙箱、
--nf/--version 用目标值、mml 多目录单次调用）。真实构建脚本流程靠手工验收。
"""
import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.pipeline.runner as pl
from app.main import app
from app.pipeline import gate

client = TestClient(app)

MML_REL = "Prod_CH_20.15.2/OM参考/命令/UDG MML命令"
FEAT_REL = "Prod_CH_20.15.2/特性部署/特性指南/UDG特性指南"

CMD_MD = (
    "---\n"
    "id: {nf}@MMLCommand@{name}\n"
    "type: MMLCommand\n"
    "nf: {nf}\n"
    "version: {ver}\n"
    "name: {name}\n"
    "---\n"
    "# {name}\n\n正文 {name}\n"
)


def _make_bundle(out: Path) -> None:
    root = out / "UDG_20.15.2" / "Prod_CH_20.15.2"
    (root / "OM参考" / "命令" / "UDG MML命令").mkdir(parents=True)
    (root / "特性部署" / "特性指南" / "UDG特性指南").mkdir(parents=True)
    (root / "特性部署" / "特性指南" / "UDG License描述").mkdir(parents=True)


class _Env:
    """每用例环境：tmp DATA/ASSETS/OUTPUT + 已解压包 + jobs 清空。"""

    def __init__(self, tmp_path, tmp_data_dir, monkeypatch):
        import app.config as config
        out = tmp_path / "pd" / "output"
        monkeypatch.setattr(config, "OUTPUT_DIR", out)
        monkeypatch.setattr(config, "DATA_DIR", out.parent)
        monkeypatch.setattr(config, "ASSETS_DIR", tmp_data_dir)
        self.assets = tmp_data_dir
        self.data = out.parent
        _make_bundle(out)
        from app import jobs as jobs_mod
        jobs_mod._registry.clear()
        self.calls: list = []

    # ---- 桩：按 --storage 写沙箱（cmd 场景三文件 + 清单） ----
    def stub_cmd(self, *, new=("ADD NEW",), same=("ADD SAME",),
                 mod=(("MOD ME", "旧正文"),), extra_binary=False):
        """live 预置 same/mod 的旧内容（same 与沙箱一致；mod 与沙箱不同）。"""
        cmd_live = self.assets / "Command" / "UDG" / "20.15.2"
        cmd_live.mkdir(parents=True, exist_ok=True)
        for name in same:
            (cmd_live / f"UDG@MMLCommand@{name}.md").write_text(
                CMD_MD.format(nf="UDG", ver="20.15.2", name=name), encoding="utf-8")
        for name, old in mod:
            (cmd_live / f"UDG@MMLCommand@{name}.md").write_text(
                CMD_MD.format(nf="UDG", ver="20.15.2", name=name) + f"{old}\n", encoding="utf-8")
        if extra_binary:
            (cmd_live / "assets").mkdir(exist_ok=True)
            (cmd_live / "assets" / "pic.png").write_bytes(b"\x89PNG-old")

        env = self

        def fake_run(script, *args, job_id=""):
            name = Path(str(script)).name
            argv = [str(a) for a in args]
            storage = Path(argv[argv.index("--storage") + 1])
            env.calls.append({"script": name, "storage": storage, "argv": argv})
            nf = argv[argv.index("--nf") + 1]
            ver = argv[argv.index("--version") + 1]
            if name == "build_commands.py":
                d = storage / "Command" / nf / ver
                d.mkdir(parents=True, exist_ok=True)
                for nm in new:
                    (d / f"{nf}@MMLCommand@{nm}.md").write_text(
                        CMD_MD.format(nf=nf, ver=ver, name=nm), encoding="utf-8")
                for nm in same:
                    (d / f"{nf}@MMLCommand@{nm}.md").write_text(
                        CMD_MD.format(nf=nf, ver=ver, name=nm), encoding="utf-8")
                for (nm, _old) in mod:
                    (d / f"{nf}@MMLCommand@{nm}.md").write_text(
                        CMD_MD.format(nf=nf, ver=ver, name=nm) + "新正文\n", encoding="utf-8")
                if extra_binary:
                    (d / "assets").mkdir(exist_ok=True)
                    (d / "assets" / "pic.png").write_bytes(b"\x89PNG-new-longer")
                (d / "_build_manifest.json").write_text(
                    json.dumps({"command_count": len(new) + len(same) + len(mod)}), encoding="utf-8")
            elif name == "build_configobjects.py":
                d = storage / "ConfigObject" / nf / ver
                d.mkdir(parents=True, exist_ok=True)
                (d / "_build_manifest.json").write_text(
                    json.dumps({"object_count": 0}), encoding="utf-8")
            elif name == "build_features.py":
                d = storage / "Feature" / nf / ver
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{nf}@Feature@GWFD-01").mkdir()
                (d / f"{nf}@Feature@GWFD-01" / "概述.md").write_text(
                    "# 概述\n特性正文\n", encoding="utf-8")
                (d / "_build_manifest.json").write_text(
                    json.dumps({"feature_count": 1}), encoding="utf-8")
            return ""
        return fake_run

    def stub_feature_only(self):
        env = self

        def fake_run(script, *args, job_id=""):
            name = Path(str(script)).name
            argv = [str(a) for a in args]
            env.calls.append({"script": name, "storage": Path(argv[argv.index("--storage") + 1]),
                              "argv": argv})
            if name == "build_features.py":
                nf = argv[argv.index("--nf") + 1]
                ver = argv[argv.index("--version") + 1]
                d = Path(argv[argv.index("--storage") + 1]) / "Feature" / nf / ver
                d.mkdir(parents=True, exist_ok=True)
                (d / "_build_manifest.json").write_text(
                    json.dumps({"feature_count": 1}), encoding="utf-8")
            return ""
        return fake_run

    # ---- 发起任务（直调 run_mine，不经 HTTP——闸门端点用 HTTP 测） ----
    def start(self, monkeypatch, fake_run, *, extractor="cmd", dirs=None,
              target_nf="UDG", target_version="20.15.2"):
        monkeypatch.setattr(pl, "_run", fake_run)
        from app import jobs as jobs_mod
        job = jobs_mod.create_job(kind="product_doc_mine", nf=target_nf, version=target_version)
        spec = {"bundle_nf": "UDG", "bundle_version": "20.15.2",
                "target_nf": target_nf, "target_version": target_version,
                "extractor": extractor,
                "dirs": dirs if dirs is not None else {"mml": MML_REL}}
        pl.run_mine(job.job_id, spec)
        return jobs_mod.get_job(job.job_id)


@pytest.fixture
def env(tmp_path, tmp_data_dir, monkeypatch):
    e = _Env(tmp_path, tmp_data_dir, monkeypatch)
    yield e
    from app import jobs as jobs_mod
    jobs_mod._registry.clear()
    jobs_mod._db().execute("DELETE FROM import_jobs")
    jobs_mod._db().commit()


def _seed_live(assets: Path, layer: str, nf="UDG", ver="20.15.2") -> Path:
    d = assets / layer / nf / ver
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{nf}@{layer}@X.md").write_text(CMD_MD.format(nf=nf, ver=ver, name="X"), encoding="utf-8")
    return d


def _rows(job_id):
    from app import db as dbmod
    from app.repos import extract_files_repo
    return extract_files_repo.list_for_job(dbmod.get_shared_db(), job_id)


# ---------- cmd：awaiting 报告 → confirm 覆盖 ----------

def test_cmd_gate_report_then_confirm_overwrite(env, monkeypatch, tmp_data_dir):
    import app.service as svc_mod
    fake = env.stub_cmd(extra_binary=True)
    j = env.start(monkeypatch, fake)
    assert j.status == "awaiting", j.error
    rep = j.result
    assert rep["stage"] == "gate"
    assert rep["new_total"] == 1 and rep["new_by_layer"] == {"Command": 1}
    assert rep["identical_total"] == 1                       # SAME
    assert rep["modified_total"] == 2                        # MOD ME + assets/pic.png
    mods = {m["path"]: m for m in rep["modified"]}
    modme = next(p for p in mods if p.endswith("MOD ME.md"))
    assert mods[modme]["plus"] == 1 and mods[modme]["minus"] == 1
    png = next(p for p in mods if p.endswith(".png"))
    assert mods[png]["binary"] is True and mods[png]["old_bytes"] < mods[png]["new_bytes"]
    assert all(not Path(m["path"]).name.startswith("_")
               for m in rep["modified"])                      # sidecar 不进报告
    # 沙箱契约：--storage=沙箱；正式资产未被碰（MOD ME 仍是旧内容）
    assert str(env.calls[0]["storage"]).startswith(str(gate.gate_storage(j.job_id)))
    live_mod = tmp_data_dir / "Command" / "UDG" / "20.15.2" / "UDG@MMLCommand@MOD ME.md"
    assert "旧正文" in live_mod.read_text(encoding="utf-8")

    r = client.post(f"/api/v1/import/extract/{j.job_id}/confirm", json={"action": "overwrite"})
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "applying"                       # 立即返回（后台执行）
    j2 = _get_job(j.job_id)                                      # TestClient 同步跑完后台任务
    assert j2["status"] == "done"
    st = j2["result"]["applied"]
    assert st["added"] == 1 and st["modified"] == 2 and st["skipped_identical"] == 1
    assert "新正文" in live_mod.read_text(encoding="utf-8")
    assert (tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@ADD NEW.md").exists()
    assert (tmp_data_dir / "Command/UDG/20.15.2/assets/pic.png").read_bytes() == b"\x89PNG-new-longer"
    # 产物清单：add+modify（identical 不入）；sidecar 也入清单供失败回滚。
    rows = {(r2["path"], r2["op"]) for r2 in _rows(j.job_id)}
    assert any(p.endswith("ADD NEW.md") and op == "add" for p, op in rows)
    assert any(p.endswith("MOD ME.md") and op == "modify" for p, op in rows)
    assert any("_build_manifest" in p and op == "add" for p, op in rows)
    # 旧版备份存在（revert 还原源）；沙箱大头已清
    orig = gate.gate_originals(j.job_id) / "Command/UDG/20.15.2/UDG@MMLCommand@MOD ME.md"
    assert "旧正文" in orig.read_text(encoding="utf-8")
    assert not gate.gate_storage(j.job_id).exists()
    # 索引可查（增量索引已跑）；层计数沿用 result.layers 形状
    s = svc_mod.get_service()
    assert s.index.node("UDG@MMLCommand@ADD NEW", "20.15.2") is not None
    j2 = _get_job(j.job_id)
    assert j2["status"] == "done" and j2["result"]["layers"]["Command"] == 3
    assert j2["result"]["stage"] == "applied"
    # 模拟重启后只读 DB：终态/计数仍正确，且串行守卫已解除。
    from app import jobs as jobs_mod
    jobs_mod._registry.clear()
    durable = jobs_mod.get_job(j.job_id)
    assert durable.status == "done" and durable.added == 3
    assert jobs_mod.pending_for("product_doc_mine") is None


def _get_job(job_id):
    from app import jobs as jobs_mod
    return jobs_mod.get_job(job_id).summary()


# ---------- confirm 只新增 ----------

def test_confirm_new_only_keeps_existing(env, monkeypatch, tmp_data_dir):
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)
    assert j.status == "awaiting"
    live_mod = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@MOD ME.md"
    r = client.post(f"/api/v1/import/extract/{j.job_id}/confirm", json={"action": "new_only"})
    assert r.status_code == 200
    j2 = _get_job(j.job_id)
    st = j2["result"]["applied"]
    assert st["added"] == 1 and st["modified"] == 0 and st["skipped_existing"] == 1
    assert "旧正文" in live_mod.read_text(encoding="utf-8")     # 重复保留现有
    rows = _rows(j.job_id)
    assert len(rows) == 3
    assert all(row["op"] == "add" for row in rows)             # 新 md + sidecar


# ---------- cancel ----------

def test_cancel_awaiting(env, monkeypatch, tmp_data_dir):
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)
    assert j.status == "awaiting"
    r = client.post(f"/api/v1/import/extract/{j.job_id}/cancel")
    assert r.status_code == 200
    assert not gate.gate_dir(j.job_id).exists()                 # 沙箱全删
    assert not (tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@ADD NEW.md").exists()
    assert _get_job(j.job_id)["status"] == "cancelled"
    # cancelled 是终态：再 confirm/cancel 均 409
    assert client.post(f"/api/v1/import/extract/{j.job_id}/confirm",
                       json={"action": "overwrite"}).status_code == 409
    assert client.post(f"/api/v1/import/extract/{j.job_id}/cancel").status_code == 409


def test_confirm_twice_409(env, monkeypatch):
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)
    r1 = client.post(f"/api/v1/import/extract/{j.job_id}/confirm", json={"action": "overwrite"})
    assert r1.status_code == 200
    r2 = client.post(f"/api/v1/import/extract/{j.job_id}/confirm", json={"action": "overwrite"})
    assert r2.status_code == 409


# ---------- 崩溃自愈（评审修复 2026-08-26） ----------

def test_restart_after_confirm_keeps_originals_for_revert(env, monkeypatch, tmp_data_dir):
    """done 任务的 originals 备份跨重启保留（sweep 只清 failed/cancelled/无主）。"""
    j = _confirmed_task(env, monkeypatch)
    assert gate.gate_originals(j.job_id).exists()
    from app import jobs as jobs_mod
    jobs_mod._registry.clear()                                  # 模拟重启
    jobs_mod.sweep_interrupted()
    gate.sweep_orphan_gates()
    assert gate.gate_originals(j.job_id).exists()               # 备份未被清扫
    r = client.post(f"/api/v1/import/extract/{j.job_id}/revert")
    assert r.status_code == 200
    assert _get_job(j.job_id)["result"]["revert"]["restored"] == 1  # 回退照常可用


def test_sweep_done_removes_leftover_storage_but_keeps_originals(env, monkeypatch):
    """done 后 cleanup 中断的 storage 可清理，originals 不可误删。"""
    job = _confirmed_task(env, monkeypatch)
    gate.gate_storage(job.job_id).mkdir(parents=True)
    (gate.gate_storage(job.job_id) / "leftover").write_text("x", encoding="utf-8")
    assert gate.gate_originals(job.job_id).exists()

    assert gate.sweep_orphan_gates() >= 1
    assert not gate.gate_storage(job.job_id).exists()
    assert gate.gate_originals(job.job_id).exists()


def test_confirm_copy_failure_retry_completes_manifest(env, monkeypatch, tmp_data_dir):
    """apply 后台执行拷贝中断（清单先行已写）→ 任务自动回 awaiting 带告警 →
    重试：已落盘文件 identical 但清单行保留，覆盖面完整——revert 不丢覆盖。"""
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)
    orig_copy = gate._copy
    state = {"n": 0}

    def flaky_copy(src, dst):
        state["n"] += 1
        if state["n"] == 3:                                      # sidecar+ADD NEW 已拷，MOD ME 备份时断
            raise OSError("disk full")
        return orig_copy(src, dst)

    monkeypatch.setattr(gate, "_copy", flaky_copy)
    r1 = client.post(f"/api/v1/import/extract/{j.job_id}/confirm", json={"action": "overwrite"})
    assert r1.status_code == 200                                 # 立即返回（后台）
    j1 = _get_job(j.job_id)                                      # 后台失败已回 awaiting
    assert j1["status"] == "awaiting"
    assert any("入库执行失败" in w for w in j1["warnings"])
    assert len(_rows(j.job_id)) == 4                             # 清单先行：含两层 sidecar

    monkeypatch.setattr(gate, "_copy", orig_copy)
    r2 = client.post(f"/api/v1/import/extract/{j.job_id}/confirm", json={"action": "overwrite"})
    assert r2.status_code == 200, r2.text
    j2 = _get_job(j.job_id)
    assert j2["status"] == "done"
    rows = _rows(j.job_id)
    ops = {r["op"] for r in rows}
    assert len(rows) == 4 and ops == {"add", "modify"}           # 重试后清单仍完整
    mod_p = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@MOD ME.md"
    assert "新正文" in mod_p.read_text(encoding="utf-8")         # 重试补完了拷贝


def test_apply_persists_done_before_storage_cleanup(env, monkeypatch):
    """storage 清理遇到 BaseException 也不得丢掉已完成的终态。"""
    from app import jobs as jobs_mod

    job = env.start(monkeypatch, env.stub_cmd())
    jobs_mod.update_job(job.job_id, status="processing",
                        result={**job.result, "stage": "applying"})

    def interrupted_cleanup(*_args, **_kwargs):
        raise SystemExit("synthetic cleanup interruption")

    monkeypatch.setattr(gate.shutil, "rmtree", interrupted_cleanup)
    with pytest.raises(SystemExit):
        gate.apply_gate(job.job_id, "overwrite")

    jobs_mod._registry.clear()
    restored = jobs_mod.get_job(job.job_id)
    assert restored.status == "done"
    assert restored.result["stage"] == "applied"
    assert restored.added == 2


def test_apply_wrapper_records_baseexception_before_reraising(env, monkeypatch):
    """SystemExit/KeyboardInterrupt 越过 Exception 时也要先恢复任务态。"""
    from app import jobs as jobs_mod
    from app.routers import productdoc

    job = env.start(monkeypatch, env.stub_cmd())
    jobs_mod.update_job(job.job_id, status="processing",
                        result={**job.result, "stage": "applying"})
    assert jobs_mod.acquire_mutex("product_doc_mine")
    monkeypatch.setattr(gate, "apply_gate",
                        lambda *_args: (_ for _ in ()).throw(SystemExit("boom")))

    with pytest.raises(SystemExit):
        productdoc._apply_and_release(job.job_id, "overwrite")

    restored = jobs_mod.get_job(job.job_id)
    assert restored.status == "awaiting"
    assert restored.result["stage"] == "gate"
    assert "SystemExit" in restored.result["confirm_error"]
    assert jobs_mod.acquire_mutex("product_doc_mine")
    jobs_mod.release_mutex("product_doc_mine")


@pytest.mark.parametrize(
    ("kind", "wrapper", "target_name", "args"),
    [
        ("product_doc_extract", "_run_extract_and_release", "run_extract",
         (Path("missing.hwics"), "UNC", "20.16.2", "tester")),
        ("product_doc_mine", "_run_mine_and_release", "run_mine", ({},)),
    ],
)
def test_runner_wrappers_record_baseexception(
        env, monkeypatch, kind, wrapper, target_name, args):
    """解压/抽取后台线程遇 BaseException 也必须落 failed 并释放互斥。"""
    from app import jobs as jobs_mod
    from app.routers import productdoc

    job = jobs_mod.create_job(kind=kind, nf="UNC", version="20.16.2")
    assert jobs_mod.acquire_mutex(kind)
    monkeypatch.setattr(
        productdoc, target_name,
        lambda *_args, **_kwargs: (_ for _ in ()).throw(SystemExit("boom")),
    )
    with pytest.raises(SystemExit):
        getattr(productdoc, wrapper)(job.job_id, *args)
    restored = jobs_mod.get_job(job.job_id)
    assert restored.status == "failed"
    assert "SystemExit" in restored.error
    assert jobs_mod.acquire_mutex(kind)
    jobs_mod.release_mutex(kind)


def test_index_failure_keeps_gate_retryable(env, monkeypatch):
    """文件已拷贝但索引失败时不得假装 done，sandbox/清单必须保留。"""
    from app.service import get_service

    job = env.start(monkeypatch, env.stub_cmd())
    monkeypatch.setattr(
        get_service(), "reindex_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fts failed")),
    )
    response = client.post(
        f"/api/v1/import/extract/{job.job_id}/confirm", json={"action": "overwrite"}
    )
    assert response.status_code == 200
    restored = _get_job(job.job_id)
    assert restored["status"] == "awaiting"
    assert "fts failed" in restored["result"]["confirm_error"]
    assert gate.gate_storage(job.job_id).exists()
    assert _rows(job.job_id)


def test_new_only_serializes_classification_and_copy_with_asset_writer(
        env, monkeypatch, tmp_data_dir):
    """并发资产写者不得在 new_only 分类后插入文件却又被沙箱覆盖。"""
    from app.service import get_service, import_lock

    job = env.start(monkeypatch, env.stub_cmd(new=("RACE",), same=(), mod=()))
    service = get_service()
    rel = "Command/UDG/20.15.2/UDG@MMLCommand@RACE.md"
    live = tmp_data_dir / rel
    original_classify = gate._classify
    classified = threading.Event()
    writer_acquired = threading.Event()

    def observed_classify(*args, **kwargs):
        yield from original_classify(*args, **kwargs)
        classified.set()
        # 旧实现此时没有 import_lock，写者可插入；修复后写者须等 apply 完成。
        writer_acquired.wait(timeout=0.2)

    def competing_writer():
        assert classified.wait(timeout=2)
        with import_lock:
            writer_acquired.set()
            live.parent.mkdir(parents=True, exist_ok=True)
            live.write_text(
                CMD_MD.format(nf="UDG", ver="20.15.2", name="RACE") + "外部写入\n",
                encoding="utf-8",
            )
            service.reindex_paths([rel])

    monkeypatch.setattr(gate, "_classify", observed_classify)
    writer = threading.Thread(target=competing_writer)
    writer.start()
    gate.apply_gate(job.job_id, "new_only")
    writer.join(timeout=3)

    assert not writer.is_alive()
    assert "外部写入" in live.read_text(encoding="utf-8")


def test_reconcile_completed_stale_apply_marks_done(env, monkeypatch):
    """兼容修复前遗留：live/索引/清单已完成且 storage 已删应补 done。"""
    from app import jobs as jobs_mod
    import app.service as service_mod

    confirmed = _confirmed_task(env, monkeypatch)
    result = dict(jobs_mod.get_job(confirmed.job_id).result)
    result["stage"] = "applying"
    jobs_mod.update_job(confirmed.job_id, status="processing", result=result,
                        added=0, finished_at=0)
    jobs_mod._registry.clear()

    class TrackingLock:
        entered = False

        def __enter__(self):
            self.entered = True

        def __exit__(self, *_args):
            return False

    lock = TrackingLock()
    monkeypatch.setattr(service_mod, "import_lock", lock)

    assert gate.reconcile_interrupted() >= 1
    assert lock.entered
    restored = jobs_mod.get_job(confirmed.job_id)
    assert restored.status == "done"
    assert restored.result["stage"] == "applied"
    assert restored.added == 2
    assert jobs_mod.pending_for("product_doc_mine") is None


def test_reconcile_completed_stale_apply_repairs_files_catalog_before_done(
        env, monkeypatch):
    """崩在对象索引完成、files 户口未写之间时，对账须先补册再确认 done。"""
    from app import jobs as jobs_mod
    from app.service import get_service

    confirmed = _confirmed_task(env, monkeypatch)
    svc = get_service()
    manifest = _rows(confirmed.job_id)
    assert manifest

    # 模拟 apply 在 files_repo 同步之前崩溃：资产、objects/FTS/清单均已完成，户口为空。
    for table in ("files", "files_fts", "files_fts_map"):
        svc.db.execute(f"DELETE FROM {table}")
    result = dict(jobs_mod.get_job(confirmed.job_id).result)
    jobs_mod.update_job(
        confirmed.job_id, status="processing",
        result={**result, "stage": "applying"}, added=0, finished_at=0,
    )
    jobs_mod._registry.clear()

    assert gate.reconcile_interrupted() >= 1
    restored = jobs_mod.get_job(confirmed.job_id)
    assert restored.status == "done"
    catalog_paths = {row["path"] for row in svc.db.execute(
        "SELECT path FROM files")}
    manifest_paths = {row["path"] for row in manifest}
    assert manifest_paths <= catalog_paths
    assert all("/".join(path.split("/")[:-1]) in catalog_paths
               for path in manifest_paths)


def test_get_job_is_read_only_for_stale_apply(env, monkeypatch):
    """GET 任务轮询必须无写副作用，不得隐式执行孤儿清扫/终止 PID。"""
    from app import jobs as jobs_mod
    from app.routers import productdoc

    confirmed = _confirmed_task(env, monkeypatch)
    result = dict(jobs_mod.get_job(confirmed.job_id).result)
    jobs_mod.update_job(
        confirmed.job_id, status="processing",
        result={**result, "stage": "applying"}, added=0, finished_at=0,
    )
    jobs_mod._registry.clear()
    monkeypatch.setattr(
        productdoc, "_reconcile_idle_jobs",
        lambda: (_ for _ in ()).throw(AssertionError("GET must stay read-only")),
    )

    response = client.get(f"/api/v1/import/jobs/{confirmed.job_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "processing"


def test_explicit_reconcile_endpoint_recovers_completed_stale_apply(env, monkeypatch):
    """受资产权限保护的显式 POST 对账可在线解锁孤儿 applying。"""
    from app import jobs as jobs_mod

    confirmed = _confirmed_task(env, monkeypatch)
    result = dict(jobs_mod.get_job(confirmed.job_id).result)
    jobs_mod.update_job(
        confirmed.job_id, status="processing",
        result={**result, "stage": "applying"}, added=0, finished_at=0,
    )
    jobs_mod._registry.clear()

    reconciled = client.post("/api/v1/import/jobs/reconcile")
    assert reconciled.status_code == 200
    assert reconciled.json()["reconciled"] >= 1
    response = client.get(f"/api/v1/import/jobs/{confirmed.job_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["added"] == 2
    assert jobs_mod.pending_for("product_doc_mine") is None


def test_job_poll_recovers_extract_finalizing_checkpoint(env):
    """bundle.json 已 done 时，轮询将遗留 extract_finalizing 补成 done。"""
    from app import jobs as jobs_mod
    from app.pipeline import bundles

    source_sha = "abc123"
    bundle_dir = bundles.bundle_dir("UDG", "20.15.2")
    bundles.write_meta(bundle_dir, {
        "nf": "UDG", "version": "20.15.2", "status": "done",
        "source_sha256": source_sha, "md_count": 17,
    })
    job = jobs_mod.create_job(kind="product_doc_extract", nf="UDG", version="20.15.2")
    jobs_mod.update_job(job.job_id, result={
        "stage": "extract_finalizing", "source_sha256": source_sha,
        "md_count": 17, "bundle": "UDG_20.15.2",
    })
    jobs_mod._registry.clear()

    assert client.post("/api/v1/import/jobs/reconcile").status_code == 200
    response = client.get(f"/api/v1/import/jobs/{job.job_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "done"
    assert response.json()["added"] == 17
    assert response.json()["result"]["stage"] == "extracted"


def test_job_poll_recovers_gate_ready_checkpoint(env):
    """diff 已持久化而 awaiting 丢写时，轮询自动补齐闸门态。"""
    from app import jobs as jobs_mod

    job = jobs_mod.create_job(kind="product_doc_mine", nf="UDG", version="20.15.2")
    jobs_mod.update_job(job.job_id, result={
        "stage": "gate_ready", "target_nf": "UDG", "target_version": "20.15.2",
        "layers": ["Command"], "new_total": 1, "modified_total": 0,
    })
    jobs_mod._registry.clear()

    assert client.post("/api/v1/import/jobs/reconcile").status_code == 200
    response = client.get(f"/api/v1/import/jobs/{job.job_id}")
    assert response.status_code == 200
    assert response.json()["status"] == "awaiting"
    assert response.json()["result"]["stage"] == "gate"


def test_cancel_after_applied_state_reindexes_rollback(env, monkeypatch, tmp_data_dir):
    """即使文件曾经完成索引，cancel 回滚也必须同步 objects/FTS。"""
    from app import jobs as jobs_mod
    from app.repos import fts_repo
    from app.service import get_service

    confirmed = _confirmed_task(env, monkeypatch)
    current = jobs_mod.get_job(confirmed.job_id)
    jobs_mod.update_job(confirmed.job_id, status="awaiting",
                        result={**current.result, "stage": "gate"})

    response = client.post(f"/api/v1/import/extract/{confirmed.job_id}/cancel")
    assert response.status_code == 200, response.text
    service = get_service()
    added = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@ADD NEW.md"
    assert not added.exists()
    assert service.index.node("UDG@MMLCommand@ADD NEW", "20.15.2") is None
    assert service.index.node("UDG@MMLCommand@MOD ME", "20.15.2") is not None
    assert fts_repo.integrity_ok(service.db)
    # files 户口册随 cancel 收敛（modify 行在 / add+sidecar 行无 / 三表一致）
    from app.repos import files_repo
    have = {r["path"] for r in service.db.execute(
        "SELECT path FROM files").fetchall()}
    assert any(p.endswith("MOD ME.md") for p in have)
    assert not any(p.endswith("ADD NEW.md") for p in have)
    assert not any("_build_manifest" in p for p in have)
    assert files_repo.integrity_ok(service.db)


def test_cancel_retry_reconciles_full_manifest_after_index_failure(
        env, monkeypatch, tmp_data_dir):
    """文件已回滚但首次索引补偿失败，重试仍须按完整清单清掉幽灵节点。"""
    from app import jobs as jobs_mod
    from app.service import get_service

    confirmed = _confirmed_task(env, monkeypatch)
    current = jobs_mod.get_job(confirmed.job_id)
    jobs_mod.update_job(confirmed.job_id, status="awaiting",
                        result={**current.result, "stage": "gate"})
    service = get_service()
    original_reindex = service.reindex_paths
    monkeypatch.setattr(
        service, "reindex_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index down")),
    )

    with pytest.raises(RuntimeError, match="index down"):
        client.post(f"/api/v1/import/extract/{confirmed.job_id}/cancel")
    added = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@ADD NEW.md"
    assert not added.exists()
    assert service.index.node("UDG@MMLCommand@ADD NEW", "20.15.2") is not None

    monkeypatch.setattr(service, "reindex_paths", original_reindex)
    response = client.post(f"/api/v1/import/extract/{confirmed.job_id}/cancel")
    assert response.status_code == 200, response.text
    assert service.index.node("UDG@MMLCommand@ADD NEW", "20.15.2") is None


def test_cancel_after_partial_apply_rolls_back_files(env, monkeypatch, tmp_data_dir):
    """拷贝中断后用户选择撤销：已部分落盘的文件按清单回滚（正式资产零改动）。"""
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)
    orig_copy = gate._copy
    state = {"n": 0}

    def flaky_copy(src, dst):
        state["n"] += 1
        if state["n"] == 3:                                      # ADD NEW 已落盘，MOD ME 备份时断
            raise OSError("disk full")
        return orig_copy(src, dst)

    monkeypatch.setattr(gate, "_copy", flaky_copy)
    r1 = client.post(f"/api/v1/import/extract/{j.job_id}/confirm", json={"action": "overwrite"})
    assert r1.status_code == 200                                 # 后台执行失败自动回 awaiting
    assert _get_job(j.job_id)["status"] == "awaiting"
    add_p = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@ADD NEW.md"
    assert add_p.exists()                                        # 首文件已落盘（泄漏）

    monkeypatch.setattr(gate, "_copy", orig_copy)
    r2 = client.post(f"/api/v1/import/extract/{j.job_id}/cancel")
    assert r2.status_code == 200
    assert not add_p.exists()                                    # 撤销回滚了泄漏文件
    assert not (tmp_data_dir / "Command/UDG/20.15.2/_build_manifest.json").exists()
    assert _rows(j.job_id) == []
    assert _get_job(j.job_id)["status"] == "cancelled"
    # files 户口册随 cancel 收敛：部分落盘回滚后 add/sidecar 行清、modify 行在
    from app import db as dbmod
    from app.repos import files_repo
    db = dbmod.get_shared_db()
    have = {r["path"] for r in db.execute("SELECT path FROM files").fetchall()}
    assert any(p.endswith("MOD ME.md") for p in have)
    assert not any(p.endswith("ADD NEW.md") for p in have)
    assert not any("_build_manifest" in p for p in have)
    assert files_repo.integrity_ok(db)


# ---------- feature：依赖（直调层）+ 自动重跑 ----------

def test_runner_feature_dep_blocks(env, monkeypatch):
    """runner 权威复检：缺 Command/License → failed 且中文报错（沙箱已清）。"""
    fake = env.stub_feature_only()
    j = env.start(monkeypatch, fake, extractor="feature", dirs={"feature": FEAT_REL})
    assert j.status == "failed"
    assert "缺少依赖层资产" in j.error and "Command" in j.error
    assert not gate.gate_dir(j.job_id).exists()
    assert env.calls == []                                      # 未跑任何构建器


def _seed_cmd_task_record(env):
    """造一条「最近成功 cmd 任务」记录（feature 自动重跑的数据源）。"""
    from app import jobs as jobs_mod
    job = jobs_mod.create_job(kind="product_doc_mine", nf="UDG", version="20.15.2")
    jobs_mod.update_job(job.job_id, status="done", result={
        "stage": "applied", "script": "cmd",
        "bundle_nf": "UDG", "bundle_version": "20.15.2",
        "target_nf": "UDG", "target_version": "20.15.2",
        "dirs": {"mml": [MML_REL]}})
    return job


def test_feature_auto_rerun_uses_recorded_dirs(env, monkeypatch, tmp_data_dir):
    _seed_cmd_task_record(env)
    _seed_live(tmp_data_dir, "Command")
    _seed_live(tmp_data_dir, "License")
    fake = env.stub_cmd(new=(), same=(), mod=())  # 复用 cmd 桩：rerun 会写 manifest
    j = env.start(monkeypatch, fake, extractor="feature", dirs={"feature": FEAT_REL})
    assert j.status == "awaiting", j.error
    scripts = [c["script"] for c in env.calls]
    # 主构建（features）+ 自动重跑（commands+configobjects）
    assert scripts.count("build_features.py") == 1
    assert scripts.count("build_commands.py") == 1
    assert scripts.count("build_configobjects.py") == 1
    # 重跑也写沙箱，且 mml 目录来自记录（包内解析后的绝对路径）
    cmd_call = next(c for c in env.calls if c["script"] == "build_commands.py")
    assert str(cmd_call["storage"]).startswith(str(gate.gate_storage(j.job_id)))
    mml = cmd_call["argv"][cmd_call["argv"].index("--mml-dir") + 1]
    assert "UDG MML命令" in mml
    # Command/ConfigObject 进 written 层（差异报告覆盖）
    assert "Command" in j.result["layers"] and "ConfigObject" in j.result["layers"]


def test_feature_rerun_legacy_skip_warns(env, monkeypatch, tmp_data_dir):
    _seed_live(tmp_data_dir, "Command")
    _seed_live(tmp_data_dir, "License")
    fake = env.stub_feature_only()
    j = env.start(monkeypatch, fake, extractor="feature", dirs={"feature": FEAT_REL})
    assert j.status == "awaiting"
    assert any("跳过命令层特性引用修复" in w for w in j.warnings)
    assert [c["script"] for c in env.calls] == ["build_features.py"]
    assert "Command" not in j.result["layers"]


# ---------- revert（sha 守卫） ----------

def _confirmed_task(env, monkeypatch):
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)
    r = client.post(f"/api/v1/import/extract/{j.job_id}/confirm", json={"action": "overwrite"})
    assert r.status_code == 200
    return j


def test_revert_full(env, monkeypatch, tmp_data_dir):
    j = _confirmed_task(env, monkeypatch)
    add_p = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@ADD NEW.md"
    mod_p = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@MOD ME.md"
    assert add_p.exists() and "新正文" in mod_p.read_text(encoding="utf-8")

    r = client.post(f"/api/v1/import/extract/{j.job_id}/revert")
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "reverting"                      # 立即返回（后台）
    j3 = _get_job(j.job_id)                                      # 后台同步跑完
    out = j3["result"]["revert"]
    assert out["soft_deleted"] == 1 and out["restored"] == 1 and out["skipped"] == []
    assert not add_p.exists()                                   # 新增→软删（进回收站）
    assert "旧正文" in mod_p.read_text(encoding="utf-8")        # 覆盖→还原旧版
    # 回收站登记（trash 表）
    from app import db as dbmod
    trash_rows = dbmod.get_shared_db().execute("SELECT * FROM trash").fetchall()
    assert any(r2["original_path"].endswith("ADD NEW.md") for r2 in trash_rows)
    # 清单消费 + 门目录清理 + result 标记；二次 revert 400
    assert _rows(j.job_id) == []
    assert not gate.gate_dir(j.job_id).exists()
    jr = _get_job(j.job_id)
    assert jr["result"]["reverted_at"]
    assert client.post(f"/api/v1/import/extract/{j.job_id}/revert").status_code == 400


def test_revert_retry_reconciles_full_manifest_after_index_failure(
        env, monkeypatch, tmp_data_dir):
    """文件已移除/恢复但首次索引失败，重试不得把旧 objects/FTS 留在库中。"""
    from app.service import get_service

    confirmed = _confirmed_task(env, monkeypatch)
    service = get_service()
    original_reindex = service.reindex_paths
    monkeypatch.setattr(
        service, "reindex_paths",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("index down")),
    )

    first = client.post(f"/api/v1/import/extract/{confirmed.job_id}/revert")
    assert first.status_code == 200, first.text
    added = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@ADD NEW.md"
    assert not added.exists()
    assert service.index.node("UDG@MMLCommand@ADD NEW", "20.15.2") is not None

    monkeypatch.setattr(service, "reindex_paths", original_reindex)
    second = client.post(f"/api/v1/import/extract/{confirmed.job_id}/revert")
    assert second.status_code == 200, second.text
    assert service.index.node("UDG@MMLCommand@ADD NEW", "20.15.2") is None


def test_revert_sha_guard_skips_overwritten(env, monkeypatch, tmp_data_dir):
    j = _confirmed_task(env, monkeypatch)
    add_p = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@ADD NEW.md"
    mod_p = tmp_data_dir / "Command/UDG/20.15.2/UDG@MMLCommand@MOD ME.md"
    # 模拟后续任务/手工覆盖 add 文件 → sha 守卫跳过防误删
    add_p.write_text("被后续改写", encoding="utf-8")
    r = client.post(f"/api/v1/import/extract/{j.job_id}/revert")
    assert r.status_code == 200
    out = _get_job(j.job_id)["result"]["revert"]
    assert out["soft_deleted"] == 0 and out["restored"] == 1
    assert len(out["skipped"]) == 1 and "防误删" in out["skipped"][0]
    assert add_p.exists() and add_p.read_text(encoding="utf-8") == "被后续改写"
    assert "旧正文" in mod_p.read_text(encoding="utf-8")


# ---------- 重启对账（applying/reverting 执行中断，2026-08-27 异步化） ----------

def test_reconcile_applying_back_to_awaiting(env, monkeypatch):
    """入库执行中后端重启 → 复位 awaiting（可重试确认/撤销），不被 sweep 标 failed。"""
    from app import jobs as jobs_mod
    job = jobs_mod.create_job(kind="product_doc_mine", nf="UDG", version="20.15.2")
    jobs_mod.update_job(job.job_id, status="awaiting", result={
        "stage": "gate", "target_nf": "UDG", "target_version": "20.15.2"})
    jobs_mod.update_job(job.job_id, status="processing",
                        result={"stage": "applying", "target_nf": "UDG"})
    gate.gate_storage(job.job_id).mkdir(parents=True)            # 可重试证据仍在
    jobs_mod._registry.clear()                                  # 模拟重启（DB-only）
    assert gate.reconcile_interrupted() >= 1
    j = jobs_mod.get_job(job.job_id)
    assert j.status == "awaiting" and j.result["stage"] == "gate"
    assert "入库执行中断" in j.result["confirm_error"]
    jobs_mod.sweep_interrupted()                                # 复位后 sweep 不再动它
    assert jobs_mod.get_job(job.job_id).status == "awaiting"


def test_reconcile_reverting_back_to_done(env, monkeypatch):
    """回退执行中后端重启 → 复位 done + 告警（可重发起，每文件守卫安全）。"""
    from app import jobs as jobs_mod
    job = jobs_mod.create_job(kind="product_doc_mine", nf="UDG", version="20.15.2")
    jobs_mod.update_job(job.job_id, status="done", result={
        "stage": "applied", "script": "cmd", "target_nf": "UDG"})
    jobs_mod.update_job(job.job_id, status="processing",
                        result={"stage": "reverting", "target_nf": "UDG"})
    jobs_mod._registry.clear()
    assert gate.reconcile_interrupted() >= 1
    j = jobs_mod.get_job(job.job_id)
    assert j.status == "done" and j.finished_at > 0
    assert any("回退执行中断" in w for w in j.warnings)
    assert not j.result.get("reverted_at")                       # 未标已回退→可重发起


def test_revert_rejects_wrong_state(env, monkeypatch):
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)                            # awaiting
    r = client.post(f"/api/v1/import/extract/{j.job_id}/revert")
    assert r.status_code == 400


# ---------- sweep / 重启语义 ----------

def test_awaiting_gate_dir_survives_sweep(env, monkeypatch):
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)
    assert j.status == "awaiting"
    assert gate.gate_storage(j.job_id).exists()
    from app import jobs as jobs_mod
    jobs_mod._registry.clear()                                  # 模拟重启
    jobs_mod.sweep_interrupted()                                # 只清 processing
    n = gate.sweep_orphan_gates()
    assert gate.gate_dir(j.job_id).exists()                     # awaiting 沙箱保留
    got = jobs_mod.get_job(j.job_id)
    assert got is not None and got.status == "awaiting"


def test_processing_job_sandbox_swept(env, monkeypatch, tmp_data_dir):
    from app import jobs as jobs_mod
    monkeypatch.setattr(jobs_mod, "_kill_pid_tree", lambda pid: None)
    job = jobs_mod.create_job(kind="product_doc_mine", nf="UDG", version="20.15.2")
    jobs_mod.update_job(job.job_id, status="processing")
    gate.gate_storage(job.job_id).mkdir(parents=True)           # 半途沙箱
    (gate.gate_storage(job.job_id) / "junk").write_text("x", encoding="utf-8")
    jobs_mod._registry.clear()
    jobs_mod.sweep_interrupted()                                # processing→failed
    n = gate.sweep_orphan_gates()
    assert n >= 1
    assert not gate.gate_dir(job.job_id).exists()               # 孤儿沙箱被清
    assert jobs_mod.get_job(job.job_id).status == "failed"


def test_delete_job_purges_gate_and_manifest(env, monkeypatch):
    j = _confirmed_task(env, monkeypatch)
    assert gate.gate_originals(j.job_id).exists()
    assert len(_rows(j.job_id)) == 4
    r = client.delete(f"/api/v1/import/jobs/{j.job_id}")
    assert r.status_code == 200
    assert not gate.gate_dir(j.job_id).exists()
    assert _rows(j.job_id) == []                                # 回退权随历史删除丧失


def test_delete_job_mutex_conflict_preserves_history_and_revert_data(env, monkeypatch):
    """闸门操作占锁时删除应无副作用，不能先删历史再返回 409。"""
    from app import jobs as jobs_mod

    confirmed = _confirmed_task(env, monkeypatch)
    assert gate.gate_dir(confirmed.job_id).exists()
    assert _rows(confirmed.job_id)
    assert jobs_mod.acquire_mutex("product_doc_mine")
    try:
        response = client.delete(f"/api/v1/import/jobs/{confirmed.job_id}")
    finally:
        jobs_mod.release_mutex("product_doc_mine")

    assert response.status_code == 409
    assert jobs_mod.get_job(confirmed.job_id) is not None
    assert gate.gate_dir(confirmed.job_id).exists()
    assert _rows(confirmed.job_id)


def test_delete_awaiting_rejected(env, monkeypatch):
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake)
    r = client.delete(f"/api/v1/import/jobs/{j.job_id}")
    assert r.status_code == 400


# ---------- 沙箱契约细节 ----------

def test_sandbox_pre_copies_existing_layers_for_cumulative(env, monkeypatch, tmp_data_dir):
    """分次累积：第二批抽取时沙箱预拷第一批命令（build_configobjects 读全量命令）。"""
    _seed_live(tmp_data_dir, "Command")                         # 第一批命令（已在正式资产）
    seen = {}

    def fake_run(script, *args, job_id=""):
        name = Path(str(script)).name
        argv = [str(a) for a in args]
        storage = Path(argv[argv.index("--storage") + 1])
        if name == "build_configobjects.py":
            # 读沙箱 Command 层（应含预拷的第一批文件）
            seen["sandbox_cmds"] = [p.name for p in
                                    (storage / "Command/UDG/20.15.2").glob("*.md")]
        if name == "build_commands.py":
            d = storage / "Command/UDG/20.15.2"
            d.mkdir(parents=True, exist_ok=True)
            (d / "UDG@MMLCommand@ADD B2.md").write_text(
                CMD_MD.format(nf="UDG", ver="20.15.2", name="ADD B2"), encoding="utf-8")
            (d / "_build_manifest.json").write_text('{"command_count": 1}', encoding="utf-8")
        if name == "build_configobjects.py":
            d = storage / "ConfigObject/UDG/20.15.2"
            d.mkdir(parents=True, exist_ok=True)
            (d / "_build_manifest.json").write_text('{"object_count": 0}', encoding="utf-8")
        return ""

    j = env.start(monkeypatch, fake_run)
    assert j.status == "awaiting", j.error
    assert any("UDG@Command@X.md" == n for n in seen["sandbox_cmds"])  # 第一批在场


def test_multi_mml_dirs_single_invocation_into_sandbox(env, monkeypatch):
    """mml 两目录 → 单次构建两次 --mml-dir；--storage=沙箱；目标 nf 可异于包名。"""
    import app.config as config
    from pathlib import Path as P
    extra = config.OUTPUT_DIR / "UDG_20.15.2" / "分册二"
    extra.mkdir(parents=True, exist_ok=True)
    fake = env.stub_cmd()
    j = env.start(monkeypatch, fake,
                  dirs={"mml": [MML_REL, "分册二"]}, target_nf="AMF")
    assert j.status == "awaiting", j.error
    cmd_call = next(c for c in env.calls if c["script"] == "build_commands.py")
    argv = cmd_call["argv"]
    idx = [i for i, a in enumerate(argv) if a == "--mml-dir"]
    assert len(idx) == 2                                          # 单次构建两次旗标
    assert argv[argv.index("--nf") + 1] == "AMF"                  # 目标网元传给脚本
    assert str(cmd_call["storage"]).startswith(str(gate.gate_storage(j.job_id)))
    # 产物在沙箱的 AMF 槽位（目标≠包名解耦核心）
    assert (gate.gate_storage(j.job_id) / "Command/AMF/20.15.2/_build_manifest.json").exists()


# ---------- files 户口册同步（spec §4.2，apply/cancel/revert 三流） ----------

def test_gate_apply_and_revert_sync_files(env, monkeypatch, tmp_data_dir):
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
    # 父目录行入册（path 直接子项浏览可见新文件；批量 API 的 parents 调用）
    dir_row = db.execute(
        "SELECT is_dir FROM files WHERE path='Command/UDG/20.15.2'").fetchone()
    assert dir_row is not None and dir_row["is_dir"] == 1
    assert "Command/UDG/20.15.2/assets" in have  # apply 新建的目录（extra_binary）
    # revert：add → 软删/物理删（行消失）；modify → 还原旧版（行保留）
    from app.pipeline import gate as gate_mod
    gate_mod.revert_job(j.job_id, deleted_by="tester")
    have2 = {row["path"] for row in db.execute("SELECT path FROM files").fetchall()}
    assert not any(p.endswith("ADD NEW.md") for p in have2)
    assert not any("_build_manifest" in p for p in have2)
    assert any(p.endswith("MOD ME.md") for p in have2)
    # ①MOD ME 行的 size/mtime 已随还原旧版刷新（与磁盘 stat 一致）
    mod_rel = "Command/UDG/20.15.2/UDG@MMLCommand@MOD ME.md"
    mod_row = db.execute("SELECT size, mtime FROM files WHERE path=?",
                         (mod_rel,)).fetchone()
    st = (tmp_data_dir / mod_rel).stat()
    assert mod_row["size"] == st.st_size
    assert mod_row["mtime"] == pytest.approx(st.st_mtime)
    # ②三表一致（files / files_fts / files_fts_map）
    from app.repos import files_repo
    assert files_repo.integrity_ok(db)
