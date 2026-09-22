---
id: "NetworkScenario@{{scenario}}"
type: "NetworkScenario"
name: "{{场景中文名}}"
name_zh: "{{场景中文名}}"
domain: "{{domain}}"
scenario: "{{scenario}}"
status: "draft"
---

# {{场景中文名}}

> {{一句话：解决什么业务问题}}。属于 [[BusinessDomain@{{domain}}]]。

## 概览

{{场景定义 + 什么业务需求触发本场景 + 典型产出，1-2 段；不写配置}}

## 边界

- 覆盖：{{网元 / 接口 / 控制维度}}
- 不覆盖：{{相邻场景区分}}

## 决策点

### DP1：方案路由

| 业务诉求 / 条件 | 推荐方案 |
|---|---|
| {{诉求，如"中国移动现网 AMF 开通异网漫游"}} | [[ConfigurationSolution@{{scenario}}-{{solution}}]] |

## 约束

- **{{规则名}}**（{{critical|warning|info}}）：{{约束}} — {{违反后果}}

> 无场景级约束则删除本段。

## 边
- 上游域: [[BusinessDomain@{{domain}}]]
- 下游方案: [[ConfigurationSolution@{{scenario}}-{{solution}}]]
