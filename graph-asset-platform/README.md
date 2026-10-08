# 图谱资产管理平台 (Graph Asset Platform) v2

一个**独立**的图谱资产管理平台：上传 md 资产包 → 自动解压、分析、归类合并进**唯一统一资产库** → 用通用机制解析对象/边 → 提供**读 API（单对象 / 单跳）**和**四菜单前端**（图谱浏览 / 统计 / 上传 / 测试）浏览游走 → 可导出。

> 与仓库里的 `assets/` **完全无关**——平台自管一份资产库，导入即权威。

## 前端界面（v2，四菜单）

- **图谱浏览**（默认页，三栏）：左=层 Tab 导航（命令层/特性层/任务层/业务层 + 网元/版本/类型选择器 + 虚拟列表，扛 4577 命令不卡）；中=对象 md（frontmatter 面板 + 正文 + 底部 `## 边` 可点跳转）；右=当前对象单跳邻居图谱。中栏 wiki 链接 / 右栏邻居点击 → 左栏自动切层+选择器+高亮（前端缓存，跨栏跳转不丢上下文）。
- **统计**：卡片式按层统计（命令层/特性层/任务层/业务层，命令层下按网元+版本），前期纯数字。
- **上传**：两种模式——①指定层上传（拖 md/zip 到选定层目录）；②**产品文档导入**（上传 `.hwics/.hdx` + 网元/版本，后台异步：解压导出 → 自动构建 命令/配置对象/License/特性 四类资产 → 重建索引，分步进度轮询；同 nf+version 默认拒绝、勾选「覆盖重建」可全量替换）。构建脚本拷贝自《三层图谱构建规范》（`backend/app/pipeline/`，规范权威、手动同步，详见其 README）。
- **资产目录**（/fs）：「图谱资产 / 原始产品文档」双根切换——后者浏览产品文档导出的原始 md（`platform-data/output/`，只读、不进图谱索引）；md 预览内图片（`assets/x.png`）经 `/fs/raw`、`/docs/raw` 端点渲染。
- **测试**（数据飞轮·**独立隔离子系统**）：用例（输入）→ 运行（产出）→ 审查（问题清单，带归因 + 涉及对象）。只读写 `platform-data/tests/`，不碰图谱资产；详见下方「启动 · 测试菜单数据」。

> v1 设计 spec：`docs/superpowers/specs/2026-07-16-graph-asset-platform-design.md`；v2 前端重构 spec：`docs/superpowers/specs/2026-07-17-graph-asset-platform-v2-frontend-design.md`。

## 资产格式（权威：`三层图谱构建规范/command`）

完整设计见 `docs/superpowers/specs/2026-07-16-graph-asset-platform-design.md`。要点：

- 每个 md = YAML frontmatter + 正文 + 底部 `## 边` 章节（边用 wikilink `[[{nf}@{Type}@{local}]]`）。
- 对象 ID **版本无关**：NF 隔离类 3 段 `{nf}@{Type}@{local}`，跨 NF 类 2 段 `{Type}@{semantic}`。
- **版本只在目录和 frontmatter 字段**：NF 类目录 `{Layer}/{nf}/{version}/`，文件名 = 逻辑ID。
- 版本解析：每个 (id, version) 一个节点；不带版本时按**该 id 最新现存版本**解析，`?version=` 显式指定。
- bundle = 一个 zip，里面一堆 md（**无 manifest**）；导入按 frontmatter 自动归类合并。

## 目录结构

```
graph-asset-platform/
├── backend/        FastAPI 后端（Python）
│   ├── app/        models/logical_id/registry/classify/md_parser/edges/version
│   │               store/index/bundle/service/main + routers/
│   └── tests/      单元 + 集成 + e2e；fixtures/sample_bundle/ 样例资产
└── frontend/       Vue3+TS 三栏只读前端
```

## 启动

### 原理：为什么日常只起后端

前端 build 后是纯静态文件（`frontend/dist/`），**由后端 `main.py` 自动托管**——挂在根路径 `/`，并用 SPA 兜底把所有非 `/api` 路径回 `index.html` 交给 Vue Router。所以**日常只起后端一个进程**，前端页面 + 接口都在 `http://localhost:8000`。只有**改前端代码**时才需要单独起前端 dev server 或重新 build。

### 日常使用（只起后端）

> 前提：`frontend/dist/` 已存在。首次或改完前端代码后，需先跑一次「构建前端」（见下）。

仓库根目录执行（单行）：

```bash
cd graph-asset-platform/backend && python -m uvicorn app.main:app --port 8000
```

> **鉴权（v2 用户体系）**：`platform-data/users.json` 存用户（明文 KEY，不入 git）。前端访问跳登录页（用户名+KEY，仅 `can_frontend` 用户可登录）；Agent 可走 MCP 服务（`/mcp`）或 REST 兼容接口（`POST /api/v1/domains`、`POST /api/v1/md`、`POST /api/v1/search`、`POST /api/v1/files`，与 MCP 四公开工具同契约），均在 header 传 `X-API-Key: <用户KEY>`、每次调用传同名字段 `AGENT_USERNAME`/`AGENT_SESSION_ID`（MCP 工具参数或 REST JSON body），详见 `图谱平台接口文档.md`。**不再使用 `GAP_API_KEY` 环境变量**。
>
> **初始化 admin**：`users.json` 不存在或为空时无法登录。生成初始 admin（打印 KEY）：
> ```bash
> cd graph-asset-platform/backend && python -c "import secrets,json; from pathlib import Path; k='gap_'+secrets.token_hex(16); Path('../platform-data/users.json').write_text(json.dumps({'users':[{'username':'admin','key':k,'can_frontend':True,'can_skill':True,'is_admin':True,'created_at':'2026-07-24T00:00:00+00:00'}]},ensure_ascii=False,indent=2),encoding='utf-8'); print('ADMIN_KEY='+k)"
> ```
> 登录后到「用户」菜单（仅 admin 可见）创建其他用户（自动生成 KEY + 勾权限）。

→ 浏览器开：
- 平台首页（四菜单：图谱浏览 / 统计 / 上传 / 测试）：http://localhost:8000/
- 测试菜单直达：http://localhost:8000/tests
- Swagger：http://localhost:8000/docs
- API 根：http://localhost:8000/api/v1

**Agent 接入（MCP）**：端点 `http://<host>:8000/mcp`（`X-API-Key` 鉴权，4 个公开工具：
get_domains / search_graph / search_files / get_md——统一搜索、文件名/目录浏览与权威原文读取；
旧 3 工具 hidden 兼容 deprecated）。
完整配置见 [docs/MCP配置指南.md](docs/MCP配置指南.md)，工具明细见 [图谱平台接口文档.md](图谱平台接口文档.md)。

### 一次性环境准备

后端依赖（首次）：

```bash
cd graph-asset-platform/backend && pip install -e ".[dev]"
```

前端依赖 + 首次构建（首次）：

```bash
cd graph-asset-platform/frontend && npm install && npm run build
```

### 改前端代码时

开发模式（热更新；**后端要另起**，命令见上）：

```bash
cd graph-asset-platform/frontend && npm run dev
```

→ http://localhost:5173 （`vite.config.ts` 把 `/api` 代理到后端 8000）

改完收尾——重新 build，让后端托管新版本（之后回到「只起后端」）：

```bash
cd graph-asset-platform/frontend && npm run build
```

| 场景 | 起什么 | 访问 |
|---|---|---|
| 用平台 / 演示 | 只起后端（dist 已 build） | `:8000` |
| 改前端（要热更新） | 后端 + `npm run dev` | `:5173` |
| 前端改完 | `npm run build` 一次 | 回到 `:8000` |

### 测试菜单数据（数据飞轮）

第 4 菜单「测试」是**独立隔离子系统**（只读写 `platform-data/tests/`，不碰图谱资产）。三类 md：

| 类型 | 落点 | 谁写 |
|---|---|---|
| TestCase（输入） | `platform-data/tests/cases/{域}/{场景}/TestCase@{slug}.md` | SA 手写 |
| Run（产出） | `platform-data/tests/runs/{用例ID}/{RunID}/{RunID}.md` + 同目录 `config.txt` / `方案.md` / `LLD.md` | Agent 写 |
| Review（审查） | `platform-data/tests/reviews/{RunID}/Review@{slug}.md` | 人工 UI 写（未来 Agent） |

> 解析全降级——只靠「路径 + 文件名」识别/分组，YAML 字段缺失不报错。详见 spec：`docs/superpowers/specs/2026-07-22-test-case-mgmt-platform-design.md`。

Agent 写完 Run 后让平台看见（一条 curl）：

```bash
curl -X POST http://localhost:8000/api/v1/tests/reindex
```

人工审查直接在 UI 点「+ 添加审查」填问题清单（含归因 + 涉及对象）即可，平台自动落 md + 刷新索引。

### 导入图谱样例数据（首次浏览图谱用）

后端起来、资产库为空时，导入一份样例 bundle 才能浏览图谱：

```bash
cd graph-asset-platform/backend && python -c "import zipfile,glob,os; z=zipfile.ZipFile('sample.zip','w',zipfile.ZIP_DEFLATED); [z.write(f, os.path.relpath(f,'tests/fixtures/sample_bundle')) for f in glob.glob('tests/fixtures/sample_bundle/**/*', recursive=True) if os.path.isfile(f)]; z.close()"
curl -F "file=@sample.zip" http://localhost:8000/api/v1/import
```

或直接在前端顶栏点「上传」拖入 `sample.zip`。

## API 速查（前缀 `/api/v1`）

| 端点 | 说明 |
|---|---|
| `POST /import` | **异步**上传 zip → 202 `{job_id, status}`；后台解压+建索引 |
| `GET /import/jobs` | 导入任务列表（活状态：processing/done/failed + added/updated） |
| `GET /import/jobs/{id}` | 单任务状态 |
| `GET /imports` | 上传历史（完成记录） |
| `GET /export?nf=&version=&domain=&scenario=` | 导出资产库（可切片）→ zip |
| `GET /stats` | 计数 + `per_layer`(4 UI层) / `per_layer_per_nf` / `per_layer_per_nf_per_version` / `per_domain` |
| `GET /objects?layer=&type=&q=&nf=&version=&domain=&page=&size=` | 列对象（**layer**=UI层 命令层/特性层/任务层/业务层；`version` 过滤；按 id 聚合 + versions[]） |
| `GET /objects/{id}?version=` | 单对象（不带版本=最新现存；带 `?version=` 指定） |
| `GET /objects/{id}/neighbors?hops=1&version=` | 单跳邻居（含反链） |
| `GET /objects/{id}/md?version=` | 原始 md |
| `GET /subgraph?center=&hops=&type=&version=` | N 跳子图 |
| `GET /telemetry/stats?days=30` | SKILL 取用频次聚合（total/by_type/top_ids/timeline，统计页展示） |
| `POST /domains`、`/md`、`/search`、`/files` | Agent REST 兼容接口（与 MCP 四公开工具同契约；`X-API-Key` + 归因字段，见 `图谱平台接口文档.md`） |

> `{id}` 含 `@` 和空格，URL 须 encode（`@`→`%40`、空格→`%20`）。`?version=X` 不在该 id 可用版本时 → 404 + `available_versions`。

## 测试

```bash
cd graph-asset-platform/backend
python -m pytest -q          # 全量（单元+集成+e2e，99 测试）
```

## ⚠️ 提交注意（GIT 陷阱）

仓库里 `三层图谱构建规范/scripts/product_doc_md_exporter_optimized.py` **长期处于 git 暂存态（A）**。对本仓库做任何 `git commit`，若**不路径限定**，都会把它夹带进提交。

**务必**这样提交平台代码：
```bash
git add graph-asset-platform/<具体文件>
git commit -m "<type>: <描述>" -- graph-asset-platform/
```
**绝不要** `git add -A` / `git add .` / `git commit -am`。

## 二期预留（v1 不做）

在线编辑 / 写 API、资产内容级版本管理、变更评审回路 + Agent 编排、多用户鉴权。接入点见 spec §10。
