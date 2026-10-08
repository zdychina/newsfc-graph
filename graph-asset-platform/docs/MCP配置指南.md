# MCP 配置指南

> 图谱查询 MCP 服务（`/mcp`，4 个公开工具）的完整接入配置：服务端启动 → 获取 API KEY →
> 各类客户端配置 → 云 Agent 用户身份传递 → 验证排障。
> 2026-09-08 三工具重构：公开工具收敛为 get_domains / search_graph / get_md；
> 旧三工具（search_objects/search_md/get_object）hidden 兼容（deprecated）。
> 2026-09-29：新增 search_files（文件名搜索/目录浏览，find/ls 语义，不搜内容）。
> 工具参数与返回明细见 [../图谱平台接口文档.md](../图谱平台接口文档.md) §2。

## 0. 接入模型（一图看懂）

```
Agent（Claude Code / Cursor / 云 Agent）
  │
  │  header：X-API-Key（鉴权，客户端配置一次，所有用户共用）
  ▼
http://<平台地址>:8000/mcp
  │
  ├─ get_domains      全部业务域 md（业务方案定位入口）
  ├─ search_graph     统一搜索（元数据+正文；多关键词 terms + any/all）
  ├─ search_files     文件名搜索 / 目录浏览（find/ls 语义；不搜内容）
  └─ get_md           批量取对象 md（权威原文；沿 references/[[ID]] 下钻）
  ── hidden 兼容：search_objects / search_md / get_object（deprecated）
        ▲
        └─ 每次工具调用必传：AGENT_USERNAME / AGENT_SESSION_ID（用户工号+会话ID，
           从沙箱环境变量读取后传入——打点归因用，不影响结果）
```

鉴权走 **header（静态，配一次）**；用户身份走 **工具参数（动态，每次调用传）**——两者分离，
云 Agent 平台只需配一次 KEY，就能区分每个实际使用者。

---

## 1. 服务端准备

```bash
cd graph-asset-platform/backend
python -m uvicorn app.main:app --port 8000 --host 0.0.0.0
```

- MCP 端点：`http://<服务器IP>:8000/mcp`（与 Web 界面同端口同进程，无独立服务）
- 传输协议：**Streamable HTTP**（stateless + 纯 JSON 响应；无需会话保持，无 SSE 长连接）
- 内网部署注意：`--host 0.0.0.0` 才能让其他机器访问（默认只监听本机）

## 2. 获取 API KEY

MCP 鉴权要求 `skill` 权限：**`can_skill` 或 `can_frontend` 任一为真**（`is_admin` 隐含全权）。

### 方式一：Web 界面（推荐）

admin 登录平台 → 用户管理 → 新建/编辑用户：
- 勾选 `skill` 权限（can_skill）
- KEY 可让系统生成，也可**自定义**（规则：≥8 位、不含空格、无前缀要求、全局唯一）

### 方式二：管理 API（admin 的 KEY 调用）

```bash
# 新建用户（KEY 自动生成，响应里返回）
curl -X POST http://127.0.0.1:8000/api/v1/users \
  -H "X-API-Key: <ADMIN_KEY>" -H "Content-Type: application/json" \
  -d '{"username": "agent-svc", "can_skill": true}'

# 或给已有用户自定义 KEY
curl -X PATCH http://127.0.0.1:8000/api/v1/users/agent-svc \
  -H "X-API-Key: <ADMIN_KEY>" -H "Content-Type: application/json" \
  -d '{"set_key": "my-key-at-least-8-chars"}'
```

> KEY 即凭证等价于密码：请通过内网安全渠道分发，不要提交进代码库。
> 首次部署自动生成的 admin KEY 以 `gap_` 开头（见 README「启动」节）。

## 3. 客户端配置

### 3.1 Claude Code（命令行，一次配置）

```bash
claude mcp add --transport http graph http://<平台地址>:8000/mcp \
  --header "X-API-Key: <你的KEY>"
```

### 3.2 Claude Code（项目级 `.mcp.json`，随仓库共享）

```json
{
  "mcpServers": {
    "graph": {
      "type": "http",
      "url": "http://<平台地址>:8000/mcp",
      "headers": { "X-API-Key": "<你的KEY>" }
    }
  }
}
```

### 3.3 Cursor / 通用 MCP JSON 格式

```json
{
  "mcpServers": {
    "graph": {
      "url": "http://<平台地址>:8000/mcp",
      "headers": { "X-API-Key": "<你的KEY>" }
    }
  }
}
```

### 3.4 云 Agent 平台（平台级配置一次）

在云 Agent 的 MCP 服务配置页填：

| 配置项 | 值 |
|---|---|
| 类型 / 传输 | Streamable HTTP（或 "HTTP"，**不是** stdio / SSE-only） |
| URL | `http://<平台地址>:8000/mcp` |
| 鉴权 Header | `X-API-Key: <平台统一KEY>`（所有用户共用这条连接） |

用户身份**不在这里配**——见下节，随每次工具调用动态传入。

---

## 4. 用户身份传递（云 Agent 场景，关键）

云 Agent 的 MCP 连接是平台级共用的，但每个使用者的工号/会话ID不同——因此
**身份不进 header，进工具参数**。全部 4 个公开工具都有两个必填参数：

| 工具参数 | 取值来源（沙箱环境变量） | 用途 |
|---|---|---|
| `AGENT_USERNAME` | `_AGENT_USERNAME` | 使用者工号 → 打点 `operator`（谁在用） |
| `AGENT_SESSION_ID` | `_AGENT_SESSION_ID` | 会话ID → 打点 `session_id`（会话维度统计） |

两个工具参数都会去除首尾空白并拒绝空值；工号最长 64 字符，会话 ID 最长
128 字符。它们是调用方声明的统计归因元数据，不参与鉴权。

### 给 Agent 的系统提示词模板（直接粘贴到云 Agent 指令配置）

```text
【图谱 MCP 工具调用规范】
调用 graph 服务的任何工具（get_domains / search_graph / search_files / get_md）时，必须同时传入：
- AGENT_USERNAME：从环境变量 _AGENT_USERNAME 读取的当前用户工号
- AGENT_SESSION_ID：从环境变量 _AGENT_SESSION_ID 读取的当前会话ID
这两个参数仅用于平台取用统计与追溯，不影响查询结果，但不可省略。

【图谱查询建议路径】
1. 已知准确对象 ID：直接 get_md。
2. 不知道 ID（按内容找对象）：search_graph 定位候选（多个关键词放 terms 数组：
   ["计费欺诈","免费RG"]，默认 match=any 任一命中即召回；"ADD URR" 这类带空格的
   算一个短语 term）；选定候选后必须 get_md 取完整原文——搜索摘要不是权威依据。
3. 按文件名找文件 / 列目录：search_files（query=按文件名搜；path=列目录；
   全量获取用 after 游标循环翻页直到 has_more=false）；md 文件命中回带
   obj_id+version，get_md(ids=[obj_id], version=version) 读该文件内容。
4. 业务方案定位：get_domains 看全部业务域，沿 references/[[ID]] 引用 get_md
   逐层下钻（业务层→任务层→特性层→命令层）。
5. 参数字段范围：定位 MMLCommand 后 get_md，以 CommandParameter 段为准。
```

> 说明：Agent 的工具参数名不允许下划线开头，所以环境变量是 `_AGENT_USERNAME`
> 而工具参数是 `AGENT_USERNAME`——名字去掉了下划线前缀，值原样传递。
> 若云平台 MCP 配置支持 header 环境变量插值（如 `${_AGENT_USERNAME}`），
> 可评估改为 header 注入（不依赖 LLM 自觉传参，更可靠）——当前按传参设计。

## 5. 工具速查

| 工具 | 必填参数 | 选填参数 | 一句话用途 |
|---|---|---|---|
| `get_domains` | 工号 + 会话ID | — | 全部业务域 md + references（入口，量小可全读） |
| `search_graph` | `terms[]`(1~10) + 工号 + 会话ID | `match`/`layer`/`type`/`nf`/`version`/`domain`/`scenario`/`page`/`size` | 统一搜索（元数据+正文），定位候选 ID |
| `search_files` | `query`/`path`/`ext` 至少一个 + 工号 + 会话ID | `recursive`/`limit`(1~500，默认100)/`after`(游标) | 文件名搜索 / 目录浏览（find/ls 语义，**不搜内容**） |
| `get_md` | `ids[]`(1~100) + 工号 + 会话ID | `version` | 批量取对象完整 md（权威原文；总量≤2MB） |

兼容工具（deprecated，默认不出现在 tools/list，旧客户端仍可直调）：`search_objects`（元数据搜索）/ `search_md`（正文短语搜索）/ `get_object`（单对象+出边）——新接入不要使用。

### 5.1 search_files：ls 与 find 语义对照

| 传参 | 等价 shell | 语义 |
|---|---|---|
| `query="ADD URR"` | `find -name '*ADD URR*'` | 全库按文件名搜（子串、不分大小写；3 字符以上走索引，2 字符语料扫描稍慢） |
| `path="Command/UDG"` | `ls Command/UDG` | 列**直接子项**（含子目录行，目录行 `obj_id`/`version` 为 null） |
| `path="Command/UDG"` + `recursive=true` | `find Command/UDG -type f` | 递归取子树**全部文件**（仅文件行） |
| 上述任一 + `ext="md"` | `... -name '*.md'` | 扩展名精确过滤（组合=交集；`query`/`path`/`ext` 至少给一个） |

覆盖 assets 下全部文件（含图片等非 md）。md 文件命中回带 `obj_id`+`version`——
`get_md(ids=[obj_id], version=version)` 读**该文件**的完整内容（不带 version
会取最新版，可能不是这个文件）；非 md 文件只有元数据。

**游标翻页（全量获取）**：`after` = 上一页返回的 `next_cursor`，循环直到
`has_more=false`；全量遍历优先用 `path` 模式（query 模式深翻页每页成本更高）。

```text
第一页：  search_files(path="Command/UDG", recursive=true, limit=500, +工号/会话ID)
        → has_more=true, next_cursor="Command/UDG/20.16.0/UDG@MMLCommand@XXX.md"
下一页：  search_files(..., after="Command/UDG/20.16.0/UDG@MMLCommand@XXX.md")
循环直到 has_more=false
```

> `total` = 从当前游标位置起的**剩余**条数（翻页递减，非全集绝对数），精确到
> 10000（超过置 `total_is_bounded=true`）——全量遍历以 `has_more=false` 为准。
> `index_building=true` 表示首启文件户口册仍在建（结果可能不全，稍后重试），
> 不是错误。参数与返回明细见接口文档 §2.4。

## 6. 验证与排障

### 6.1 连通性验证（curl / Postman，无需 MCP 客户端）

```bash
# initialize —— 返回 JSON-RPC 响应即通（stateless，无需会话）
curl -s http://<平台地址>:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-API-Key: <你的KEY>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{
        "protocolVersion":"2025-03-26","capabilities":{},
        "clientInfo":{"name":"curl","version":"0.0.0"}}}'

# tools/list —— 应返回 4 个公开工具（get_domains/search_graph/search_files/get_md）
curl -s http://<平台地址>:8000/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "X-API-Key: <你的KEY>" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}'
```

### 6.2 错误对照

| 现象 | 原因 | 处理 |
|---|---|---|
| HTTP 401 `missing or invalid api key` | 未带 / KEY 错误 | 检查 header 名大小写不敏感、KEY 是否被重置 |
| HTTP 403 `permission denied` | 用户无 skill 权限 | admin 给该用户勾 can_skill（或 can_frontend） |
| 客户端连接超时 | 端口未放通 / 未 `--host 0.0.0.0` | 先 `curl http://<IP>:8000/docs` 验证 Web 通 |
| 工具报「全文索引重建中」(INDEX_REBUILDING) | 平台启动初 FTS 后台重建 | 稍等重试（重建中搜索明确报错，不返回残缺结果） |
| INVALID_FILTER / INVALID_FILTER_COMBINATION | search_graph 传了非法过滤值或组合 | 按 details.available_values 修正，不要原样重试 |
| search_files 报 INVALID_FILTER（path 不存在） | path 目录不存在/不是目录，或首启建册未建全 | 确认路径（相对 assets 根、正斜杠）；建册期稍后重试或联系管理员执行 files-reindex |
| 命中量过大（`total_is_bounded=true`，非错误） | 宽词候选池触顶被采样截断（原 SEARCH_TOO_BROAD 已退场，不再报错） | 增加 nf/type/layer 过滤或减少 terms；全量获取改用 search_files 的 after 游标 |
| get_md 报 ids/总量超限 | >100 id 或响应 >2MB | 按提示分批，每批 ≤50 个 id |
| MCP 工具 isError=true | 参数校验/业务错误 | content[0].text 是 {"error":{code,message,...}} JSON，按错误码修正 |

### 6.3 打点核对（确认归因是否生效）

调用几次工具后，admin 在平台统计页（或 telemetry 表）按 `caller=mcp` 过滤：
`operator` 列 = 传入的工号，`session_id` 列 = 传入的会话ID，
`params`/`result` 列 = 该次调用的入参 JSON 与出参摘要（截断 2KB）。

若 `operator` 为空 → Agent 没传 `AGENT_USERNAME`，检查 §4 系统提示词是否配置。

---

## 7. 管理员工具配置（前端「MCP 工具」页）

admin 登录平台 → 顶部「MCP 工具」页（仅 admin 可见），可配置：

| 配置项 | 说明 | 生效语义 |
|---|---|---|
| visibility 三态 | 每工具 visible / hidden / disabled | **visible**=展示+可调；**hidden**=不展示但仍可直调（兼容已缓存旧 schema 的客户端）；**disabled**=不展示+返回 TOOL_DISABLED |
| 补充说明 | tools/list 里追加在 canonical 描述之后的文字 | **只追加不覆盖**（接口契约/schema/错误码由代码固定）；清空 = 仅 canonical |
| 服务总体说明 | initialize 时 Agent 收到的 instructions | 同样**只追加**在 canonical 决策树之后；清空 = 仅 canonical |

要点：

- **全局生效**：一套配置对所有 API KEY / 所有用户生效（不做按用户差异化）
- **保存即生效，无需重启**：启用状态与描述每请求实时读库；总体说明即时应用
- **重启不丢**：配置持久化在 platform.db（`mcp_tools` 表 + `meta.mcp_instructions`），服务重启自动恢复
- 配置 API（admin 的 KEY 调用）：`GET /api/v1/mcp-tools` 查看全量；`PATCH /api/v1/mcp-tools` 保存（body：`{"tools": [{"name", "visibility", "supplemental_description"}], "instructions": "..."}`）。旧字段 `enabled`/`description` 仍收（兼容映射：enabled=true→visible/hidden，false→disabled）
- **旧说明备份**：2026-09-08 升级前如配置过总体说明（旧语义=全文覆盖），升级时自动备份停用（含已下线 search_md 引导，继续生效会误导）；管理页可查看备份内容，把仍适用部分重新加入补充说明
