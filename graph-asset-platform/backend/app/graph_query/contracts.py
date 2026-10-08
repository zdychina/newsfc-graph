"""图谱查询公共契约：错误模型/错误码 + domains/md 输入输出模型 + 护栏常量。

MCP 与 REST（skill_compat）共用——业务数据、护栏、错误不分叉
（需求 §5/§6/§8/§10，2026-09-08 三工具重构 M1）。

错误码是对外稳定契约：管理员配置、schema 演进都不得改名或复用旧义。
"""
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, RootModel

from ..attribution import AgentSessionId, AgentUsername

# ---------- 错误码（稳定对外契约，需求 §5.2 表） ----------

UNAUTHENTICATED = "UNAUTHENTICATED"
PERMISSION_DENIED = "PERMISSION_DENIED"
INVALID_ARGUMENT = "INVALID_ARGUMENT"
INVALID_FILTER = "INVALID_FILTER"
INVALID_FILTER_COMBINATION = "INVALID_FILTER_COMBINATION"
OBJECT_NOT_FOUND = "OBJECT_NOT_FOUND"
VERSION_NOT_FOUND = "VERSION_NOT_FOUND"
RESULT_TOO_LARGE = "RESULT_TOO_LARGE"
INDEX_REBUILDING = "INDEX_REBUILDING"
SEARCH_TOO_BROAD = "SEARCH_TOO_BROAD"  # deprecated-unused（2026-09-29 超时治理退场，仅保留枚举兼容）
TOOL_DISABLED = "TOOL_DISABLED"
INTERNAL_ERROR = "INTERNAL_ERROR"

# REST HTTP 状态映射（其余错误码仅出现在 MCP tool error 或 /md 单项错误里；
# 2026-09-09 REST /search 上线后新增搜索类码的映射）
HTTP_STATUS = {
    UNAUTHENTICATED: 401,
    PERMISSION_DENIED: 403,
    INVALID_ARGUMENT: 422,
    INVALID_FILTER: 422,
    INVALID_FILTER_COMBINATION: 422,
    RESULT_TOO_LARGE: 413,
    SEARCH_TOO_BROAD: 413,
    INDEX_REBUILDING: 503,
    INTERNAL_ERROR: 500,
}

# 内部错误对外固定消息（详细异常只写服务端日志，禁止回传 traceback/SQL/路径）
INTERNAL_ERROR_MESSAGE = "服务器内部错误，请稍后重试或联系平台管理员"


class GraphError(BaseModel):
    """公共业务错误体（需求 §5.2）。"""

    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    retryable: bool = False
    details: dict = Field(default_factory=dict)


class GraphQueryError(Exception):
    """业务错误异常：adapter 转 REST envelope（HTTP_STATUS）或 MCP ToolError(JSON)。"""

    def __init__(self, error: GraphError):
        super().__init__(error.message)
        self.error = error


def err(code: str, message: str, **details) -> GraphQueryError:
    """构造 GraphQueryError 的简写（details 进 error.details）。"""
    return GraphQueryError(GraphError(code=code, message=message,
                                      details=details or {}))


# ---------- 护栏常量（需求 §8.1/§8.4；REST 与 MCP 同护栏） ----------

MAX_IDS_PER_CALL = 100              # ids 批量上限（100 是硬上限，推荐每批 5~20）
MAX_TOTAL_BYTES = 2 * 1024 * 1024   # 响应总字节上限（2MB）


# ---------- get_domains 输出（需求 §6.3/§6.4） ----------

class DomainItem(BaseModel):
    """业务域条目：全部字段 required，name/version 允许 null。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    name: Optional[str]
    version: Optional[str]
    md: str
    references: list[str]


class DomainsResponse(BaseModel):
    """MCP get_domains 外层包装（REST /domains 返回裸数组，内容一致）。"""

    model_config = ConfigDict(extra="forbid")

    domains: list[DomainItem]


# ---------- get_md 输入/输出（需求 §8.1/§8.2） ----------

class RestDomainsRequest(BaseModel):
    """REST /domains 请求体：归因必填 + extra=forbid（防拼错被静默忽略，§5.1）。"""

    model_config = ConfigDict(extra="forbid")

    AGENT_USERNAME: AgentUsername
    AGENT_SESSION_ID: AgentSessionId


class RestMdRequest(RestDomainsRequest):
    """REST /md 请求体。ids 数量护栏在 core 校验（1~MAX_IDS_PER_CALL，去重后计）；
    条目长度上限在此早拒（防超大 body，安全审查 L2）。"""

    ids: list[Annotated[str, Field(min_length=1, max_length=256)]]
    version: Optional[str] = None


class RestSearchRequest(RestDomainsRequest):
    """REST /search 请求体（2026-09-09 用户决策：搜索补 REST 通道，与 MCP
    search_graph 同契约）。字段约束对齐 MCP 工具 schema：terms 原始项数 1~10、
    每项 1~80；layer/match 用 Literal（非法值 → INVALID_ARGUMENT 422）；
    type/nf/version/domain/scenario 留给 core 动态校验（INVALID_FILTER）。"""

    terms: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field(
        min_length=1, max_length=10)
    match: Literal["any", "all"] = "any"
    layer: Optional[Literal["命令层", "特性层", "任务层", "业务层"]] = None
    type: Optional[str] = None
    nf: Optional[str] = None
    version: Optional[str] = None
    domain: Optional[str] = None
    scenario: Optional[str] = None
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=50)


class MdSuccess(BaseModel):
    """get_md 成功项：全部字段 required；无版本对象 version=null、versions=[]。"""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[True]
    id: str
    type: str
    name: Optional[str]
    nf: Optional[str]
    domain: Optional[str]
    scenario: Optional[str]
    version: Optional[str]
    versions: list[str]
    md: str
    references: list[str]


class MdFailure(BaseModel):
    """get_md 失败项：单项失败不阻断整批（§8.2）。"""

    model_config = ConfigDict(extra="forbid")

    ok: Literal[False]
    id: str
    error_code: str
    error: str
    requested_version: Optional[str]
    available_versions: list[str]


MdResultValue = MdSuccess | MdFailure


class MdResultMap(RootModel):
    """外层动态 ID map（wire 兼容：MCP content[0].text 与 REST body 同构，§8.3）。

    RootModel 返回注解让 FastMCP 生成非空 outputSchema（SDK 1.27.1
    structured_output），content[0].text 仍是原 map JSON——不改 wire shape。
    """

    root: dict[str, MdResultValue]


# ---------- search_graph 输出（需求 §7.8） ----------

class SearchSnippet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    term: str
    text: str


class SearchHit(BaseModel):
    """命中条目：全部字段 required；name/nf/domain/scenario/version 允许 null；
    matched_terms/matched_in/rank_reasons 非空；snippets 0~3 项。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    layer: str
    name: Optional[str]
    nf: Optional[str]
    domain: Optional[str]
    scenario: Optional[str]
    version: Optional[str]
    versions: list[str]
    matched_terms: list[str]
    matched_in: list[str]
    snippets: list[SearchSnippet]
    rank_reasons: list[str]


class SearchFacets(BaseModel):
    """facets=当前结果构成（terms/match+全部 filters 后、分页前的精确计数）——
    不是合法值目录，不用于证明被当前 filter 排除的其他值（§7.7）。"""

    model_config = ConfigDict(extra="forbid")

    layers: dict[str, int]
    types: dict[str, int]
    nfs: dict[str, int]
    versions: dict[str, int]


class TermCountStat(BaseModel):
    """term_stats：hit=候选池内存在命中；capped=该词的候选池触顶。"""

    model_config = ConfigDict(extra="forbid")

    hit: bool
    capped: bool


class SearchDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # 保留既有整数契约；旧调用方可继续直接执行 ``count > 0``。
    term_counts: dict[str, int]
    term_stats: dict[str, TermCountStat]
    recovery_codes: list[str]
    # 跳过正文搜索的短词展示值（Task 10 两档：1 字符恒跳 / 2 字符 metadata_only 档跳）
    body_skipped_short_terms: list[str] = []


class SearchGraphResponse(BaseModel):
    """search_graph 响应：空 map/数组也必须返回（禁止按结果省略字段，§7.8）。"""

    model_config = ConfigDict(extra="forbid")

    terms: list[str]
    match: str
    applied_filters: dict
    total: int
    total_is_bounded: bool = False
    page: int
    size: int
    has_more: bool
    next_page: Optional[int]
    hits: list[SearchHit]
    facets: SearchFacets
    diagnostics: SearchDiagnostics
    suggestions: list[str]


# ---------- search_files 输入/输出（spec 2026-09-29 §4.3） ----------

# search_files 输入长度护栏（REST Field 与 core 校验同值引用，两通道不分叉；
# 规范化后 query 上限 80 只在 core 用，留 file_query 本地）
MAX_FILES_QUERY_LEN = 200     # query 原始输入
MAX_FILES_PATH_LEN = 1024     # path 目录限定
MAX_FILES_EXT_LEN = 64        # ext 扩展名
MAX_FILES_AFTER_LEN = 1024    # after 游标（与 path 同值，独立常量便于演进）


class RestFilesRequest(RestDomainsRequest):
    """REST /files 请求体（与 MCP search_files 同契约）。query/path/ext 至少
    一个（core 校验）；limit 1~500；after=游标（上一页 next_cursor）。长度上限
    引上方 MAX_FILES_* 常量（core 同值校验，两通道不分叉）。"""

    query: Optional[Annotated[str, Field(max_length=MAX_FILES_QUERY_LEN)]] = None
    path: Optional[Annotated[str, Field(max_length=MAX_FILES_PATH_LEN)]] = None
    ext: Optional[Annotated[str, Field(max_length=MAX_FILES_EXT_LEN)]] = None
    recursive: bool = False
    limit: int = Field(default=100, ge=1, le=500)
    after: Optional[Annotated[str, Field(max_length=MAX_FILES_AFTER_LEN)]] = None


class FileHit(BaseModel):
    """文件行：目录行 is_dir=true、obj_id/version 为 null（键恒在——MCP 走
    pydantic 补 null、REST 直接返回 dict，两边 wire 必须逐字节同构）。"""

    model_config = ConfigDict(extra="forbid")

    path: str
    name: str
    ext: str
    is_dir: bool
    size: int
    mtime: Optional[str] = None
    obj_id: Optional[str] = None
    version: Optional[str] = None


class SearchFilesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    files: list[FileHit]
    total: int
    total_is_bounded: bool
    has_more: bool
    next_cursor: Optional[str]
    index_building: bool
    applied_filters: dict
