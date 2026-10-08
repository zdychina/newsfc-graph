"""telemetry router：SKILL 取用统计/明细导出（受鉴权中间件保护，需 can_frontend）。"""
from fastapi import APIRouter, Query

from ..telemetry.aggregator import aggregate_stats, list_skill_usage

router = APIRouter()


@router.get("/telemetry/stats")
def telemetry_stats(days: int = Query(default=30, ge=1, le=365),
                    start: str = Query(default="", description="时间窗起点（ISO8601 或纯日期 YYYY-MM-DD）；给了 start/end 则优先于 days"),
                    end: str = Query(default="", description="时间窗终点（ISO8601 或纯日期，纯日期=含当天到 23:59:59）")):
    """返回 SKILL 取用频次聚合：{total, by_type, top_ids, timeline, by_user}。
    时间范围：start/end 优先；否则近 days 天。"""
    return aggregate_stats(days, start, end)


@router.get("/telemetry/skill-usage")
def telemetry_skill_usage(
    since: str = Query(default="", description="ISO8601 UTC 游标；空=全量起点。透传上批 next_since 做增量轮询"),
    limit: int = Query(default=1000, ge=1, le=10000, description="单批最大行数"),
    start: str = Query(default="", description="时间窗起点（ISO8601 或纯日期；首轮 since 空时生效）"),
    end: str = Query(default="", description="时间窗终点（ISO8601 或纯日期=含当天）；翻页全程生效"),
    scope: str = Query(default="call", description="call=调用级（默认，每次 REST/MCP 图谱工具调用 1 行，含 params/result）；object=对象级细粒度（每对象 1 行，单独导出用）；all=两类全含"),
):
    """底表导出（供外部系统对接）。

    输出：{events:[{ts, caller, endpoint, obj_id, obj_type, user, operator,
    session_id, level, params, result}], next_since, has_more}（ts 升序）。
    消费方把本批 next_since 作为下次 since 原样回传即可增量推进
    （翻页时继续携带 start/end/scope）。caller 恒为 skill/mcp。
    """
    return list_skill_usage(since, limit, start, end, scope)


@router.get("/telemetry/usage")
def telemetry_usage_table(
    scope: str = Query(default="call", description="call|object|all，同 skill-usage"),
    start: str = Query(default="", description="时间窗起点（ISO8601 或纯日期）"),
    end: str = Query(default="", description="时间窗终点（ISO8601 或纯日期=含当天）"),
    endpoint: str = Query(default="", description="端点多选（逗号分隔，取值：/md,/domains,/search,mcp:get_md,mcp:get_domains,mcp:search_graph,mcp:search_objects,mcp:search_md,mcp:get_object）"),
    q: str = Query(default="", description="账号/工号子串过滤"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=50, ge=1, le=200),
):
    """运维页底表表格：时间倒序 + 服务端分页 + 筛选。返回 {rows, total}。"""
    from ..telemetry.aggregator import list_usage_table
    eps = tuple(e.strip() for e in endpoint.split(",") if e.strip())
    return list_usage_table(scope=scope, start=start, end=end,
                            endpoints=eps, user_like=q.strip(), page=page, size=size)
