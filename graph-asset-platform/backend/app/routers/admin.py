"""admin router：运维端点（需 admin 权限）。

- ``POST /admin/reindex``：手动全量重建 DB 索引（兜底；正常写操作已增量维护）。
  md 被外部大批量改、或怀疑 DB 不一致时调用。慢（全量 parse md）。
- ``POST /admin/files-reindex``：手动全量重建 files 户口册（兜底）。assets 被外部
  直拷后漂移、或首启 bootstrap 半途失败时调用（只 stat 扫盘，不 parse md）。
"""
from fastapi import APIRouter, HTTPException, Request

from ..service import get_service
from ..users.service import check_perm

router = APIRouter()


def _require_admin(request: Request) -> None:
    user = getattr(request.state, "user_obj", None)
    if not user or not check_perm(user, "admin"):
        raise HTTPException(status_code=403, detail="需要 admin 权限")


@router.post("/admin/reindex")
def reindex(request: Request):
    """全量重建 DB 索引 + 重载内存（兜底）。返回对象/边计数。"""
    _require_admin(request)
    svc = get_service()
    svc.rebuild()
    return {"ok": True, "objects": len(svc.index.nodes)}


@router.post("/admin/files-reindex")
def files_reindex(request: Request):
    """全量重建 files 户口册（兜底：外部直拷磁盘后漂移；正常写路径已增量维护）。

    走 ``Service.rebuild_files``（自带 import_lock + files_building + 完成标记）。"""
    _require_admin(request)
    svc = get_service()
    return {"ok": True, "files": svc.rebuild_files()}
