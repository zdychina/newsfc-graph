# NewSFCGraph

> 从华为 5G 产品文档（UDG / UNC）自动构建**三层知识图谱**的完整体系：
> **原料 → 规范构建 → 资产产出 → 平台管理与消费**。

本仓库由 4 个独立又衔接的目录组成，构成一条"文档进、图谱出、可浏览可消费"的闭环。
本文件是**顶层总览与导航**；每个子目录都有自己的详细 README。

---

## 整体闭环

```
① output/                ② 三层图谱构建规范/           ③ 三层图谱资产/           ④ graph-asset-platform/
 产品文档原料      ──▶    构建规范 SOP (v0.14.1)  ──▶   产出的图谱资产     ──▶   管理与消费平台
 UDG / UNC HDX 归档      四层独立能力包                3 万+ md，8 类对象        FastAPI + Vue3
       │                        │                           │                         │
  阶段0 导出器             Agent 按规范构建               打包 zip 上传            解析→归类→索引→读 API
       ▼                        ▼                           ▼                         ▼
  产品文档 md 树         命令层 / 特性层 /                                       四菜单前端
                         Task 层 / 业务层                                       （浏览/统计/上传/测试）
                                                                                ＋ Skill 消费
                                                                                （配置生成 / 通用查询）
```

| 角色 | 你要看哪里 |
|---|---|
| 想理解"怎么构建图谱" | `② 三层图谱构建规范/` |
| 想看"图谱长什么样 / 规模" | `③ 三层图谱资产/` |
| 想浏览、上传、查询图谱 | `④ graph-asset-platform/` |
| 想用图谱生成配置 / 排障 | `④` 平台 ＋ 根目录 `config-generation/` ＋ `②` 里的 `graph-query-skill/` |

---

## ① `output/` — 产品文档原料

华为 5G 核心网产品文档原始归档，是整条流水线的输入。

- `UDG_Product_Documentation_CH_20.15.2/`（用户面网元，已解压；另有同名 `.zip` 856MB）
- `UNC 20.15.2 产品文档(裸机容器) 05/`（控制面网元）
- 章节结构：`5G基础知识 / OM参考 / 快速入门 / 特性部署 / 网络部署 / 网络运维`
- 已带 `html_to_md_mapping.json`（原始 HTML → md 的映射，由阶段0 导出器产出）

**命令层 / 特性层的源文档位置**（`output/` 下，已确认存在）：

| 产出层 | 网元 | 源文档路径 |
|---|---|---|
| 命令层 | UDG | `UDG_Product_Documentation_CH_20.15.2/OM参考/命令/UDG MML命令` |
| 命令层 | UNC | `UNC 20.15.2 产品文档(裸机容器) 05/OM参考/命令/UNC MML命令` |
| 特性层 | UDG | `UDG_Product_Documentation_CH_20.15.2/特性部署/特性指南/UDG特性指南` |
| 特性层 | UNC | `UNC 20.15.2 产品文档(裸机容器) 05/网络部署/特性部署/UNC特性指南` |

> 这一层不写代码，只放原料。把 HDX/HWICS 归档转成 md 树的脚本在 `②` 的 `scripts/`。

---

## ② `三层图谱构建规范/` — 构建规范（SOP，权威依据）

**版本 v0.14.1**（见 `VERSION`，变更见 `CHANGELOG.md`）。一个自包含、场景无关的"能力包"——Agent 构建图谱资产的唯一权威依据。

**四层（每层是一个独立、可单独拿走测试的能力包）：**

| 层 | 构建对象 | 体裁 | 状态 |
|---|---|---|---|
| **命令层** `command/` | 命令 `MMLCommand` ＋ 配置对象 `ConfigObject` | Spec（代码构建） | ✅ 完成 |
| **特性层** `feature/` | 特性 `Feature`（文档簇）＋ `License` | Spec（代码构建） | ✅ 完成 |
| **Task 层** `task/` | `AtomTask` / `CompoundTask` / `FeatureTask` | Procedure（Agent 手工） | 🟡 迁移中 |
| **业务层** `business/` | `BusinessDomain` / `NetworkScenario` / `ConfigurationSolution` | Procedure（Agent 手工） | ✅ 层包就位，资产部分迁移 |

每层同构：`agent.md`（构建师人设）→ `SKILL.md`（构建方法）→ `字段定义.md` → `template/` → `check.md`（核查）→ `change-requests/`，自带"构建 → 核查 → 反馈"闭环。

**顶层全局**：`README / VERSION / CHANGELOG / 演进机制 / 层包标准`（治理）、`conventions/`（通用约定）、`scripts/`（阶段0 导出器）、`graph-query-skill/`（通用查询 Skill）。配置生成 Skill 已移到仓库根目录 `config-generation/`。

📖 入口：[`三层图谱构建规范/README.md`](三层图谱构建规范/README.md) · [`层包标准`](三层图谱构建规范/层包标准.md) · [`演进机制`](三层图谱构建规范/演进机制.md) · [`conventions/命名规范`](三层图谱构建规范/conventions/命名规范-建议.md)

---

## ③ `三层图谱资产/` — 产出的图谱资产

按规范实际构建出来的 markdown 资产库（**就是图谱本身**）。

**组织方式：**
- **版本化对象**（命令/特性/Task 层）：`{ObjectType}/{nf}/{version}/{id}.md`，如 `Command/UDG/20.15.2/UDG@MMLCommand@ADD APN.md`
- **业务层**（跨网元，不带 nf/version）：`Business/{domain}/{scenario}/{solution}.md` 语义嵌套
- 每个版本目录带 `_build_manifest.json`（记录 sop_version / 构建范围 / 时间）
- `_intermediates/` 是构建中间产物（如 atom-input 工作底稿），非资产

**资产规模（实测，2026-07-29）：**

| 对象 | UDG | UNC | 合计 | 备注 |
|---|---|---|---|---|
| Command | 4577 | 8498 | 13075 | 最大类 ✅ |
| ConfigObject | 1175 | 2325 | 3500 | ✅ |
| Feature | 865 md / 258 特性 | 2394 md / 470 特性 | 3259 md / 728 特性 | 文档簇 ✅ |
| License | 187 | 448 | 635 | 多为叶子节点 ✅ |
| AtomTask | 237 | 280 | 517 | ✅ 全建 |
| CompoundTask | 34 | 38 | 72 | 🟡 迁移中 |
| FeatureTask | 46 | 37 | 83 | 🟡 迁移中 |
| Business | — | — | 37 | 完备度低（apn + business-awareness 两域 / 4 场景） |

> 命令层（Command + ConfigObject）与特性层（Feature + License）已全量构建完成。

---

## ④ `graph-asset-platform/` — 管理与消费平台

独立的图谱资产管理平台：**上传 md 资产包 → 自动解压、解析、归类合并进唯一统一资产库 → 读 API ＋ 四菜单前端**。

- **后端** FastAPI（Python）：纯 md 文件存储 ＋ 启动时全量构建内存索引（无数据库）。核心模块 `index.py`（构图）/ `bundle.py`（导入）/ `classify.py`＋`logical_id.py`（ID 与归类）/ `version.py`（版本解析）。
- **前端** Vue3＋TS：四菜单——**图谱浏览**（三栏：层导航 / 对象 md / 邻居图）、**统计**、**上传**、**测试**；另含登录与用户管理。
- **测试菜单**是独立隔离子系统（数据飞轮：TestCase / Run / Review，只读写 `platform-data/tests/`，不碰图谱资产）。
- **v2 用户体系**：`platform-data/users.json` 存用户与 KEY（前端 / upload / test / skill / admin 权限）。

📖 入口：[`graph-asset-platform/README.md`](graph-asset-platform/README.md)（含完整启动命令、API 速查、Docker 部署）

---

## 核心机制速记（跨目录通用）

### 对象 ID（版本无关）

- **NF 隔离类（3 段）** `{nf}@{Type}@{local}` → 命令/配置/特性/license/task
  例：`UDG@MMLCommand@ADD APN`、`UDG@Feature@GWFD-010101`
- **跨 NF 类（2 段）** `{Type}@{slug}` → 业务域/场景/方案
  例：`BusinessDomain@business-awareness`
- **version 不进 ID**：只在 YAML `version:` 字段 ＋ 目录路径里。文件名 = 完整逻辑 ID（保留空格，如 `UDG@MMLCommand@ADD URR.md`）。

### 资产 md 骨架（所有对象一致）

```
---
<YAML frontmatter：id / type / name / nf / version / ...>
---
# 标题
<正文 ## 章节>
## 边
- 关系类型: [[目标逻辑ID]]      ← 裸 wikilink，无路径、无 .md、无别名
```

### 图谱连接模式（严格分层 ＋ 双向回填）

```
BusinessDomain ─下游场景─▶ NetworkScenario ─下游方案─▶ ConfigurationSolution
                                                              │ 跨网元编排（UDG ＋ UNC）
                                                              ▼
FeatureTask ─编排─▶ CompoundTask ─步骤─▶ AtomTask ─对应命令─▶ Command ◀──双向──▶ ConfigObject
   │ ref                │ ref                 │ ref                              ▲ 控制
Feature(文档簇)                                                        License(叶子)
```

- 每层有 `ref` 字段回指上一层；被引用对象在 `## 边` 反向回填 `被引用于`。
- 业务层方案直接编排 UDG＋UNC 两侧 FeatureTask，是跨网元的顶层节点。

---

## 快速上手

### 我想浏览 / 管理已构建的图谱 → 起平台

```bash
# 首次：装依赖 ＋ 构建前端
cd graph-asset-platform/backend && pip install -e ".[dev]"
cd ../frontend && npm install && npm run build

# 日常：只起后端（前端 dist 由后端托管）
cd graph-asset-platform/backend && python -m uvicorn app.main:app --port 8000
```

→ 浏览器开 http://localhost:8000 （首次需初始化 admin KEY，见 [`平台 README`](graph-asset-platform/README.md)）。
资产库为空时，先在前端"上传"拖入一份资产 zip。

### 我想消费图谱（生成配置 / 排障 / 查询）→ 用 Skill

- 配置生成：[`config-generation/SKILL.md`](config-generation/SKILL.md)（`config-generation`）
- 通用查询：[`三层图谱构建规范/graph-query-skill/SKILL.md`](三层图谱构建规范/graph-query-skill/SKILL.md)（`graph-query`）
- Skill/Agent 沿对象 md 的 `[[ID]]` 引用，经平台 MCP 服务（`/mcp`，工具 `get_md` 等 5 个）逐层取，不全量加载——详见 [`graph-asset-platform/图谱平台接口文档.md`](graph-asset-platform/图谱平台接口文档.md)。

### 我想构建 / 补充图谱资产 → 跑 SOP

1. 读 [`构建规范 README`](三层图谱构建规范/README.md) 的"阅读顺序"。
2. 构建某层时，读该层 `{layer}/` 下全部（agent → SKILL → 字段 → check）。
3. 实际跑构建时，读 [`scripts/README.md`](三层图谱构建规范/scripts/README.md)（阶段0 导出器）。
4. 改了规范 → 升版本 ＋ 写 CHANGELOG（见 [`演进机制`](三层图谱构建规范/演进机制.md)）。

---

## 当前状态与已知缺口

- ✅ 命令层 / 特性层 全量验证完成；AtomTask 全建。
- 🟡 Task 层 `CompoundTask` / `FeatureTask` 迁移中——**`_index.md` 复用库被 SKILL 反复引用但实际不存在**（Jaccard 复用判定"有规则无载体"），是当前主要卡点。
- 🟡 业务层完备度低，仅 `apn-domain` + `business-awareness` 两域。
- ⚠️ 若干**状态同步问题**：各层状态表里的"资产待迁入 / 迁移中"描述偏旧（实际已部分迁入，见上表实测数）；CHANGELOG 存在两个 `0.12.0`（版本号笔误）。
- ⚠️ 平台 `/objects/{id}/neighbors` 与 MCP `get_object` **只索引 `## 边` 段**，正文内联 `[[ID]]` 不进边索引（已知硬约束），完备查询需 `get_md` 取全文扫描。

---

## 文档索引

| 想了解 | 看这里 |
|---|---|
| 构建规范全局 ＋ 层包模板 | [`三层图谱构建规范/README.md`](三层图谱构建规范/README.md) |
| 加新层 / 每层文件夹结构 | [`层包标准.md`](三层图谱构建规范/层包标准.md) |
| 规范怎么演进 | [`演进机制.md`](三层图谱构建规范/演进机制.md) · [`CHANGELOG.md`](三层图谱构建规范/CHANGELOG.md) |
| 命名 / ID / 图片引用约定 | [`conventions/`](三层图谱构建规范/conventions/) |
| 阶段0 产品文档导出 | [`scripts/README.md`](三层图谱构建规范/scripts/README.md) |
| 配置生成 Skill | [`config-generation/SKILL.md`](config-generation/SKILL.md) |
| 通用查询 Skill | [`graph-query-skill/SKILL.md`](三层图谱构建规范/graph-query-skill/SKILL.md) |
| 资产管理平台 | [`graph-asset-platform/README.md`](graph-asset-platform/README.md) |
