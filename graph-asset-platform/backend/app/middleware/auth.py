"""鉴权中间件：KEY 反查用户 → 路径权限校验。

- /users/login 豁免（登录前无 user，空 users 也能调）。
- 其他 /api/*：无 KEY/未知 KEY → 401；权限不符 → 403。
- 空 users → 所有 /api/*（非 login）→ 401（取消旁路）。
- caller：can_frontend → web；否则 skill（从用户属性派生，不信请求头）。

v3（MCP 服务化 2026-08-24）：Agent 访问迁移至 MCP（/mcp，独立纯 ASGI 鉴权，
caller=mcp）。2026-09-03 又恢复 SKILL REST 双接口；其权限仍由本中间件校验，
但通道 caller 与 AGENT_USERNAME / AGENT_SESSION_ID 归因由 skill_compat router
按 REST/MCP 公共契约记录。

v4（打点瘦身 2026-08-26）：请求级全量打点移除（见文件尾注释）——中间件回归
纯鉴权职责。
"""
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..users.service import authenticate, check_perm

# REST 图谱接口（2026-09-08 三工具重构；2026-09-09 增 /search；2026-09-29 增
# /files）：401/403 用 GraphError envelope（需求 §5.2），其余路径保持 {"detail"}
# 形态（前端 api.ts 兼容）。
_GRAPH_API_PATHS = ("/api/v1/domains", "/api/v1/md", "/api/v1/search",
                    "/api/v1/files")


def _auth_json(path: str, status: int, code: str, message: str) -> JSONResponse:
    if path in _GRAPH_API_PATHS:
        return JSONResponse(status_code=status, content={"error": {
            "code": code, "message": message, "retryable": False, "details": {}}})
    return JSONResponse(status_code=status, content={"detail": message})


def _need_perm(path: str) -> str:
    """路径 → 所需权限。/users/login 在 dispatch 里先豁免，不走到这。"""
    if path.startswith("/api/v1/users"):  # 用户管理（login 已豁免）
        return "admin"
    if path.startswith("/api/v1/fs"):  # 资产目录（/fs 文件管理 + /fs/upload 上传）
        return "assets"
    if path.startswith("/api/v1/docs"):  # 原始产品文档（资产页签内，D15 用户决策：页签级权限）
        return "assets"
    if path in _GRAPH_API_PATHS:
        # SKILL 兼容双接口（2026-09-03 恢复）：权限与 MCP 一致（can_skill ∨
        # can_frontend；admin 全权）——存量 Agent 配置免改，两套并行
        return "skill"
    if path.startswith("/api/v1/import") or path.startswith("/api/v1/export"):
        return "upload"  # 菜单3：上传/导出
    if path.startswith("/api/v1/tests"):
        return "test"  # 菜单4：测试子系统
    return "frontend"  # 其他前端用的接口（/objects /names /stats /subgraph）


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if not path.startswith("/api/"):
            return await call_next(request)
        # login 豁免（登录前无 user；空 users 也能登录）
        if path == "/api/v1/users/login":
            return await call_next(request)

        key = request.headers.get("X-API-Key", "")
        # 图片端点例外：`<img src>` 由浏览器发起带不了自定义头——接受 ?key= 查询参数
        # （评审清单 D2；仅限 raw 两端点，缩小 key 入 URL 的日志面）
        if not key and (path.startswith("/api/v1/fs/raw") or path.startswith("/api/v1/docs/raw")):
            key = request.query_params.get("key", "")
        user = authenticate(key)
        if user is None:
            return _auth_json(path, 401, "UNAUTHENTICATED",
                              "missing or invalid api key")

        # caller 从用户属性派生（不信任请求头，防伪装躲统计）
        caller = "web" if user.get("can_frontend") else "skill"
        request.state.user = user["username"]
        request.state.caller = caller
        request.state.user_obj = user  # 整 dict，供 router 内 check_perm 二次校验（如 fs 写操作要 upload）

        if not check_perm(user, _need_perm(path)):
            return _auth_json(path, 403, "PERMISSION_DENIED", "permission denied")

        return await call_next(request)


# 请求级全量打点已移除（2026-08-26 打点瘦身·方案B）：任务面板轮询（~2s/行）与
# 图谱浏览读请求曾占打点绝对大头，而统计页只消费 object 级取用行——request 级
# 唯一消费方「行为轨迹」退化为 fs/import 写操作审计（各 router 自行 _record）。
