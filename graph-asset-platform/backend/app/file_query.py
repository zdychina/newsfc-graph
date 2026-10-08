"""search_files 核心（spec 2026-09-29 §4.3）——文件名搜索 / 目录浏览（find/ls 语义）。

与 graph_query 平行的共享核心：MCP search_files 与 REST POST /files 调用同一
``search_files_core``，错误/护栏不分叉。不搜内容（内容搜索是 search_graph 职责）。

游标=上一页最后一条 path（keyset，path ASC 确定性排序）：深翻页 O(1)、不受并发
增删的页错位影响。total 精确到 ``TOTAL_CAP``（10000），超过置 ``total_is_bounded``。
total = 从游标位置起的剩余条数（翻页递减），非全集绝对数——取剩余口径是性能
考量（count 从游标 PK 范围起扫）。query 模式深翻页每页重扫匹配集（MATCH 驱动 +
temp B-tree 排序），全量遍历场景请用 path 模式（索引干净）。

2 字符 query 走 ``files_fts.name LIKE``（trigram 索引对 <3 字符模式与 ESCAPE
子句均不生效——3.45.3 实测为 name 语料级扫描，1M 行 ~250ms；常见词被 LIMIT
早停兜住，罕见词扫全语料。语料=name 列远小于正文，当前规模可接受；千万级
文件若变慢再评估（如加长度门槛）。⚠️ 前缀短语 ``MATCH '"xx"*'`` 已实证否决。
"""
from datetime import datetime, timezone
from typing import Optional

from .graph_query.contracts import (INDEX_REBUILDING, INVALID_ARGUMENT, INVALID_FILTER,
                                    GraphError, GraphQueryError,
                                    MAX_FILES_AFTER_LEN, MAX_FILES_EXT_LEN,
                                    MAX_FILES_PATH_LEN, MAX_FILES_QUERY_LEN,
                                    err)
from .repos.graph_search_repo import normalize_search_text
from .service import get_service
from .store import normalize_relpath

TOTAL_CAP = 10_000
MAX_QUERY_LEN = 80            # 规范化后（NFKC→strip→casefold）；原始输入上限
                              # = contracts.MAX_FILES_QUERY_LEN（与 REST Field 同值）
ECHO_CAP = 200                # INVALID_FILTER 回显截断（错误消息不放大输入）


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
    if len(query or "") > MAX_FILES_QUERY_LEN:
        raise err(INVALID_ARGUMENT, f"query 原始长度最长 {MAX_FILES_QUERY_LEN} 字符")
    if norm_query and len(norm_query) < 2:
        raise err(INVALID_ARGUMENT,
                  "文件名搜索词规范化后至少 2 个字符（1 字符无法走索引且无意义）")
    if len(norm_query) > MAX_QUERY_LEN:
        raise err(INVALID_ARGUMENT, f"query 规范化后最长 {MAX_QUERY_LEN} 字符")
    ext_n = (ext or "").strip().lstrip(".").lower() or None
    raw_path = (path or "").strip()
    if len(raw_path) > MAX_FILES_PATH_LEN:
        raise err(INVALID_ARGUMENT, f"path 最长 {MAX_FILES_PATH_LEN} 字符")
    try:
        path_n = (normalize_relpath(raw_path) or None) if raw_path else None
    except ValueError as ex:
        raise err(INVALID_ARGUMENT, str(ex), field="path") from None
    if len(ext_n or "") > MAX_FILES_EXT_LEN:
        raise err(INVALID_ARGUMENT, f"ext 最长 {MAX_FILES_EXT_LEN} 字符")
    if len(after or "") > MAX_FILES_AFTER_LEN:
        raise err(INVALID_ARGUMENT, f"after 游标最长 {MAX_FILES_AFTER_LEN} 字符")
    if not (norm_query or path_n or ext_n):
        raise err(INVALID_ARGUMENT,
                  "query / path / ext 至少给一个：query=按文件名搜；path=列目录；"
                  "组合=交集")
    if path_n is not None:
        ok = conn.execute("SELECT 1 FROM files WHERE path=? AND is_dir=1",
                          (path_n,)).fetchone()
        if ok is None:
            if bool(getattr(svc, "files_building", False)):
                raise GraphQueryError(GraphError(
                    code=INDEX_REBUILDING,
                    message="文件索引正在构建，目录尚未可用，请稍后重试",
                    retryable=True,
                    details={"field": "path", "value": path_n[:ECHO_CAP]},
                ))
            raise err(INVALID_FILTER,
                      f"path 不存在或不是目录: {path_n[:ECHO_CAP]}"
                      f"（首启建册期间可能未建全，稍后重试或联系管理员执行 "
                      f"files-reindex）",
                      field="path", value=path_n[:ECHO_CAP])
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
        else:  # 2 字符 → LIKE 扫 name 语料（trigram 索引不生效，见模块 docstring）
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
                      .isoformat(timespec="seconds").replace("+00:00", "Z")
                      if r["mtime"] else None),
            # 键恒在（目录/非对象文件为 None）——MCP/REST wire 同构
            "obj_id": r["obj_id"] if has_obj else None,
            "version": (r["o_version"] or None) if has_obj else None,
        })
    applied = {k: v for k, v in {
        "query": (query or "").strip() or None, "path": path_n, "ext": ext_n,
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
