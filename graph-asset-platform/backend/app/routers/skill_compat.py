"""SKILL 兼容接口（薄 adapter；业务实现走 graph_query 共享核心）。

- 2026-09-08 三工具重构（M1）：/domains、/md 与 MCP get_domains/get_md 调用
  同一 ``get_domains_core`` / ``get_md_core``——业务数据、版本解析、护栏
  （ids 1~100、响应 2MB）、错误不分叉（需求 §10）；本文件只做协议包装与打点。
- 2026-09-09 搜索补 REST 通道（用户决策，覆盖原「搜索只属于 MCP」的非目标）：
  ``POST /search`` 与 MCP ``search_graph`` 调用同一 ``search_graph_core``，
  响应/错误/打点口径一致（caller=skill、endpoint=/search，只记 tool 行）。
- 请求体手动解析（extra=forbid + 统一 INVALID_ARGUMENT envelope 422），
  不走 FastAPI 默认 RequestValidationError 形态（需求 §5.2：REST 图谱路由
  错误体固定为 ``{"error": {GraphError}}``）。
- 归因参数与 MCP 同名且必填：``AGENT_USERNAME`` / ``AGENT_SESSION_ID`` 落
  telemetry 的 ``operator`` / ``session_id`` 专列，不重复写入 params。
- 权限与 MCP 一致：skill（``can_skill`` 或 ``can_frontend``，admin 全权）；
  401/403 的 error envelope 由 AuthMiddleware 对图谱路径分支输出。
"""
import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from ..attribution import telemetry_attribution
from ..file_query import search_files_core
from ..graph_query import contracts as gq
from ..graph_query import read as graph_read
from ..graph_query import search as graph_search
from ..telemetry.recorder import record

router = APIRouter()
_REST_CALLER = "skill"


def _error_response(error: gq.GraphError) -> JSONResponse:
    """REST 图谱路由错误体：{"error": {GraphError}} + HTTP_STATUS 映射（§5.2）。"""
    return JSONResponse(status_code=gq.HTTP_STATUS.get(error.code, 500),
                        content={"error": error.model_dump()})


async def _parse_body(request: Request, model_cls):
    """JSON body → Pydantic 模型（extra=forbid）。失败统一 INVALID_ARGUMENT 422。"""
    try:
        body = await request.json()
    except Exception:
        raise gq.err(gq.INVALID_ARGUMENT, "请求体须为 JSON 对象")
    try:
        return model_cls.model_validate(body)
    except ValidationError as e:
        details = [{"field": ".".join(str(x) for x in x["loc"]),
                    "message": x["msg"]} for x in e.errors()]
        raise gq.err(gq.INVALID_ARGUMENT, "请求参数校验失败", errors=details)


def _record_call(endpoint: str, request: Request, operator: str, session_id: str,
                 params: dict, result: dict) -> None:
    """调用级行（底表默认口径）：一次 HTTP 请求记一条 level=tool 行（与 MCP 工具
    行同构——params/result 摘要与 MCP 对齐，仅 caller/endpoint 不同，§8.6）。"""
    record(endpoint, user=request.state.user, caller=_REST_CALLER,
           level="tool", **telemetry_attribution(operator, session_id),
           params=json.dumps(params, ensure_ascii=False),
           result=json.dumps(result, ensure_ascii=False))


@router.post("/domains")
async def list_domains_with_md(request: Request):
    """一次性返回全部业务域的完整 md（``[{id, type, name, version, md, references}]``）。

    业务域是用户最优先的业务归属定位层——数量少（跨 NF 类，version 恒 null），
    Agent 入口直接取全部域 md。其他层级仍按 ``POST /md`` 沿 ``[[ID]]`` 引用下钻。
    """
    req = None
    try:
        req = await _parse_body(request, gq.RestDomainsRequest)
        items = graph_read.get_domains_core()
    except gq.GraphQueryError as e:
        _record_error("/domains", request, req, e)
        return _error_response(e.error)
    except Exception as e:  # noqa: BLE001 详细异常只进服务端日志
        print(f"[skill_compat] INTERNAL_ERROR /domains: {e!r}", flush=True)
        _record_error("/domains", request, req, e)
        return _error_response(gq.GraphError(
            code=gq.INTERNAL_ERROR, message=gq.INTERNAL_ERROR_MESSAGE))
    attribution = telemetry_attribution(req.AGENT_USERNAME, req.AGENT_SESSION_ID)
    # 调用级 1 行（底表默认口径）+ 对象级每域 1 行（运维页统计热榜用）
    _record_call("/domains", request, req.AGENT_USERNAME, req.AGENT_SESSION_ID,
                 params={}, result={"domains": len(items)})
    for item in items:
        record("/domains", item.id, "BusinessDomain",
               user=request.state.user, caller=_REST_CALLER,
               level="object", **attribution)
    return [item.model_dump() for item in items]


def _record_error(endpoint: str, request: Request, req, e: Exception,
                  params: dict = None) -> None:
    """失败也留痕（与 MCP tool 行同构）：解析成功后的失败记 1 条 error tool 行。
    body 解析失败（无归因可用）不记——与 MCP SDK 参数校验失败不落 tool 行一致。
    message 只回业务错误文本——未知异常统一 "internal error"（str(e) 可能含
    SQL/路径，不落 telemetry/运维页，安全审查 M1）。params 显式传入优先
    （/search 等）；否则 /md 按规范化 ids 自行推导。"""
    if req is None:
        return
    if isinstance(e, gq.GraphQueryError):
        message = e.error.message
        code = e.error.code
    else:
        message = "internal error"
        code = gq.INTERNAL_ERROR
    result = {"error": {"code": code, "message": message[:200]}}
    if params is None:
        if endpoint == "/md" and isinstance(req, gq.RestMdRequest):
            # 规范化 ids（trim 去重）——与 MCP get_md 失败路径同口径
            norm_ids = [k for k in dict.fromkeys((i or "").strip() for i in req.ids) if k]
            params = {"ids": norm_ids, "version": req.version}
        else:
            params = {}
    _record_call(endpoint, request, req.AGENT_USERNAME, req.AGENT_SESSION_ID,
                 params=params, result=result)


@router.post("/md")
async def batch_md(request: Request):
    """批量取多个对象的原始 markdown（与 MCP get_md 完全同构，§10）。

    响应 ``{id: MdSuccess | MdFailure}``（动态 ID map）：成功项含完整元数据 +
    md + references；失败项含 error_code/requested_version/available_versions，
    单项失败不影响其余 id。护栏与 MCP 相同：ids 1~100（去重后）、响应 ≤2MB，
    超限整单失败（413 RESULT_TOO_LARGE）。
    """
    req = None
    try:
        req = await _parse_body(request, gq.RestMdRequest)
        result_map, summary = graph_read.get_md_core(req.ids, req.version)
    except gq.GraphQueryError as e:
        _record_error("/md", request, req, e)
        return _error_response(e.error)
    except Exception as e:  # noqa: BLE001 详细异常只进服务端日志
        print(f"[skill_compat] INTERNAL_ERROR /md: {e!r}", flush=True)
        _record_error("/md", request, req, e)
        return _error_response(gq.GraphError(
            code=gq.INTERNAL_ERROR, message=gq.INTERNAL_ERROR_MESSAGE))
    attribution = telemetry_attribution(req.AGENT_USERNAME, req.AGENT_SESSION_ID)
    # 护栏已过 → 成功 id 逐个留取用点（失败 id 不写 object 行，§8.6）
    for id_, item in result_map.items():
        if item["ok"]:
            record("/md", id_, item["type"], user=request.state.user,
                   caller=_REST_CALLER, level="object", **attribution)
    _record_call("/md", request, req.AGENT_USERNAME, req.AGENT_SESSION_ID,
                 params={"ids": summary["ids"], "version": summary["version"]},
                 result={"ok": summary["ok"], "failed": summary["failed"],
                         "failed_ids": summary["failed_ids"],
                         "bytes": summary["bytes"]})
    return result_map


@router.post("/search")
async def search_graph(request: Request):
    """统一搜索（与 MCP ``search_graph`` 完全同构，2026-09-09 补 REST 通道）。

    请求体 = MCP 工具参数 + 归因字段（terms/match/layer/type/nf/version/
    domain/scenario/page/size + AGENT_USERNAME/AGENT_SESSION_ID，extra=forbid）。
    响应与 MCP content JSON 完全相同（SearchGraphResponse）；错误 envelope：
    422 INVALID_ARGUMENT（枚举/越界/未知字段）、422 INVALID_FILTER(_COMBINATION)
    （带 available_values）、503 INDEX_REBUILDING（retryable）。
    打点：1 条 tool 行（caller=skill、endpoint=/search），无 object 行——与
    MCP §7.9 同口径。
    """
    tel_params: dict = {}
    req = None
    try:
        req = await _parse_body(request, gq.RestSearchRequest)
        tel_params = {k: v for k, v in {
            "terms": req.terms, "match": req.match, "layer": req.layer,
            "type": req.type, "nf": req.nf, "version": req.version,
            "domain": req.domain, "scenario": req.scenario,
            "page": req.page, "size": req.size}.items() if v is not None}
        out = graph_search.search_graph_core(
            terms=req.terms, match=req.match, layer=req.layer, type=req.type,
            nf=req.nf, version=req.version, domain=req.domain,
            scenario=req.scenario, page=req.page, size=req.size)
    except gq.GraphQueryError as e:
        _record_error("/search", request, req, e, params=tel_params)
        return _error_response(e.error)
    except Exception as e:  # noqa: BLE001 详细异常只进服务端日志
        print(f"[skill_compat] INTERNAL_ERROR /search: {e!r}", flush=True)
        _record_error("/search", request, req, e, params=tel_params)
        return _error_response(gq.GraphError(
            code=gq.INTERNAL_ERROR, message=gq.INTERNAL_ERROR_MESSAGE))
    # result 摘要与 MCP search_graph 完全一致（§7.9：不写全文/全部 hit/snippet）
    _record_call("/search", request, req.AGENT_USERNAME, req.AGENT_SESSION_ID,
                 params=tel_params,
                 result={"total": out["total"], "returned": len(out["hits"]),
                         "top_ids": [h["id"] for h in out["hits"][:10]],
                         "matched_terms_count": sum(
                             1 for c in out["diagnostics"]["term_counts"].values()
                             if c > 0),
                         "recovery_codes": out["diagnostics"]["recovery_codes"]})
    return out


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
