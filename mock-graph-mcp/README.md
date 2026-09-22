# mock-graph-mcp：模拟 Graph MCP 服务

在访问不到内网图谱平台的环境里测试 `graph-build` skill。工具契约（`get_domains` / `search_graph` / `get_md` 的参数、返回和错误码）对齐 `graph-asset-platform/图谱平台接口文档.md`。**所有数据都是模拟的**：命令参数由测试语料自动推断得出，特性码 `WSFD-9001xx` 是虚构的，只用于验证流程，不能用来判断映射是否准确。

只依赖 Python 3 标准库。

## 快速开始

```bash
cd mock-graph-mcp
# 1. 从语料抽取结果生成模拟数据（先用 graph-build 的 extract_corpus.py 产出 corpus.json）
python ../graph-build/scripts/extract_corpus.py "../中国移动AMF异网漫游配置规范（20.13.2）.md" -o /tmp/t/_work
python gen_data.py --corpus /tmp/t/_work/corpus.json
# 2. 启动服务（默认 http://127.0.0.1:8765/mcp；--key 开启 X-API-Key 校验）
python server.py
# 3. 冒烟测试
python client.py domains
python client.py md "UNC@MMLCommand@SET ROAMCOMMPLCY" "UNC@AtomTask@ADD PNFPROFILE"
python client.py search LKV2SAIRA01 --type License
```

### 接入 Claude Code

在项目根目录的 `.mcp.json` 中加入以下配置（已被 .gitignore 忽略）：

```json
{"mcpServers": {"graph": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}}
```

- 本机配置了 `HTTP_PROXY` 时，要设置 `NO_PROXY=127.0.0.1,localhost`，否则请求会被转发到代理，返回 502。
- 归因参数从环境变量 `_AGENT_USERNAME` / `_AGENT_SESSION_ID` 读取，随便填一个测试值即可。
- 重启 Claude Code 后，工具显示为 `mcp__graph__get_md` 等。

### 模拟上传

把 skill 的产出目录作为第二个数据目录挂上，服务会自动重载，之后 `get_md` 就能查到新对象（同 ID 以后挂载的目录为准，相当于覆盖）：

```bash
python server.py --data data --data <batch>/assets
```

## 调用日志

每次工具调用都会追加一行到 `logs/calls.jsonl`（工具、参数、结果摘要、耗时），用来审查 Agent 是否做了 `图谱接口.md` §5 要求的复用查询。

## 数据构成（gen_data.py）

| 来源 | 内容 |
|---|---|
| 自动生成 | 语料中出现的全部命令的 MMLCommand（20.15.2），`## 参数说明（CommandParameter）` 由语料中实际出现的参数推断：类型、枚举候选、引用关系（“该参数引用 ADD X 命令配置的对象”） |
| 自动生成 | 14 个存量 AtomTask（`EXISTING_ATOMS`） |
| seed/Feature、seed/License | 7 个特性（其中 WSFD-900101 带激活、原理两个子文档）、5 个 License |
| seed/Task | 存量 CT：`peer-nf-profile`、`plmn-access-basic`；存量 FT：`WSFD-900102`、`WSFD-900107`；字典不完整的 atom `SET NGMMFUNC` |
| seed/Business | BD：`business-awareness`（含 NS `charging`）、`mobility-management`（含 NS `registration-access`） |

## 测试点（skill 应该表现出的行为）

| # | 埋点 | 期望 skill 的行为 |
|---|---|---|
| T1 | `ADD SBIFQDNPORTPLCY` 在平台上不存在 | 登记为“版本差异：命令不存在”，不建 atom、不编排，P3 请用户决定 |
| T2 | `SET AMFROAMFUNC` 没有参数 `VGMLCSW` | 登记为参数不存在；Task 中不写该参数 |
| T3 | `ADD SCTPLE.CROSSIPFLG` 枚举为 YES/NO（文档写 `E_SCTP_CROSS_YES`）；`SET CACHEPLCY.CACHEPOLICY` 枚举为 BEST_EFFORT/ALWAYS（文档写 BESTEFFORT） | 写法差异或冲突待确认，不擅自改值 |
| T4 | `SET DNNCMPT` 新增必选参数 `SMFHRUSRFMT` | 在 atom / CT 中按 `<规划值>` 列出，并注明“文档未给出” |
| T5 | `SET ROAMCOMMPLCY` 有 20.13.2 和 20.15.2 两个版本 | 取最新版 20.15.2，构建报告中记录所用版本 |
| T6 | LKV2SAIRA01 → License → WSFD-900101（与特性的「所需License」互相印证） | 强证据映射，建 FT；读完激活子文档 |
| T7 | 切片命令只能通过命令指纹映射到 WSFD-900106（没有 License） | 中等证据，P3 请用户确认 |
| T8 | `ADD PNFPROFILE` 同时出现在 LCS 特性（WSFD-900107）和漫游激活文档中 | 不因为单条命令命中就映射到 LCS（弱证据） |
| T9 | SEPP 对接（`ADD PNFPROFILE`/`ADD SEPPBINDGRP`/`ADD PLMNBINDSEPPGRP`）与存量 CT `peer-nf-profile` 部分重合（J=1/6≈0.17） | 算 Jaccard 后新建 CT（相位不同），而不是强行复用 |
| T10 | 电信 / 联通配置包含 `ADD NGHPLMN`/`ADD NGSRVPLMN`/`ADD GUAMI`，与存量 CT `plmn-access-basic` 重合 | 复用或扩展该 CT，并回填「场景差异」（输出更新版并登记修改清单） |
| T11 | 存量 atom `SET NGMMFUNC` 缺 `ROAMINGSW` 维度 | 增补配置维度，原内容不删，登记修改清单 |
| T12 | 存量 FT `WSFD-900102` 已覆盖漫游用户的 H-SMF 选择 | 不改 FT，只在 CS 中写运营商变种 |
| T13 | 存量 CT `peer-nf-profile` 带有旧的「被引用于」边 | 更新时原样保留，不再新增 |
| T14 | 没有“漫游”业务域；`mobility-management` 的 NS 边界写明“不覆盖漫游” | 在 P3 中提出新建 NS（或 BD）并请用户确认 |
| T15 | 5.8.6.3.5 紧急呼叫只在正文中提到 `SET NGMMFUNC: EMG=…`，并写明“当前不用配置” | 不编排；在 CS 约束或未入图清单中说明 |
