# AI MML 核查流程

> Stage 6 Step A 的**后备路径**：运行环境中**没有**独立 SKILL `aimml-check` 时，按本流程直接调用 coremaster configcheck 接口做语法 + 语义核查，解析结果并修正脚本。有 `aimml-check` 时优先调用它，不走本流程。
> 无静态降级，统一走接口。Token 凭据、reference 算子表、核查 API 由部署环境提供。
> 来源：旧版配置生成 SKILL（`三层图谱构建规范/skill/common/`）流程固化，2026-09-21 迁入本目录。

---

## 1. 进入条件

Stage 5 配置脚本已生成（每条命令已对照图谱校验）后，**直接进入本流程**。核查是 GATE-3 前的必经步骤（SKILL §11），不再询问是否核查。

---

## 2. 阶段一：信息确认

向用户确认：
1. **核查场景**：新建 vs 动网（动网需现网脚本做基线）
2. **各网元脚本**：动网配置 + 现网配置路径（多网元时每个网元至少一个现网脚本）
3. **逻辑网元类型 + 版本**（如 SMF 20.13.2 / UPF 20.13.2）

**内置步骤**：
- 在 `reference/NE_VERSION_MAPPING.md` 中匹配正确的逻辑网元类型和版本（**务必找到一个正确的**）
- 版本号规范化：
  - 点分式只保留三段（如 `20.13.2`）
  - VRC 版本只保留 SPC 前（如 `V100R009C50`）
- 参数检查：动网配置路径、现网配置路径、**逻辑网元类型**（必须用逻辑类型，否则不能执行）、网元版本
- 告知用户匹配结果（"逻辑网元类型 SMF，版本 20.13.2，核查数据已准备就绪"），确认后执行

---

## 3. 阶段二：Token 获取

凭据由部署环境通过环境变量提供，**不得写入本文件、脚本或对话**：

| 环境变量 | 含义 |
|---|---|
| `COREMASTER_APP_ID` | 应用 ID（部署环境提供） |
| `COREMASTER_APP_CREDENTIAL` | 应用凭据原文（部署环境提供；下方命令会做 base64） |

```bash
curl -k -X POST "https://w3cloud.huawei.com/ApiCommonQuery/appToken/getRestAppDynamicToken" \
  -H "Content-Type: application/json" \
  -d "{\"appId\": \"${COREMASTER_APP_ID}\", \"credential\": \"$(echo -n "${COREMASTER_APP_CREDENTIAL}" | base64 -w0)\"}"
```
- 返回 `result` 值作为 `${TOKEN}`，格式为 `Basic xxx`
- 环境变量缺失时停止并报告“缺少核查凭据”，不要向用户索要凭据明文

---

## 4. 阶段三：构建并执行核查

### 4.1 构建 scriptInfo（两层嵌套数组）

- **外层 []**：多网元的配置集合
- **内层 []**：每个网元的配置信息（动网 + 现网）

```bash
SCRIPT_INFO='[[
  {"fieldId": "full1", "logicalNeType": "SMF", "neVersion": "20.13.2", "scriptType": 1},
  {"fieldId": "full2", "logicalNeType": "SMF", "neVersion": "20.13.2", "scriptType": 2}
],[
  {"fieldId": "full1", "logicalNeType": "UPF", "neVersion": "20.13.2", "scriptType": 1},
  {"fieldId": "full2", "logicalNeType": "UPF", "neVersion": "20.13.2", "scriptType": 2}
]]'
```

参数说明：
- `fieldId`：full1~fullN，待核查脚本（绝对路径，脚本名自动从路径获取）
- `scriptType`：**1=动网(MOP)，2=现网(full)**
- `scenarioId`：`"语法核查算子ID,通用语义算子assetId"`，逗号分隔
  - **语法核查算子ID**：从 `reference/B_AI_CONFIG_CHECK_ITEM_T_SYTAX.md` 匹配 `CHECK_ID`（一个网元配置只匹配一个值）
  - **通用语义算子ID**：从 `reference/B_AI_CONFIG_CHECK_ITEM_T_UNIVERSAL_SEMANTICS.md` 匹配 `CHECK_ID`，或从 `App.groovy` 获取 assetId

### 4.2 执行核查 API

```bash
curl -k -X POST "https://netlive.gts.huawei.com/apiaccess/coremaster/CMAIConfigCheckCoreService/rest/v1/configcheck/general-check" \
  -H "authorization: ${TOKEN}" \
  -H "Cookie: lang_key=zh_cn;locale=zh_CN;" \
  -F "client=GSC" \
  -F "account=agent" \
  -F "scenarioId=${scenarioId}" \
  -F "full1=@/path/to/${MOP_SCRIPT}.txt" \
  -F "full2=@/path/to/${LIVE_SCRIPT}.txt" \
  -F "scriptInfo=${SCRIPT_INFO}" \
  --output check_result.zip
```

---

## 5. 阶段四：解析核查结果并修正

**内置步骤**：
1. 解压 `check_result.zip`，提取核查结果中的**错误提示信息**
2. 分析每条错误，回溯到对应的命令 / 参数 / 对象，修正动网脚本；结果按 SKILL §11 Step A 的规则分类处理（语法错误 / 真实语义错误必须修正；可能误报的语义错误追踪间接引用链后向用户说明）
3. 修正后重新核查，直到通过或用户接受
4. 告知用户："已完成核查并根据结果修正脚本，请确认"，进入 Step B（GATE-3）

`check_result.zip` 及解压内容属于“核查临时文件”，交付时清理（SKILL §11 Step C）。

---

## 6. reference 资源清单

| 文件 | 用途 |
|---|---|
| `reference/NE_VERSION_MAPPING.md` | 逻辑网元类型 + 版本匹配 |
| `reference/B_AI_CONFIG_CHECK_ITEM_T_SYTAX.md` | 语法核查算子 CHECK_ID |
| `reference/B_AI_CONFIG_CHECK_ITEM_T_UNIVERSAL_SEMANTICS.md` | 通用语义算子 CHECK_ID |
| `App.groovy` | 通用语义算子 assetId |

> 这些 reference 由部署环境提供，本 SKILL 不内置。部署时挂载到 `config-generation/reference/` 路径即可。缺失时停止并报告，不凭记忆填写算子 ID。
