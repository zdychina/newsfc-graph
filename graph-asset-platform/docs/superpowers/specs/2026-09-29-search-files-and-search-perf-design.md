# 设计：search_files 文件搜索工具 + search_graph 超时治理

- 日期：2026-09-29
- 状态：已与平台 Owner 对齐需求（本文档待评审）
- 范围：graph-asset-platform backend（MCP + REST 兼容通道 + SQLite v14 迁移）

## 1. 背景与目标

Agent 暴露面当前收敛为 3 个 MCP 公开工具（get_domains / search_graph / get_md）+ 3 个
legacy hidden 工具，全部是**对象维度**（逻辑 ID）。需求：

1. **新增文件维度开放通道**：Agent 能按**文件名**搜索 `platform-data/assets/` 下的
   所有文件（含非 md，如图片），并能分页/全量获取；md 文件命中后回带对象 ID 走现有
   get_md 读内容。
2. **治理 search_graph 超时**：内网数据量下 search_graph 频繁超时，属硬伤，与 1 同批修复。

规模预期（Owner 口径）：单目录全量 ≤ 1 万文件；搜索结果 ≤ 几百条；索引需扛百万级文件。

## 2. 非目标与冻结约束

- **get_md / get_domains 冻结**：参数与内部机制不允许变；描述词（docstring）可以改。
- search_graph **参数形状不变**，内部查询结构与响应的 total/计数语义可变（详见 §5）。
- 不覆盖 `output/`（产品文档）、`tests/`、`.trash/`——仅 `assets/` 根。
- 不新增"按路径读文件内容"的工具：md 内容读取由 get_md 承担（带 obj_id+version），
  非 md 文件（图片等）只给元数据，不做二进制内容下发。
- 不做在线编辑/写通道（MCP 侧只读）。

## 3. 决策记录（Owner 已拍板）

| # | 决策 |
|---|---|
| D1 | 范围=仅 `assets/` 根，含非 md 文件 |
| D2 | 不合并进 search_graph（契约污染 + 超时工具不宜再加载），新增独立 `search_files` |
| D3 | "读文件"不新增工具，复用 get_md（search_files 回带 obj_id+version） |
| D4 | 超时治理与 search_files 同项目两轨推进 |
| D5 | 全量获取用**游标分页**（has_more + next_cursor 循环），单页上限 500 |
| D6 | 两字关键词正文搜索走 **正文语料 LIKE**（POOL_CAP 早停兜常见词；⚠️两次实证修订：①前缀短语 `MATCH '"xx"*'` 在 3.45.3 返回空——FTS5 trigram MATCH 需 ≥3 字符；②「2 字符 LIKE 走 trigram 索引」不成立——ESCAPE 子句使优化失效且 <3 字符本就无法用索引，1M 行实测 250ms 语料级扫描。罕见两字词在大正文库下仍慢 → `metadata_only` 是大规模下的**主要缓解手段**而非仅保命）。meta 表开关 `body_like`/`metadata_only`，默认 body_like |
| D7 | REST 通道新增第四个 `POST /api/v1/files`，与 MCP search_files 同契约（沿用 MCP/REST 同构对账测试模式） |

## 4. Track A：search_files 新工具

### 4.1 数据模型（db schema v14）

```sql
-- 文件户口册：assets 下所有文件 + 目录行（目录行支撑 path 模式的 ls 式浏览）
CREATE TABLE IF NOT EXISTS files(
  path TEXT PRIMARY KEY,          -- 相对 assets 根，正斜杠归一化，磁盘真实大小写
  name TEXT NOT NULL,             -- basename 原样
  ext  TEXT NOT NULL DEFAULT '',  -- 小写、无点（无扩展名=''；目录恒 ''）
  is_dir INTEGER NOT NULL DEFAULT 0,
  size INTEGER NOT NULL DEFAULT 0,   -- 目录恒 0
  mtime REAL NOT NULL DEFAULT 0      -- 目录不随子项变更刷新（仅展示用途，避免噪音）
) WITHOUT ROWID;

-- 文件名全文索引（trigram：与 md_fts/graph_search_fts 同选型，中英统一子串语义）
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
  path UNINDEXED, name, tokenize='trigram'
);
-- 伴生映射（同 md_fts_map/graph_search_map 教训：按 rowid O(1) 删，防批量 O(N²)）
CREATE TABLE IF NOT EXISTS files_fts_map(
  path TEXT PRIMARY KEY, fts_rowid INTEGER NOT NULL
) WITHOUT ROWID;
```

- `files_fts.name` 存 `normalize_search_text(name)`（NFKC→strip→casefold，复用
  graph_search_repo 现有函数）；`files.name` 存原样（响应展示用）。
- `files` 主键即 path，是游标分页与目录前缀过滤的排序/范围基础。

### 4.2 索引维护

新增 `repos/files_repo.py`：`upsert_path / remove_path / remove_prefix / rebuild_all /
integrity_ok`（对账：files 与磁盘行集合一致性；path 集合对账，成本可忽略）。

维护挂钩（全部在既有 `import_lock` 约定内）：

| 写入口 | 挂钩 |
|---|---|
| `/fs` 写端点（upload / put_file / move / rename / delete / trash_restore） | 端点内显式调 files_repo（move/rename=remove+upsert；delete=remove_path 或 remove_prefix） |
| `service.rebuild()` | 末尾追加 `files_repo.rebuild_all()`（覆盖 tests 路由等走 rebuild 的批量写） |
| **`pipeline/gate.py` 应用/回退**（抽取任务：直接 `_copy` 到 assets + `svc.reindex_paths()`，**不走 rebuild**；且写入非 md 的 `_` 前缀 sidecar 文件——`reindex_paths` 只认 md，挂钩 rebuild/reindex_paths 都覆盖不到） | gate apply 后按其已知行集合（rows_map / extract_files 清单）显式 files_repo 增量同步（remove_path 旧 + upsert 新）；revert 同理。**顺序要求**：files_repo 同步须在耐久完成点 `update_job(done)` **之前**——apply 中途崩溃时任务重试会重跑同步（沿用 gate.py 既有耐久完成点纪律） |
| `POST /admin/reindex` | 走 svc.rebuild()，天然覆盖 |
| 启动 | **一次性 bootstrap**：files 表为空且 assets 非空 → 后台线程全量扫描建册。落点在 `Service.__init__`（与 `_fts_reconcile_async` 同款后台模式；db.py 只建表不碰文件系统）。不阻塞启动；百万级为一次性分钟级，可接受。扫描期间 search_files 正常服务但结果不全，响应带 `index_building: true` 标记（模块级 bool，**须在启动线程之前置位**，防 Service 构造与线程启动之间的请求窗口看到空表+false） |
| 外部直拷磁盘（绕过平台） | 不自动对账（注意：`_sync_mtime_async` 启动时会自动把外部拷入的 md 治理进 objects，**但不会治理 files 表**——外部拷贝后 objects/files 漂移是常态而非边缘case）；admin 手动触发 `POST /admin/files-reindex`（新增，与 /admin/reindex 同款：admin 权限 + svc.rebuild 级别的兜底语义） |

**点文件策略**：跳过 `.` 开头的文件/目录（与 `store.list_children` 一致）；pipeline 的
`_` 前缀 sidecar **不是**点文件，正常入册。

不做的：启动时周期性 mtime 全量对账（10M 级全走一遍是分钟级，YAGNI；bootstrap + 写路径
增量 + admin 兜底已闭环）。

### 4.3 工具契约（MCP `search_files`，REST `POST /api/v1/files` 同构）

参数：

| 参数 | 语义 |
|---|---|
| `query` | 文件名子串（规范化后 ≥2 字符；1 字符 → INVALID_ARGUMENT）。≥3 走 trigram 短语 MATCH（索引）；2 字符走 name 语料 LIKE（trigram 对 <3 字符/ESCAPE 均不生效，实测语料级扫描，LIMIT 早停兜常见词——name 语料远小于正文，当前规模可接受） |
| `path` | 目录过滤。`recursive=false`（默认）= 列**直接子项**（文件+目录行，ls 语义——Agent 可逐层探目录结构）；`recursive=true` = 递归子树**仅文件**（`find <dir> -type f` 语义，全量场景）。实现用 GLOB 前缀范围扫描（转义 `[ * ?`），走 path 主键索引；path 不存在 → INVALID_FILTER（结构化错误，不静默 0 结果） |
| `recursive` | bool，默认 false；仅 path 模式有意义（query 模式恒全局搜索） |
| `ext` | 扩展名精确过滤（小写，如 `md` / `png`；给了 ext 自然只余文件行） |
| `limit` | 1~500，默认 100（文件行小，500 行 ≈ 百 KB 级） |
| `after` | 游标：上一页的 `next_cursor`（即上一页最后一条 path） |
| `AGENT_USERNAME` / `AGENT_SESSION_ID` | 打点归因（同现有工具） |

约束：`query / path / ext` **至少给一个**（全空 → INVALID_ARGUMENT）。多条件 = 交集。

响应：

```json
{
  "files": [
    {"path": "Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md",
     "name": "UDG@MMLCommand@ADD URR.md", "ext": "md", "is_dir": false,
     "size": 3200, "mtime": "2026-08-01T10:00:00Z",
     "obj_id": "UDG@MMLCommand@ADD URR", "version": "20.15.2"}
  ],
  "total": 123, "total_is_bounded": false,
  "has_more": true, "next_cursor": "Command/UDG/20.15.2/UDG@MMLCommand@ADD XYZ.md",
  "index_building": false,
  "applied_filters": {"path": "Command/UDG/20.15.2"}
}
```

- 排序：`path` 字典序 ASC（确定性排序 = 游标稳定性基础）。
- 目录行：query 模式与 path 直接子项模式都会返回目录行（`is_dir: true`，无 obj_id）；
  `recursive=true` 的子树全量只返回文件行（find -type f 语义）。目录行 FTS 同样入册
  （按目录名搜索可用）。
- `obj_id`/`version`：`LEFT JOIN objects ON objects.source_path = files.path`（source_path
  与文件一一对应，无 latest 歧义）。**必须回带 version**：文件可能是旧版本目录下的 md，
  get_md 不带 version 会取到最新版内容而非该文件内容。非图谱文件（图片/未入索引 md）无
  obj_id 字段。
- `total`：与 search_graph 对齐的同名语义——精确计数到 10000，超过为 10000 +
  `total_is_bounded: true`（计数 SQL 用 `SELECT COUNT(*) FROM (… LIMIT 10001)` 有界化）。
  不另造 `total_capped` 之名，兄弟工具间同语义同字段名。
- `index_building`：首启 bootstrap 扫描进行中为 true（此时结果不全，Agent 可等待后重试）。
- 全量获取循环：**拿 → 看 has_more → 带 next_cursor 再拿**，直到 has_more=false。游标是
  path 键序（keyset），深翻页 O(1)、不受并发增删导致的页错位影响。

实现落点：新增 `app/file_query.py`（search_files_core + 复用 graph_query.contracts 的
GraphError/错误码）；MCP 侧在 mcp_server.py 注册 `search_files`（照抄三工具的打点/错误
envelope 模式，tool 级打点、无逐文件 object 级打点——同 search_graph 先例）；REST 侧在
skill_compat.py 加 `POST /api/v1/files`（同契约同 envelope）。

### 4.4 权限 / 可见性 / 打点

- 鉴权：与现有工具一致——`X-API-Key` + **skill 权限**（只读，不要求 can_assets；与
  get_md 读 md 同口径）。
- mcp_tools 可见性：默认 visible（无行=visible 的现有默认），admin 可经用户菜单改三态。
- 打点：tool 级（`mcp:search_files` / REST `files`），params/result 摘要照现有截断策略。

## 5. Track B：search_graph 超时治理（内部重构）

### 5.1 根因（代码审查结论，graph_query/search.py）

1. **百万行回 Python**：每 term 元数据 LIKE 与正文 FTS 各 `LIMIT 200万+1` 全量 fetch 回
   Python 聚合排序——宽泛词（如"配置"）命中数十万即分钟级。
2. **短词全表扫描**：<3 字符的词（中文两字词极常见）走不了 trigram 索引，退化为对全部
   正文文本的 LIKE 扫描（读 GB 级正文页）。
3. **bm25 全量排序**：宽词命中大时先对所有命中算相关度才取前 N。

### 5.2 重构方案

1. **候选池有界化**：每 term 每来源只取 **top K=2000** 进 Python 合并池。
   ≥3 字符元数据与正文均走 trigram FTS；元数据按 exact → prefix → broad 三段
   去重入池，避免最相关的精确/前缀项被宽包含池挤掉。正文来源带 bm25 rank 供 RRF。
   深翻页翻到池底即止，响应标注截断（见 5.3）。
2. **总数封顶**：total = 合并池大小的精确值不再保证全局精确；任一来源触顶 K 时
   `total_is_bounded=true`。语义="至少 total 条，已按相关度截断"。
3. **SEARCH_TOO_BROAD 退场**：不再抛错（错误码保留在枚举中标注 deprecated-unused，不再
   触发）。宽泛搜索正常返回 top 结果 + 截断标记 + "建议加过滤"提示（diagnostics）。
4. **短词路径**（D6 两档，开关存 **meta 表 key=`search_short_term_mode`**（`body_like` /
   `metadata_only`，默认 `body_like`），每请求读取（同 mcp_tools 配置模式，改即生效、无 UI
   依赖，admin 经 SQL/后续运维页切换；读取失败回退上次成功值）：
   - 2 字符 term：正文走 LIKE + POOL_CAP（**实测为正文语料级扫描**——trigram 索引对
     <3 字符模式与 ESCAPE 子句均不生效，1M 行 250ms 实测；早期「3.45 给出索引计划」
     为 EQP 文本误读）。常见词被 POOL_CAP 早停兜住；**罕见两字词在大正文库下仍慢，
     `metadata_only` 档是大规模下的主要缓解手段**（内网验收两字词超时即切档）；
     元数据 LIKE 照常（metadata_text 行短，扫描可承受）。**前缀短语方案已实证否决**
     （`MATCH '"xx"*'` 在 3.45.3 返回空，FTS5 trigram MATCH 需 ≥3 字符）。
   - 1 字符 term：只搜元数据，不搜正文（diagnostics 注明 `body_skipped_short_terms`）。
   - perf 冒烟测试（合成 10 万对象语料，标记 slow）不达标 → 切 metadata_only 档发布。
5. **catalog 校验缓存**：`_validate_filters` 每请求最多 5 次 DISTINCT 全表扫，改为模块级
   缓存 + rebuild/reload_index 时失效。
6. 兜底保留：`fts_rebuilding` 拒绝窗口、组合校验（INVALID_FILTER_COMBINATION）、
   `_probe_without_filters` 诊断探针（EXISTS LIMIT 1）。诊断探针必须服从主查询的
   短词档位：一字词与 `metadata_only` 两字词不得从诊断路径重新执行正文 LIKE。

### 5.3 契约变化清单（MCP 与 REST 同步，共享核心）

| 字段 | 旧 | 新 |
|---|---|---|
| `total` | 全局精确 | 合并池精确值；触顶时配合 `total_is_bounded=true` |
| `total_is_bounded` | 无 | 新增 bool |
| `diagnostics.term_counts` | 每 term 精确命中数 | 保持整数形状，改为有界候选池内去重数（旧客户端可继续 `count > 0`） |
| `diagnostics.term_stats` | 无 | 新增 `{hit: bool, capped: bool}`（候选池命中 + 触顶标记） |
| SEARCH_TOO_BROAD 错误 | 命中超 200 万抛出 | 不再触发 |
| 其余（terms/match/filters/page/size/hits/facets 结构） | — | **不变**；facets 基于合并池计算（截断时为池内构成，语义在接口文档标注） |

## 6. 描述词与决策树更新（get_md/get_domains 仅改词，机制冻结）

1. **修"两个第一"打架**：get_domains 描述改为"**按业务意图**定位时第一步（先域后场景）"；
   search_graph 描述改为"**按关键词找对象**时的入口"——分工=意图维度 vs 关键词维度。
2. **版本默认语义写明**：get_md / search_graph 描述里明示"默认作用于每个 ID 的最新现存
   版本"（现在只在参数描述里）。
3. **DEFAULT_INSTRUCTIONS 决策树补一支**：
   "按文件名找文件 / 列某目录下文件 → search_files（md 命中回带 obj_id+version，用
   get_md 读该文件内容）；按内容找对象 → search_graph"。
4. search_graph 描述同步：宽泛搜索返回截断标记的语义、term_counts 新形状。

## 7. 测试策略

- **files_repo 单元**：upsert/remove/prefix/rebuild/integrity；win_long 路径。
- **search_files 核心**：query/path/ext 组合交集；path 直接子项模式（ls：含目录行）与
  recursive=true 子树模式（仅文件）各自的行为；游标循环遍历总数==total（合成 5k 文件
  全量走完）；obj_id+version 关联（含旧版本目录文件）；1 字符拒绝；path 越界/不存在
  INVALID_FILTER；GLOB 特殊字符转义（目录名含 `[`）；点文件跳过；bootstrap 期间
  `index_building` 标记。
- **同构对账**：MCP search_files vs REST POST /api/v1/files 同请求同结果（沿用三对模式
  扩为四对）。
- **search_graph 行为**：既有测试随契约更新（total_is_bounded/term_counts 新形状）；
  新增截断路径测试（合成高基数 term）；短词两档开关各测一遍。
- **perf 冒烟（slow 标记）**：合成 10 万对象，宽词 + 2 字词断言 < 2s（CI 可跑的回归门）；
  另在**内网验收 checklist** 里用真实数据量复测同口径（合成门防回归，真实门防外推失真）。
- 全量 pytest 保持绿。

## 8. 交付物清单

1. `db.py`：v14 迁移（仅三表建表，幂等）
2. `service.py`：`Service.__init__` 追加首启异步 bootstrap（files 表空且 assets 非空时
   后台全量建册 + `index_building` 标记）；`rebuild()` 末尾追加 `files_repo.rebuild_all()`
3. `repos/files_repo.py`（新）
4. `app/file_query.py`（新）：search_files_core
5. `mcp_server.py`：search_files 注册 + 三工具描述词修订 + DEFAULT_INSTRUCTIONS 决策树
   + get 打点摘要按 term_counts 新形状适配
6. `routers/skill_compat.py`：POST /api/v1/files + 打点摘要同款适配
7. **`middleware/auth.py`**：`_GRAPH_API_PATHS` 增加 `/api/v1/files`（否则 skill-only 用户
   403、错误响应丢 GraphError envelope——REST 同构要求）
8. `routers/fs.py`：写端点挂 files_repo
9. **`pipeline/gate.py`**：抽取任务 apply/revert 显式同步 files 表（含非 md sidecar；
   rebuild/reindex_paths 均覆盖不到该路径）
10. `routers/admin.py`：POST /admin/files-reindex
11. `graph_query/search.py`：有界化重构 + 短词两档（meta 表开关） + catalog 缓存
12. 测试（§7）+ `图谱平台接口文档.md` / `docs/MCP配置指南.md` 更新（接口文档须分别写明
    两处 `total_is_bounded` 的封顶口径：search_graph=候选池 2000/term/来源；search_files=
    计数 10000）
13. deploy 侧无镜像变更（纯代码包，走既有 sync.sh pack/apply + db 自动迁移）

## 9. 风险与回退

| 风险 | 缓解 |
|---|---|
| 两字词 LIKE 在低版本 SQLite 无索引计划（前缀短语方案已实证否决并按 D6 修订弃用） | meta 表开关切 metadata_only 档；perf 冒烟测试把门 |
| 深翻页到池底即止（语义变化） | 接口文档明示"top-N 语义 + 截断标记"；全量场景由 search_files 游标承担 |
| files 索引与磁盘漂移（外部直拷） | `_sync_mtime_async` 只治理 objects 不治理 files（§4.2），外部拷贝后漂移是常态；admin files-reindex 兜底 + 接口文档写明运维口径 |
| 首启 bootstrap 在超大库上耗时 | 异步后台执行，不阻塞服务；进度打日志 |
| 内网升级（v13→v14） | IF NOT EXISTS + 空表判定的幂等迁移，与既有迁移同模式 |
