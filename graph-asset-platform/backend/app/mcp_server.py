"""MCP 服务（Streamable HTTP，同进程挂载 /mcp；CR：MCP 服务化 2026-08-24）。

设计要点（docs/需求分析与实施计划-MCP服务化-2026-08-24.md）：
- 7 个注册工具：公开 get_domains / get_md / search_graph / search_files；
  legacy search_objects / search_md / get_object 默认 hidden，仅用于旧客户端兼容。
  REST 兼容通道与公开工具共享核心契约。
- 上下文参数 AGENT_USERNAME / AGENT_SESSION_ID：Agent 从沙箱环境变量
  ``_AGENT_USERNAME`` / ``_AGENT_SESSION_ID`` 读取后传入（打点归因，不影响结果；
  SDK 禁止下划线前缀参数名，故工具参数名去掉前导下划线）。
- 鉴权：纯 ASGI 中间件（X-API-Key → skill 权限）——**不用 BaseHTTPMiddleware**
  （其对 SSE 流式响应有缓冲/挂起的历史问题，对抗审查 A1）。
- stateless_http + json_response：无会话状态累积（免 TTL/清理，审查 A3 简化），
  响应纯 JSON（TestClient/普通 HTTP 客户端直测）。
- 打点三层：request 级（鉴权后立即记，caller=mcp，审查 A2）/ tool 级（每调用，
  审查 B3）/ object 级（get_md 每 id、get_domains 每域——取用统计口径）。
"""
from typing import Annotated, Literal, Optional

import json

from pydantic import Field, ValidationError

from fastapi.responses import JSONResponse
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.fastmcp.server import Context
from mcp.server.transport_security import TransportSecuritySettings

from .attribution import AgentSessionId, AgentUsername, telemetry_attribution
from .file_query import search_files_core
from .graph_query import read as graph_read
from .graph_query import search as graph_search
from .graph_query.contracts import (
    INTERNAL_ERROR,
    INTERNAL_ERROR_MESSAGE,
    INVALID_ARGUMENT,
    MAX_FILES_AFTER_LEN,
    MAX_FILES_EXT_LEN,
    MAX_FILES_PATH_LEN,
    MAX_FILES_QUERY_LEN,
    DomainsResponse,
    GraphError,
    GraphQueryError,
    MdResultMap,
    SearchFilesResponse,
    SearchGraphResponse,
)
from .objects_search import list_objects_rows
from .routers.objects import _dump, _dump_edge, _resolve
from .service import get_service
from .telemetry.recorder import record
from .users.service import authenticate, check_perm

_CTX = ("【必传】当前使用者工号：每次调用从环境变量 _AGENT_USERNAME 读取后传入。"
        "仅用于平台取用统计与追溯，不影响调用结果。")
_CTX_SID = ("【必传】当前会话ID：每次调用从环境变量 _AGENT_SESSION_ID 读取后传入。"
            "仅用于平台取用统计与追溯，不影响调用结果。")

# 服务总体说明 canonical（§9.3 短决策树）。admin 补充经 meta.mcp_instructions
# **追加**在 canonical 之后——不再全文覆盖（旧覆盖值已由 v13 迁移备份）。
DEFAULT_INSTRUCTIONS = (
    "配置知识图谱（业务层→任务层→特性层→命令层）查询服务。使用决策树：\n"
    "1. 按业务意图找方案：get_domains 读业务域 md 的全文 [[ID]] 引用后 get_md 下钻。\n"
    "2. 按关键词找对象：search_graph 定位候选（多关键词放 terms 数组，任一命中用 "
    "match=any，全部命中用 match=all）；选定候选后必须 get_md 取完整原文——"
    "snippet 不是权威依据。\n"
    "3. 按文件名找文件 / 列目录：search_files（md 命中回带 obj_id+version，"
    "get_md 读该文件内容；非 md 文件只有元数据；全量获取用 after 游标循环翻页）。\n"
    "4. 参数字段范围：定位 MMLCommand 后 get_md，读取 CommandParameter 段。\n"
    "5. get_md 单项失败不重试整批（失败项回带 available_versions，改版本或移除该 id）。\n"
    "图谱对象的搜索与读取默认作用于每个 ID 的最新现存版本（version 参数可锁定旧版）；"
    "search_files 遍历磁盘上的具体版本文件，不做最新版折叠。"
)

# legacy 工具替换映射（§11.3：deprecated 打点 + 错误指引）
_LEGACY_REPLACEMENT = {"search_objects": "search_graph",
                       "search_md": "search_graph",
                       "get_object": "get_md"}


# ---------- 工具配置动态生效（admin 前端可配，2026-08-25） ----------

def _load_config_safe() -> dict:
    """读 mcp_tools 配置；任何异常回退**上次成功配置**（安全审查 M3：配置读取
    失败不应放宽管理员设置的 hidden/disabled——fail-closed 倾向）；从未成功读过
    才回退空配置（等同历史行为）。每请求读 DB（几行 SELECT，成本可忽略）。"""
    global _last_good_config
    try:
        from .db import get_shared_db
        from .repos import mcp_tools_repo
        cfg = mcp_tools_repo.get_all(get_shared_db())
        _last_good_config = cfg
        return cfg
    except Exception:  # noqa: BLE001 配置面故障不影响工具结果（与打点同哲学）
        print("[mcp] mcp_tools 配置读取失败，使用上次成功配置", flush=True)
        return _last_good_config


_last_good_config: dict = {}


# 公开工具（需求 §1：对外收敛为 get_domains/search_graph/get_md；search_files
# 2026-09-29 注册）。仅这些走严格错误契约（INTERNAL_ERROR 只给通用消息，禁止
# 泄露 traceback/SQL/路径）；legacy 工具（search_objects/search_md/get_object）
# 冻结原错误文本透传（§11.3）。
_PUBLIC_TOOLS = {"get_domains", "get_md", "search_graph", "search_files"}


class _ConfigurableFastMCP(FastMCP):
    """tools/list 三态过滤 + canonical+补充描述；直连调用按三态拦截。

    必须子类覆写：``__init__`` 注册的是 bound method，事后 monkey-patch 无效。
    修改只作用于**返回的 MCPTool 副本**（super().list_tools 每次从注册表重建），
    注册表 Tool 对象保持代码默认——清空补充即回 canonical，无需恢复逻辑。

    - visible：tools/list 展示 + 可直调（默认；v13 迁移已给 legacy 三工具
      建 hidden 行、search_graph 建 visible 行）。
    - hidden：不展示 + 可直调（旧客户端已缓存 tools/schema 仍能调用，§11.1）。
    - disabled：不展示 + TOOL_DISABLED 错误。
    - 管理员补充说明只**追加**在 canonical 之后（§9.2 不覆盖接口契约）；
      inputSchema 注入 additionalProperties=false，与 call_tool 的未知参数
      拦截保持「所见=所执行」一致。
    """

    async def list_tools(self):
        tools = await super().list_tools()
        cfg = _load_config_safe()
        out = []
        for t in tools:
            c = cfg.get(t.name)
            if (c or {}).get("visibility", "visible") != "visible":
                continue  # hidden/disabled → 不出现在 tools/list
            d = (c or {}).get("description") or ""
            if d:
                t.description = (t.description or "") + "\n\n[管理员补充] " + d
            t.inputSchema = {**(t.inputSchema or {}),
                             "additionalProperties": False}
            out.append(t)
        return out

    async def call_tool(self, name: str, arguments: dict, **kwargs):
        c = _load_config_safe().get(name)
        if (c or {}).get("visibility", "visible") == "disabled":
            # ToolError → MCP isError=true（TOOL_DISABLED envelope）
            raise _tool_error_json(GraphError(
                code="TOOL_DISABLED",
                message=f"工具 {name} 已被管理员禁用，如有需要请联系平台管理员开启"
                        + (f"（可改用 {_LEGACY_REPLACEMENT[name]}）"
                           if name in _LEGACY_REPLACEMENT else ""),
                details={"replacement": _LEGACY_REPLACEMENT.get(name)}))
        # 未知参数前置拦截（§5.1 additionalProperties=false 语义）：SDK 生成的
        # arg_model 默认忽略多余键——拼错字段会被静默吞掉，先按 schema 拒绝。
        tool = self._tool_manager.get_tool(name)
        if tool is not None and arguments:
            props = (tool.parameters or {}).get("properties", {})
            unknown = [k for k in arguments if k not in props]
            if unknown:
                raise _tool_error_json(GraphError(
                    code=INVALID_ARGUMENT,
                    message=f"未知参数: {unknown}（允许: {sorted(props)}）",
                    details={"unknown_fields": unknown,
                             "allowed_fields": sorted(props)}))
        try:
            return await super().call_tool(name, arguments, **kwargs)
        except ToolError as e:
            # SDK Tool.run 把工具内异常统一包成 ToolError("Error executing tool
            # ...: <原消息>")，但 ``from e`` 保留 __cause__ 原始异常——按类型还原。
            cause = e.__cause__
            if isinstance(cause, GraphQueryError):
                raise _tool_error_json(cause.error) from None
            if isinstance(cause, ValidationError):
                raise _tool_error_json(GraphError(
                    code=INVALID_ARGUMENT, message="请求参数校验失败",
                    details=_validation_details(cause))) from None
            if name in _PUBLIC_TOOLS and cause is not None:
                print(f"[mcp] INTERNAL_ERROR {name}: {cause!r}", flush=True)
                raise _tool_error_json(GraphError(
                    code=INTERNAL_ERROR, message=INTERNAL_ERROR_MESSAGE,
                    details={"tool": name})) from None
            # legacy 工具 / 无 cause 的 SDK 协议错误：保持原文本透传（冻结兼容）
            raise
        except GraphQueryError as e:  # super() 在 Tool.run 之外抛出的兜底
            raise _tool_error_json(e.error) from None
        except ValidationError as e:
            raise _tool_error_json(GraphError(
                code=INVALID_ARGUMENT, message="请求参数校验失败",
                details=_validation_details(e))) from None
        except Exception as e:  # noqa: BLE001 公开工具 INTERNAL_ERROR 只给通用消息
            if name not in _PUBLIC_TOOLS:
                raise  # legacy 工具冻结原错误文本透传（与 except ToolError 分支同语义）
            print(f"[mcp] INTERNAL_ERROR {name}: {e!r}", flush=True)
            raise _tool_error_json(GraphError(
                code=INTERNAL_ERROR, message=INTERNAL_ERROR_MESSAGE,
                details={"tool": name})) from None


mcp = _ConfigurableFastMCP(
    "graph-asset-platform",
    instructions=DEFAULT_INSTRUCTIONS,
    streamable_http_path="/",   # 挂载于 FastAPI /mcp 之下，最终端点即 /mcp
    stateless_http=True,        # 无会话状态（免 TTL/孤儿清理）
    json_response=True,         # 响应纯 JSON（非 SSE 流）
    # 内网服务 + 平台自带 KEY 鉴权（ASGI 层），关闭 SDK 的 DNS rebinding Host 校验
    # （生产若暴露公网应改配 allowed_hosts 而非关闭）
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ---------- 身份与打点 ----------

def _identity(ctx: Context) -> str:
    """从 MCP 请求头解析 KEY 属主用户名（ASGI 鉴权门已拦截未授权；此处仅取归因）。"""
    try:
        req = ctx.request_context.request
        key = req.headers.get("x-api-key", "") if req is not None else ""
        u = authenticate(key)
        return (u or {}).get("username", "")
    except Exception:  # noqa: BLE001 打点归因绝不影响工具结果
        return ""


_PARAMS_MAX = 2048  # 入参/出参摘要截断上限（观测载荷不与业务等量级）


def _j(v) -> str:
    """入参/出参 → JSON 字符串（超长截断；序列化失败返回空串不阻断）。"""
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return ""
    return s if len(s) <= _PARAMS_MAX else s[:_PARAMS_MAX] + "…(截断)"


def _record_tool(name: str, *, user: str, operator: str, session_id: str,
                 params: Optional[dict] = None, result: Optional[dict] = None) -> None:
    """tool 级打点（2026-08-24 用户决策：输入输出都记录）。

    params=业务入参（上下文参数已有专列不重复）；result=**结构化摘要**而非原始
    载荷（md 全文本在 objects 表，append-only 打点表不存大字段）。
    """
    record(f"mcp:{name}", user=user, caller="mcp", level="tool",
           **telemetry_attribution(operator, session_id),
           params=_j(params or {}), result=_j(result or {}))


def _err_summary(e: Exception) -> dict:
    if isinstance(e, GraphQueryError):
        return {"error": {"code": e.error.code, "message": e.error.message}}
    return {"error": str(e)[:300]}


def _tool_error_json(error: GraphError) -> ToolError:
    """业务/参数/内部错误 → isError=true 且 content[0].text 为 {"error":{GraphError}}
    （需求 §5.2 兼容基线；REST 侧同构 envelope 由 skill_compat 输出）。"""
    return ToolError(json.dumps({"error": error.model_dump()}, ensure_ascii=False))


def _validation_details(e: ValidationError) -> dict:
    return {"errors": [{"field": ".".join(str(x) for x in x["loc"]),
                        "message": x["msg"]} for x in e.errors()]}


# ---------- 工具 ----------

@mcp.tool()
def get_domains(AGENT_USERNAME: Annotated[AgentUsername, Field(description=_CTX)], AGENT_SESSION_ID: Annotated[AgentSessionId, Field(description=_CTX_SID)], ctx: Context = None) -> DomainsResponse:
    """获取配置知识图谱全部业务域（BusinessDomain）的完整 markdown。

    业务域是业务归属的顶层定位层，按业务意图定位（找业务方案/场景归属）时的
    第一步；按关键词找对象请用 search_graph，按文件名找文件请用 search_files。
    先调本工具，按用户需求关键词锁定业务域，再从域 md 的 [[NetworkScenario@*]] 引用下钻场景/方案。
    返回量小（业务域数量少），可放心全量读取。返回
    {domains: [{id, type, name, version, md, references}]}（references=全文 [[ID]]
    引用去重保序）。已知准确对象 ID 时可跳过本工具直接 get_md。

    Args:
        AGENT_USERNAME: 当前使用者工号（从环境变量 _AGENT_USERNAME 读取传入）
        AGENT_SESSION_ID: 当前会话ID（从环境变量 _AGENT_SESSION_ID 读取传入）
    """
    user = _identity(ctx)
    params: dict = {}
    try:
        items = graph_read.get_domains_core()
        attribution = telemetry_attribution(AGENT_USERNAME, AGENT_SESSION_ID)
        for item in items:  # object 级：每域一行（取用统计口径）
            record("mcp:get_domains", item.id, "BusinessDomain", user=user,
                   caller="mcp", level="object", **attribution)
        _record_tool("get_domains", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params,
                     result={"domains": len(items),
                             "ids": [d.id for d in items[:30]]})
        # envelope 保持 {domains:[...]}（REST /domains 返回同内容裸数组）
        return DomainsResponse(domains=items)
    except Exception as e:  # noqa: BLE001 失败也留痕后原样抛出（转 MCP isError）
        _record_tool("get_domains", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params, result=_err_summary(e))
        raise


@mcp.tool()
def get_md(ids: Annotated[list[Annotated[str, Field(min_length=1, max_length=256)]], Field(
               description="对象逻辑 ID 列表（1~100 个，建议每批 5~20 个）。格式：业务层 2 段 "
                           "{Type}@{slug}（如 NetworkScenario@charging）；NF 类 3 段 "
                           "{nf}@{Type}@{name}（如 UDG@AtomTask@SET UPDEFAULTQUOTA，name 可含空格）",
               min_length=1, max_length=100,
               examples=[["UDG@MMLCommand@ADD URR"], ["BusinessDomain@charging"]])],
           AGENT_USERNAME: Annotated[AgentUsername, Field(description=_CTX)],
           AGENT_SESSION_ID: Annotated[AgentSessionId, Field(description=_CTX_SID)],
           version: Annotated[Optional[str], Field(
               description="可选全局版本；不传 = 每个 id 各取最新现存版本。传错版本该 id "
                           "单独计错并回带 available_versions，不影响其余 id")] = None,
           ctx: Context = None) -> MdResultMap:
    """按逻辑 ID 批量获取图谱对象的完整 markdown（含 frontmatter + 正文 + ## 边段）。

    这是读取权威完整内容的唯一入口：参数字段范围、类型、枚举、必填与已有约束
    以 MMLCommand 完整 md（CommandParameter 段）为准，搜索摘要不能替代。
    每项返回 {ok, id, type, name, nf, domain, scenario, version, versions, md,
    references}；失败项 {ok:false, error_code, error, requested_version,
    available_versions}（对象不存在/版本不存在，单项失败不阻断整批）。
    读完 md 后应提取全文 [[ID]] 引用（references 已给出）继续下钻。
    单次最多 100 个 id 且响应总量 ≤2MB，超限报错请分批。
    默认读取每个 id 最新现存版本（version 可全局锁定）。

    Args:
        ids: 对象逻辑 ID 列表（1~100 个）
        AGENT_USERNAME: 当前使用者工号（从环境变量 _AGENT_USERNAME 读取传入）
        AGENT_SESSION_ID: 当前会话ID（从环境变量 _AGENT_SESSION_ID 读取传入）
        version: 可选全局版本；不传 = 每个 id 各取最新现存版本
    """
    user = _identity(ctx)
    # 遥测 params 用规范化 ids（trim 去重）——成功/失败两条路径同口径，
    # 且与 REST /md 对齐（代码审查 MEDIUM）
    norm_ids = [k for k in dict.fromkeys((i or "").strip() for i in ids)
                if k] if isinstance(ids, list) else ids
    params: dict = {"ids": norm_ids, "version": version}
    try:
        result_map, summary = graph_read.get_md_core(ids, version)
        attribution = telemetry_attribution(AGENT_USERNAME, AGENT_SESSION_ID)
        for id_, item in result_map.items():  # 护栏已过 → 成功 id 逐个留取用点
            if item["ok"]:
                record("mcp:get_md", id_, item["type"], user=user, caller="mcp",
                       level="object", **attribution)
        _record_tool("get_md", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID,
                     params={"ids": summary["ids"], "version": summary["version"]},
                     result={"ok": summary["ok"], "failed": summary["failed"],
                             "failed_ids": summary["failed_ids"],
                             "bytes": summary["bytes"]})
        return MdResultMap(root=result_map)
    except Exception as e:  # noqa: BLE001 失败也留痕后原样抛出
        _record_tool("get_md", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params, result=_err_summary(e))
        raise


@mcp.tool()
def search_graph(
    terms: Annotated[list[Annotated[str, Field(min_length=1, max_length=80)]], Field(
        description=("搜索关键词列表（1~10 项，每项 1~80 字符）。每个 term 是一个字面"
                     "关键词或短语：['ADD URR'] 表示含空格的完整短语；['计费欺诈','免费RG'] "
                     "是两个独立关键词（不要把同义词拼成一个长句）。命令名、对象名、"
                     "编号和 md 正文都会被自动搜索"),
        min_length=1, max_length=10,
        examples=[["计费欺诈", "免费流量", "免费RG"], ["ADD URR"], ["N2", "接口配置"]])],
    AGENT_USERNAME: Annotated[AgentUsername, Field(description=_CTX)],
    AGENT_SESSION_ID: Annotated[AgentSessionId, Field(description=_CTX_SID)],
    match: Annotated[Literal["any", "all"], Field(
        description="any=任一 term 命中（默认，召回优先）；all=全部 term 命中"
                    "（位置可分散，精确收窄）")] = "any",
    layer: Annotated[Optional[Literal["命令层", "特性层", "任务层", "业务层"]], Field(
        description="UI 层过滤；与 type 同传按交集（不属于该层会报错而非忽略）")] = None,
    type: Annotated[Optional[str], Field(
        description="对象类型精确过滤（当前合法类型如 MMLCommand/ConfigObject/"
                    "Feature/License/AtomTask/CompoundTask/FeatureTask/Task/"
                    "BusinessDomain/NetworkScenario/ConfigurationSolution）")] = None,
    nf: Annotated[Optional[str], Field(
        description="网元过滤（自动转大写，如 UDG/UNC）。不确定就不传——结果 "
                    "facets.nfs 会给出当前结果里有哪些网元")] = None,
    version: Annotated[Optional[str], Field(
        description="版本精确匹配（如 20.16.0）。不传=只搜每个 ID 最新版本；"
                    "传旧版本可搜旧版正文")] = None,
    domain: Annotated[Optional[str], Field(
        description="业务域 slug 精确匹配（frontmatter 的 domain，如 charging-fraud）")] = None,
    scenario: Annotated[Optional[str], Field(
        description="场景 slug 精确匹配（frontmatter 的 scenario）")] = None,
    page: Annotated[int, Field(description="页码（>=1）", ge=1)] = 1,
    size: Annotated[int, Field(
        description="单页条数（1~50，默认 20——防止单页撑爆上下文）", ge=1, le=50)] = 20,
    ctx: Context = None) -> SearchGraphResponse:
    """统一搜索配置知识图谱（元数据 id/name/name_zh + md 正文），定位候选对象 ID。

    按对象关键词定位 ID 的统一入口（业务方案定位用 get_domains；按文件名定位用 search_files）。
    默认作用于每个 ID 的最新现存版本（version 参数可锁定旧版）。命中量过大时
    正常返回按相关度截断的候选（total_is_bounded=true，宽词 match=all 的交集
    可能不含池外命中，因此不保证正文搜索结果找全）；增加过滤词可提升质量。
    若目标是全量枚举物理文件，请另用 search_files 的 after 游标。返回候选列表 +
    matched_terms/matched_in/snippets/facets/diagnostics：
    - snippet 只是定位线索，**不是权威依据**——选定候选后必须调 get_md 取完整
      原文（MMLCommand 参数字段范围以 get_md 返回的 CommandParameter 段为准）；
    - facets 描述当前结果构成（不是全局合法值目录）；
    - 传错 layer/type/nf/version/domain/scenario 返回结构化错误 + 合法值，
      不是静默 0 结果；零结果时看 diagnostics.recovery_codes 与 suggestions。
    """
    user = _identity(ctx)
    params = {k: v for k, v in {"terms": terms, "match": match, "layer": layer,
                                "type": type, "nf": nf, "version": version,
                                "domain": domain, "scenario": scenario,
                                "page": page, "size": size}.items()
              if v is not None}
    try:
        out = graph_search.search_graph_core(
            terms=terms, match=match, layer=layer, type=type, nf=nf,
            version=version, domain=domain, scenario=scenario, page=page, size=size)
        _record_tool("search_graph", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params,
                     result={"total": out["total"], "returned": len(out["hits"]),
                             "top_ids": [h["id"] for h in out["hits"][:10]],
                             "matched_terms_count": sum(
                                 1 for c in out["diagnostics"]["term_counts"].values()
                                 if c > 0),
                             "recovery_codes": out["diagnostics"]["recovery_codes"]})
        return SearchGraphResponse(**out)
    except Exception as e:  # noqa: BLE001 失败也留痕后原样抛出
        _record_tool("search_graph", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params,
                     result=_err_summary(e))
        raise


@mcp.tool()
def search_files(
    AGENT_USERNAME: Annotated[AgentUsername, Field(description=_CTX)],
    AGENT_SESSION_ID: Annotated[AgentSessionId, Field(description=_CTX_SID)],
    query: Annotated[Optional[str], Field(
        max_length=MAX_FILES_QUERY_LEN,
        description="文件名关键词（子串，不分大小写；规范化后 2~80 字符）。"
                    "如 'ADD URR' 命中 UDG@MMLCommand@ADD URR.md；"
                    "3 字符以上走索引，2 字符为语料扫描（大库下罕见词稍慢）")] = None,
    path: Annotated[Optional[str], Field(
        max_length=MAX_FILES_PATH_LEN,
        description="目录限定（相对 assets 根，如 'Command/UDG'）。默认列直接子项"
                    "（ls 语义，含子目录行）；recursive=true 时递归取子树全部文件"
                    "（find -type f 语义）。不传=全库")] = None,
    ext: Annotated[Optional[str], Field(
        max_length=MAX_FILES_EXT_LEN,
        description="扩展名精确过滤（小写，如 'md'/'png'）")] = None,
    recursive: Annotated[bool, Field(
        description="path 模式下递归子树（仅文件行）；默认 False=直接子项")] = False,
    limit: Annotated[int, Field(
        description="单页条数（1~500，默认 100）", ge=1, le=500)] = 100,
    after: Annotated[Optional[str], Field(
        max_length=MAX_FILES_AFTER_LEN,
        description="游标：传上一页返回的 next_cursor 翻下一页；循环直到 "
                    "has_more=false 即拿全量。注意 total 是从游标位置起的剩余"
                    "条数（翻页递减），非全集绝对数")] = None,
    ctx: Context = None) -> SearchFilesResponse:
    """按文件名搜索 / 按目录浏览资产库文件（find/ls 语义，不搜内容）。

    覆盖 assets 下所有文件（含图片等非 md）。三个用法：
    - query='关键词'：全库按文件名搜（等价 find -name '*关键词*'）；
    - path='Command/UDG'：列直接子项（等价 ls）；
    - path + recursive=true：子树全量文件（等价 find <dir> -type f；全量获取用
      after 游标循环翻页直到 has_more=false——全量遍历请用 path 模式，query
      模式深翻页每页成本更高）。
    按内容搜对象请用 search_graph（本工具不搜正文）。
    md 文件命中回带 obj_id+version——用 get_md(ids=[obj_id], version=version)
    读**该文件**的完整内容（不带 version 会取最新版，可能不是这个文件）；
    非 md 文件（图片等）只有元数据。
    query/path/ext 至少给一个；空 path（path='' 或 '/'）不代表 assets 根目录，
    不能单独用于列根；total 精确到 10000，超过时 total_is_bounded=true。
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


@mcp.tool()
def search_objects(AGENT_USERNAME: Annotated[AgentUsername, Field(description=_CTX)], AGENT_SESSION_ID: Annotated[AgentSessionId, Field(description=_CTX_SID)], q: Optional[str] = None,
                   layer: Optional[str] = None, type: Optional[str] = None,
                   nf: Optional[str] = None, version: Optional[str] = None,
                   domain: Optional[str] = None, scenario: Optional[str] = None,
                   page: int = 1, size: int = 50, ctx: Context = None) -> dict:
    """已废弃（deprecated）：请改用 search_graph。仅搜索图谱对象元数据。

    支持按层（命令层/特性层/任务层/业务层）、类型、网元、版本、业务域过滤。
    用于：已知大致名称/编号时定位对象 ID，或浏览某层某网元的对象清单。
    兼容旧客户端保留；新调用不要再使用本工具或 search_md。

    Args:
        AGENT_USERNAME: 当前使用者工号（从环境变量 _AGENT_USERNAME 读取传入）
        AGENT_SESSION_ID: 当前会话ID（从环境变量 _AGENT_SESSION_ID 读取传入）
        q: 关键词（id/name/name_zh 子串，不区分大小写）
        layer: UI 层过滤（命令层/特性层/任务层/业务层）
        type: 单类型过滤（优先于 layer，如 MMLCommand）
        nf: 网元过滤（如 UDG）
        version: 版本精确匹配
        domain: 业务域过滤
        scenario: 场景过滤
        page: 页码（默认 1）
        size: 页大小（默认 50）
    """
    user = _identity(ctx)
    params = {k: v for k, v in {"q": q, "layer": layer, "type": type, "nf": nf,
                                "version": version, "domain": domain,
                                "scenario": scenario, "page": page, "size": size}.items()
              if v is not None}
    try:
        rows, total = list_objects_rows(q=q, layer=layer, type=type, nf=nf, version=version,
                                        domain=domain, scenario=scenario)
        start = (page - 1) * size
        page_rows = rows[start:start + size]
        _record_tool("search_objects", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params,
                     result={"total": total, "returned": len(page_rows),
                             "top_ids": [r["id"] for r in page_rows[:10]],
                             "deprecated": True, "replacement": "search_graph"})
        return {"total": total, "rows": page_rows}
    except Exception as e:  # noqa: BLE001
        _record_tool("search_objects", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params, result=_err_summary(e))
        raise


@mcp.tool()
def search_md(q: str, AGENT_USERNAME: Annotated[AgentUsername, Field(description=_CTX)], AGENT_SESSION_ID: Annotated[AgentSessionId, Field(description=_CTX_SID)],
              layer: Optional[str] = None, type: Optional[str] = None,
              nf: Optional[str] = None, version: Optional[str] = None,
              limit: int = 20, offset: int = 0, ctx: Context = None) -> dict:
    """已废弃（deprecated）：请改用 search_graph 搜索对象及 markdown 正文。

    用于：①用户意图式召回——不确定对象 ID 时按业务关键词找相关 md；②查某个
    参数/命令/特性名出现在哪些对象里。返回片段摘要而非全文，选中后用 get_md
    取完整 md。支持层/类型/网元/版本过滤。默认只搜每个对象的最新版本。

    Args:
        q: 正文关键词（≥3 字符走相关度排序；更短按字面匹配）
        AGENT_USERNAME: 当前使用者工号（从环境变量 _AGENT_USERNAME 读取传入）
        AGENT_SESSION_ID: 当前会话ID（从环境变量 _AGENT_SESSION_ID 读取传入）
        layer: UI 层过滤（命令层/特性层/任务层/业务层）
        type: 单类型过滤（优先于 layer）
        nf: 网元过滤
        version: 版本锁定（不传 = 只搜每个对象最新版本）
        limit: 返回条数（默认 20）
        offset: 偏移（默认 0）
    """
    user = _identity(ctx)
    params = {k: v for k, v in {"q": q, "layer": layer, "type": type, "nf": nf,
                                "version": version, "limit": limit,
                                "offset": offset}.items() if v is not None}
    try:
        res = get_service().search_md(q, layer=layer, type=type, nf=nf, version=version,
                                      limit=limit, offset=offset)
        _record_tool("search_md", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params,
                     result={"total": res["total"], "returned": len(res["hits"]),
                             "top_ids": [h["id"] for h in res["hits"][:10]],
                             "deprecated": True, "replacement": "search_graph"})
        return res
    except Exception as e:  # noqa: BLE001
        _record_tool("search_md", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params, result=_err_summary(e))
        raise


@mcp.tool()
def get_object(id: str, AGENT_USERNAME: Annotated[AgentUsername, Field(description=_CTX)], AGENT_SESSION_ID: Annotated[AgentSessionId, Field(description=_CTX_SID)],
               version: Optional[str] = None, ctx: Context = None) -> dict:
    """已废弃（deprecated）：请改用 get_md 获取权威原文与全文引用。

    需要看对象的关联关系（如某 FeatureTask 引用了哪些命令）时用。
    ⚠ 出边仅含 curated 边（## 边段声明的显式边），完备遍历必须读 md 全文提取
    [[ID]] 引用。要完整原文用 get_md。

    Args:
        id: 对象逻辑 ID
        AGENT_USERNAME: 当前使用者工号（从环境变量 _AGENT_USERNAME 读取传入）
        AGENT_SESSION_ID: 当前会话ID（从环境变量 _AGENT_SESSION_ID 读取传入）
        version: 版本锁定（不传 = 最新现存版本；版本缺失回带可用版本列表）
    """
    user = _identity(ctx)
    params = {k: v for k, v in {"id": id, "version": version}.items() if v is not None}
    try:
        obj = _resolve(id, version)
        idx = get_service().index
        out = {
            **_dump(obj),
            "versions": idx.versions_of(obj.id),
            "out_edges": [_dump_edge(e) for e in idx.out_edges(obj.id, obj.version)],
        }
        _record_tool("get_object", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params,
                     result={"type": obj.type, "version": obj.version,
                             "out_edges": len(out["out_edges"]),
                             "deprecated": True, "replacement": "get_md"})
        return out
    except Exception as e:  # noqa: BLE001
        _record_tool("get_object", user=user, operator=AGENT_USERNAME,
                     session_id=AGENT_SESSION_ID, params=params, result=_err_summary(e))
        raise


# ---------- 配置快照与总体说明应用 ----------

# 默认描述快照（注册表 Tool 的 docstring 描述；GET /mcp-tools 的 default_description
# 数据源）。放在全部 @mcp.tool() 注册之后。
_DEFAULT_DESCRIPTIONS = {t.name: t.description
                         for t in mcp._tool_manager.list_tools()}


def apply_instructions(supplement: str) -> None:
    """应用总体说明**补充**（''=纯 canonical，§9.3）。stateless 模式每请求经
    ``create_initialization_options()`` 读 ``_mcp_server.instructions``——改即生效。"""
    supp = (supplement or "").strip()
    mcp._mcp_server.instructions = (DEFAULT_INSTRUCTIONS + "\n\n[管理员补充] " + supp
                                    if supp else DEFAULT_INSTRUCTIONS)


# ---------- 纯 ASGI 鉴权（审查 A1：BaseHTTPMiddleware 对 SSE 流有缓冲/挂起风险） ----------

class MCPAuthMiddleware:
    """X-API-Key 鉴权 + 请求级打点（鉴权通过后立即记，不等响应完成——审查 A2）。

    纯 ASGI 实现：只读 header，不缓冲 body、不包装响应流。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                   for k, v in scope.get("headers", [])}
        user = authenticate(headers.get("x-api-key", ""))
        if user is None:
            await JSONResponse(status_code=401,
                               content={"detail": "missing or invalid api key"}
                               )(scope, receive, send)
            return
        if not check_perm(user, "skill"):
            await JSONResponse(status_code=403,
                               content={"detail": "permission denied"}
                               )(scope, receive, send)
            return
        # /mcp 请求级打点已移除（2026-08-26 打点瘦身·方案B）：无任何消费方；
        # 取用观测由 tool 级（mcp_server 工具装饰器内）+ object 级承担
        await self.app(scope, receive, send)


# ---------- 可重建挂载（SDK 限制：session manager 的 run() 仅可进一次/实例） ----------

class _RebuildableMCPMount:
    """lifespan 启动时 ``rebuild_session_manager()`` 重建；本包装器实时跟随当前 app。

    测试（TestClient 每用例启停 lifespan）与生产（单次启停）都需要：同一 FastMCP
    实例的工具注册不变，仅 session manager 换新。
    """

    def __init__(self, mcp_instance):
        self._mcp = mcp_instance
        self._app = None

    def rebuild(self) -> None:
        self._mcp._session_manager = None  # SDK 无公开重置口（1.27.1）
        self._app = self._mcp.streamable_http_app()

    async def __call__(self, scope, receive, send):
        if self._app is None:
            self.rebuild()
        await self._app(scope, receive, send)


_mount = _RebuildableMCPMount(mcp)


class _RootPathASGI:
    """配合 FastAPI ``Route("/mcp")`` 挂载：子 app 以根路由注册，改写子路径为 "/"。

    （Starlette ``Mount("/mcp")`` 对无尾斜杠的精确路径匹配不到——子路径为空串，
    子 app 的 Route("/") 不命中；显式 Route + 路径改写彻底避开该坑。）
    """

    def __init__(self, inner):
        self._inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "path": "/"}
        await self._inner(scope, receive, send)


asgi_app = MCPAuthMiddleware(_RootPathASGI(_mount))


def rebuild_session_manager():
    """lifespan 启动时调用：重建 session manager 并返回（供 ``async with .run()``）。"""
    _mount.rebuild()
    return mcp.session_manager
