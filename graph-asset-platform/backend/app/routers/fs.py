"""fs router：资产目录文件浏览器 + 指定目录上传。

设计要点（计划 splendid-forging-valiant + 真实数据布局修正）：
- **GET** 走默认 frontend 权限；**写端点** ``_require_upload`` 二次校验 upload。
- 所有写操作在 ``import_lock`` 内 + ``svc.rebuild()``（读 API 只读 index 快照）。
- **路径派生不用 ``classify()``**：真实 ``platform-data/assets`` 布局不规则——
  Command 用 layer 名、Task 层 3 子 type 用 type 名（AtomTask/CompoundTask/FeatureTask）、
  Feature 是 ``{id}/概述.md`` 子目录。classify 的 layer 名派生（``Task/``）会写错位。
  改为 **target_dir 驱动**：upload/move 由调用方指定目标目录，文件名 = frontmatter.id。
- PUT 编辑写回**原路径**（不归位），仅校验 id 未变（改 id 走 rename）。
- rename 改 id：文件名变（原目录 + new_id.md），全库重写 ``[[old]]→[[new]]``。
"""
import io
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel

from ..frontmatter_rw import rewrite_frontmatter, validate_md
from ..logical_id import split_id
from ..md_parser import parse_md  # noqa: F401  (保留供未来按需解析)
from ..repos import files_repo, trash_repo
from ..service import get_service, import_lock
from ..store import normalize_relpath
from ..telemetry.recorder import record
from ..users.service import check_perm

router = APIRouter()


# ---------- 辅助 ----------

def _require_assets(request: Request) -> None:
    """写操作权限：中间件已把 /fs* 全量门控到 assets，此处二次校验（纵深防御）。"""
    user = getattr(request.state, "user_obj", None)
    if not user or not check_perm(user, "assets"):
        raise HTTPException(status_code=403, detail="需要资产权限（can_assets）")


def _require_admin(request: Request) -> None:
    """回收站永久清理权限：仅 admin。"""
    user = getattr(request.state, "user_obj", None)
    if not user or not check_perm(user, "admin"):
        raise HTTPException(status_code=403, detail="需要 admin 权限")


def _record(request: Request, endpoint: str, id_: str = "") -> None:
    record(endpoint, id_, "", user=request.state.user,
           caller=request.state.caller, level="object",
           operator=getattr(request.state, "operator", ""))


def _safe_filename(name: str) -> str:
    return Path(name).name if name else "unnamed"


def _join(target_dir: str, filename: str) -> str:
    """target_dir + filename → 归一化相对路径（去首尾斜杠，防空）。"""
    if "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="文件名不能包含路径分隔符")
    td = _request_path(target_dir, allow_root=True)
    return _request_path(f"{td}/{filename}" if td else filename)


def _request_path(value: str, *, allow_root: bool = False) -> str:
    """规范化用户提供的 assets 相对路径；非法输入统一返回 400。"""
    try:
        return normalize_relpath(value, allow_root=allow_root)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex)) from ex


# 顶层目录 → 允许的 type（上传时校验 md.type 属于所选层，防错位：如选 Command 但 md 是 AtomTask）。
# 真实布局：Command 目录装 MMLCommand（layer 名目录）；其余目录 = type 名；Business 装 3 类 cross。
_DIR_ALLOWED_TYPES = {
    "Command": {"MMLCommand"},
    "ConfigObject": {"ConfigObject"},
    "Feature": {"Feature"},
    "License": {"License"},
    "AtomTask": {"AtomTask"},
    "CompoundTask": {"CompoundTask"},
    "FeatureTask": {"FeatureTask"},
    "Business": {"BusinessDomain", "NetworkScenario", "ConfigurationSolution"},
}
_TYPE_TO_DIR = {t: d for d, types in _DIR_ALLOWED_TYPES.items() for t in types}


# ---------- GET：目录树 / 读文件 ----------

@router.get("/fs/children")
def list_children(path: str = ""):
    """列 path 目录直接子项（懒加载）；path="" → assets 根（真实顶层目录）。"""
    return get_service().store.list_children(_request_path(path, allow_root=True))


@router.get("/fs/file", response_class=PlainTextResponse)
def read_file(path: str):
    svc = get_service()
    path = _request_path(path)
    if not svc.store.exists(path):
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    return PlainTextResponse(svc.store.read(path), media_type="text/markdown; charset=utf-8")


# 二进制白名单：资产 md 里 ![](assets/x.png) 的图片经此端点展示。
# 不含 .svg——可嵌脚本的同源 XSS 面（评审清单 D9，2026-08-19 批准剔除）
_RAW_SUFFIX_WHITELIST = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico"}


@router.get("/fs/raw")
def read_raw(path: str):
    """资产内二进制文件（图片白名单）。路径经 store.abspath 防逃逸；读权限同 /fs/file。"""
    svc = get_service()
    try:
        p = svc.store.abspath(path)  # _resolve 同款防穿越（越界抛 ValueError → 400）
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
    if p.suffix.lower() not in _RAW_SUFFIX_WHITELIST:
        raise HTTPException(status_code=400, detail=f"仅支持图片文件: {p.suffix}")
    return FileResponse(p)


# ---------- PUT：在线编辑（写回原路径）----------

class FileContentIn(BaseModel):
    content: str


@router.put("/fs/file")
def put_file(path: str, req: FileContentIn, request: Request):
    """编辑 md 原文，写回**原路径**。校验 id 未变（改 id 走 rename 避免 [[wikilink]] 断链）。

    位置字段（nf/version/domain/scenario）改了**不会移动文件**——改位置用「移动」。
    """
    _require_assets(request)
    svc = get_service()
    path = _request_path(path)
    with import_lock:
        store = svc.store
        try:
            id_, _typ = validate_md(req.content, svc.registry)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        if store.exists(path):
            try:
                old_id, _ = validate_md(store.read(path), svc.registry)
            except ValueError:
                old_id = None
            if old_id and id_ != old_id:
                raise HTTPException(
                    status_code=400,
                    detail=f"改 id（{old_id} → {id_}）请用「重命名」功能，"
                           f"否则其他文件的 [[{old_id}]] 引用会断链。",
                )
        store.write(path, req.content)
        files_repo.upsert_from_disk(svc.db, svc.store, path)
        files_repo.upsert_parents_from_disk(svc.db, svc.store, [path])
        svc.reindex_path(path)
        svc.reload_index()
    _record(request, "/fs/file", path)
    return {"ok": True, "path": path, "moved_from": None}


# ---------- DELETE ----------

@router.delete("/fs/file")
def delete_file(path: str, request: Request):
    """**软删除**文件或目录：移入回收站（``.trash/{id}/{原路径}``），可还原。

    - 文件/目录统一处理：unindex 受影响 md（目录按前缀遍历子 md）→ move 进回收站 →
      记 trash 行（原路径/时间/操作人）→ 清空父目录。
    - 永久删除走回收站的 purge（admin）。
    路径经 ``store._resolve`` 校验未逃逸 assets 根；空 path（根）拒绝。
    """
    _require_assets(request)
    path = _request_path(path)
    svc = get_service()
    with import_lock:
        store = svc.store
        if not store.exists(path):
            raise HTTPException(status_code=404, detail=f"路径不存在: {path}")
        is_dir = store.abspath(path).is_dir()
        prefix = path.rstrip("/") + "/"
        # unindex 须在 soft_delete 前（list_md 读磁盘，move 后扫不到）
        md_rels = [rel for rel in store.list_md() if rel == path or rel.startswith(prefix)]
        for rel in md_rels:
            svc.unindex_path(rel)
        files_repo.remove_prefix(svc.db, path)  # path 自身 + 子树（文件/目录通吃）
        trash_id = store.soft_delete(path)
        store.cleanup_empty_dirs(path)
        files_repo.upsert_parents_from_disk(svc.db, store, [path])
        trash_repo.insert(
            svc.db, trash_id=trash_id, original_path=path, is_dir=is_dir,
            md_count=len(md_rels),
            deleted_at=datetime.now(timezone.utc).isoformat(),
            deleted_by=getattr(request.state, "user", ""),
        )
        svc.db.commit()
        svc.reload_index()
    _record(request, "/fs/file", path)
    return {"ok": True, "path": path, "trash_id": trash_id, "md_count": len(md_rels)}


# ---------- 回收站 ----------

class TrashIdIn(BaseModel):
    id: str


@router.get("/fs/trash")
def trash_list():
    """回收站列表（资产权限；按删除时间倒序）。"""
    return trash_repo.list_all(get_service().db)


@router.post("/fs/trash/restore")
def trash_restore(req: TrashIdIn, request: Request):
    """从回收站还原到原路径 + 重建索引。原路径被占 → 409（条目保留在回收站）。"""
    _require_assets(request)
    svc = get_service()
    with import_lock:
        item = trash_repo.get(svc.db, req.id)
        if item is None:
            raise HTTPException(status_code=404, detail=f"回收站条目不存在: {req.id}")
        rel = item["original_path"]
        store = svc.store
        try:
            store.restore_from_trash(req.id, rel)
        except FileExistsError as ex:
            raise HTTPException(status_code=409, detail=str(ex))
        except (FileNotFoundError, ValueError) as ex:
            raise HTTPException(status_code=404, detail=str(ex))
        files_repo.upsert_tree(svc.db, svc.store, rel)
        files_repo.upsert_parents_from_disk(svc.db, svc.store, [rel])
        prefix = rel.rstrip("/") + "/"
        for r in store.list_md():
            if r == rel or r.startswith(prefix):
                svc.reindex_path(r)
        trash_repo.delete(svc.db, req.id)
        svc.db.commit()
        svc.reload_index()
    _record(request, "/fs/trash/restore", rel)
    return {"ok": True, "path": rel}


@router.delete("/fs/trash/{trash_id}")
def trash_purge(trash_id: str, request: Request):
    """永久删除单个回收站条目（**admin**，物理删除不可恢复）。"""
    _require_admin(request)
    svc = get_service()
    with import_lock:
        if trash_repo.get(svc.db, trash_id) is None:
            raise HTTPException(status_code=404, detail=f"回收站条目不存在: {trash_id}")
        svc.store.purge_trash(trash_id)
        trash_repo.delete(svc.db, trash_id)
        svc.db.commit()
    _record(request, "/fs/trash/purge", trash_id)
    return {"ok": True}


@router.delete("/fs/trash")
def trash_empty(request: Request):
    """清空回收站（**admin**，物理删除全部条目）。"""
    _require_admin(request)
    svc = get_service()
    with import_lock:
        n = svc.store.purge_all_trash()
        trash_repo.delete_all(svc.db)
        svc.db.commit()
    _record(request, "/fs/trash/empty", str(n))
    return {"ok": True, "purged": n}


# ---------- POST /mkdir ----------

class PathIn(BaseModel):
    path: str


@router.post("/fs/mkdir")
def mkdir(req: PathIn, request: Request):
    _require_assets(request)
    svc = get_service()
    path = _request_path(req.path)
    with import_lock:
        try:
            svc.store.makedirs(path)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex)) from ex
        files_repo.upsert_from_disk(svc.db, svc.store, path)
        files_repo.upsert_parents_from_disk(svc.db, svc.store, [path])
        svc.db.commit()
    _record(request, "/fs/mkdir", path)
    return {"ok": True, "path": path}


# ---------- POST /move（target_dir 驱动）----------

class MoveIn(BaseModel):
    src: str
    target_dir: str
    nf: Optional[str] = None
    version: Optional[str] = None
    domain: Optional[str] = None
    scenario: Optional[str] = None


@router.post("/fs/move")
def move(req: MoveIn, request: Request):
    """移动文件到 target_dir（文件名 = id 不变 → [[wikilink]] 不断）。

    可选 nf/version/domain/scenario 覆盖 frontmatter（位置权威，保持 fm 与目录一致）。
    """
    _require_assets(request)
    svc = get_service()
    src = _request_path(req.src)
    target_dir = _request_path(req.target_dir, allow_root=True)
    with import_lock:
        store = svc.store
        if not store.exists(src):
            raise HTTPException(status_code=404, detail=f"文件不存在: {src}")
        text = store.read(src)
        try:
            id_, _typ = validate_md(text, svc.registry)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        overrides = {k: v for k, v in {
            "nf": req.nf, "version": req.version,
            "domain": req.domain, "scenario": req.scenario,
        }.items() if v}
        if overrides:
            text = rewrite_frontmatter(text, overrides)
        target = _join(target_dir, f"{id_}.md")
        if target == src:
            svc.reload_index()
            return {"ok": True, "new_path": target, "moved": False}
        store.write(target, text)
        files_repo.upsert_from_disk(svc.db, svc.store, target)
        files_repo.upsert_parents_from_disk(svc.db, svc.store, [target])
        store.delete(src)
        files_repo.remove_path(svc.db, src)  # 盘删成功后再删册行（镜像 rename：盘删失败不悬空册删）
        store.cleanup_empty_dirs(src)
        files_repo.upsert_parents_from_disk(svc.db, svc.store, [src])
        svc.reindex_path(target)
        svc.unindex_path(src)
        svc.reload_index()
    _record(request, "/fs/move", src)
    return {"ok": True, "new_path": target, "moved": True}


# ---------- POST /rename（改 id，原目录 + 新文件名）----------

class RenameIn(BaseModel):
    path: str
    new_id: str
    dry_run: bool = False


@router.post("/fs/rename")
def rename(req: RenameIn, request: Request):
    """改逻辑ID：文件名变（**原目录** + new_id.md），全库把正文 ``[[old_id]]`` → ``[[new_id]]``。

    不跨目录（改 nf 段请用「移动」）。``dry_run=True`` 只返回影响范围，不写盘。
    重操作（全库扫描），前端先 dry_run 预览再执行。
    """
    _require_assets(request)
    svc = get_service()
    path = _request_path(req.path)
    with import_lock:
        store = svc.store
        if not store.exists(path):
            raise HTTPException(status_code=404, detail=f"文件不存在: {path}")
        text = store.read(path)
        try:
            old_id, _typ = validate_md(text, svc.registry)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        new_id = req.new_id
        try:
            if not new_id or new_id != new_id.strip():
                raise ValueError("new_id 不能为空或带首尾空白")
            old_nf, old_type, _old_local = split_id(old_id)
            new_nf, new_type, new_local = split_id(new_id)
            if not new_type or not new_local or (new_nf is not None and not new_nf):
                raise ValueError(f"非法逻辑ID（存在空段）: {new_id!r}")
            if new_type != old_type:
                raise ValueError("重命名不能改变对象 type")
            if new_nf != old_nf:
                raise ValueError("重命名不能改变 nf；请使用移动功能")
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex)) from ex
        # target = 原目录 + new_id.md（不跨目录）
        parent_dir = "/".join(path.split("/")[:-1])
        target = _join(parent_dir, f"{new_id}.md")
        if target != path and store.exists(target):
            raise HTTPException(status_code=409, detail=f"目标文件已存在: {target}")
        pattern = re.compile(r"\[\[" + re.escape(old_id) + r"\]\]")
        affected = [rel for rel in store.list_md() if pattern.search(store.read(rel))]
        if req.dry_run:
            return {"dry_run": True, "old_id": old_id, "new_id": new_id,
                    "affected": len(affected), "affected_files": affected,
                    "new_path": target}
        for rel in store.list_md():
            t = store.read(rel)
            nt = pattern.sub(lambda _m: f"[[{new_id}]]", t)
            if nt != t:
                store.write(rel, nt)
                files_repo.upsert_from_disk(svc.db, svc.store, rel)
                svc.reindex_path(rel)
        text2 = store.read(path)  # 重读（可能刚被上面改过 wikilink）
        new_text = rewrite_frontmatter(text2, {"id": new_id})
        store.write(target, new_text)
        files_repo.upsert_from_disk(svc.db, svc.store, target)
        files_repo.upsert_parents_from_disk(svc.db, svc.store, [target])
        svc.reindex_path(target)
        if target != path:
            store.delete(path)
            files_repo.remove_path(svc.db, path)
            store.cleanup_empty_dirs(path)
            files_repo.upsert_parents_from_disk(svc.db, svc.store, [path])
            svc.unindex_path(path)
        svc.reload_index()
    _record(request, "/fs/rename", path)
    return {"ok": True, "old_id": old_id, "new_id": new_id,
            "affected": len(affected), "new_path": target}


# ---------- POST /upload（target_dir 驱动，覆盖 frontmatter）----------

@router.post("/fs/upload")
async def upload(
    request: Request,
    target_dir: str = Form(...),
    nf: str = Form(""),
    version: str = Form(""),
    domain: str = Form(""),
    scenario: str = Form(""),
    files: List[UploadFile] = File(...),
):
    """上传到指定目录 ``target_dir``：每个 md 写到 ``{target_dir}/{id}.md``。

    可选 nf/version/domain/scenario 覆盖 frontmatter（位置权威，使 fm 与目录一致）。
    支持 .md 多选 + .zip（zip 内所有 md 展开）。每个 md 必须有 frontmatter.id。
    不校验 type 与目录的归属（target_dir 是用户在目录树选的真实目录，即权威）。
    """
    _require_assets(request)
    svc = get_service()
    target_dir = _request_path(target_dir, allow_root=True)
    added = updated = skipped = 0
    warnings: list = []
    overrides = {k: v for k, v in {
        "nf": nf, "version": version, "domain": domain, "scenario": scenario,
    }.items() if v}

    # 1. 收集 md（.md 与 .zip）
    md_items: list = []
    for uf in files:
        data = await uf.read()
        origin = _safe_filename(uf.filename or "")
        low = origin.lower()
        if low.endswith(".zip"):
            try:
                with zipfile.ZipFile(io.BytesIO(data)) as z:
                    for n in z.namelist():
                        if n.endswith(".md") and not n.endswith("/"):
                            try:
                                md_items.append((n, z.read(n).decode("utf-8")))
                            except UnicodeDecodeError as ex:
                                warnings.append(f"{n}: 编码错误 {ex}")
                                skipped += 1
            except zipfile.BadZipFile as ex:
                warnings.append(f"{origin}: 非法 zip {ex}")
                skipped += 1
        elif low.endswith(".md"):
            try:
                md_items.append((origin, data.decode("utf-8")))
            except UnicodeDecodeError as ex:
                warnings.append(f"{origin}: 编码错误 {ex}")
                skipped += 1
        else:
            warnings.append(f"{origin}: 跳过非 md/zip 文件")
            skipped += 1

    # 2. 逐 md 写入 target_dir（一把锁，最后一次 rebuild）
    with import_lock:
        store = svc.store
        written_paths: list = []
        for origin, text in md_items:
            try:
                id_, typ = validate_md(text, svc.registry)
            except ValueError as ex:
                warnings.append(f"{origin}: {ex}")
                skipped += 1
                continue
            # 校验 type 属于所选层（防错位：选 Command 但 md 是 AtomTask 等）
            top_dir = target_dir.split("/")[0]
            allowed = _DIR_ALLOWED_TYPES.get(top_dir)
            if allowed is not None and typ not in allowed:
                warnings.append(
                    f"{origin}: type「{typ}」应上传到「{_TYPE_TO_DIR.get(typ, '?')}」层，"
                    f"当前选的是「{top_dir}」，跳过")
                skipped += 1
                continue
            if overrides:
                text = rewrite_frontmatter(text, overrides)
            target = _join(target_dir, f"{id_}.md")
            if store.exists(target):
                updated += 1
            else:
                added += 1
            store.write(target, text)
            files_repo.upsert_from_disk(svc.db, svc.store, target)
            written_paths.append(target)
            svc.reindex_path(target)
        files_repo.upsert_parents_from_disk(svc.db, svc.store, written_paths)
        svc.reload_index()
    _record(request, "/fs/upload", target_dir)
    return {"added": added, "updated": updated, "skipped": skipped, "warnings": warnings}
