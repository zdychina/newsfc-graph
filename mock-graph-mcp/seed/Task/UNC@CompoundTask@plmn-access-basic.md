---
id: "UNC@CompoundTask@plmn-access-basic"
type: "CompoundTask"
name: "plmn-access-basic"
name_zh: "PLMN接入基础数据"
nf: "UNC"
command_set: ["ADD NGHPLMN", "ADD NGSRVPLMN", "ADD GUAMI"]
status: "active"
---

# PLMN接入基础数据

> 【模拟数据】为 AMF 配置归属 PLMN、服务 PLMN 以及对应的 GUAMI。

## 配置方法

| 步骤 | 命令 | 关键参数 |
|---|---|---|
| 归属 PLMN | `ADD NGHPLMN` → [[UNC@AtomTask@ADD NGHPLMN]] | `MCC`/`MNC`=<规划值> |
| 服务 PLMN | `ADD NGSRVPLMN` → [[UNC@AtomTask@ADD NGSRVPLMN]] | `MCC`/`MNC`=<规划值> |
| GUAMI | `ADD GUAMI` → [[UNC@AtomTask@ADD GUAMI]] | `MCC`/`MNC`=引用服务 PLMN，其余=<规划值> |

**典型脚本**：

```
ADD NGHPLMN: MCC="<MCC>", MNC="<MNC>";
ADD NGSRVPLMN: MCC="<MCC>", MNC="<MNC>";
ADD GUAMI: MCC="<MCC>", MNC="<MNC>", AMFREGIONID=<规划值>, AMFSETID=<规划值>, AMFPOINTER=<规划值>;
```

**步骤位置**：本局数据配置阶段；先于切片与接入控制配置。

## 场景差异

| 引用方 / 场景 | 执行命令 / 省略命令 | 相对基线参数差异（参数=值） | 对象与步骤位置 |
|---|---|---|---|
| 开局基线 | 全部执行 | 与基线相同 | 本局数据阶段 |

## 决策点

本步骤用法单一，无分支。

## 约束

- **PLMN 先于 GUAMI**（critical）：GUAMI 的 MCC/MNC 必须是已配置的服务 PLMN — 否则命令执行失败

## 边
- 组成: [[UNC@AtomTask@ADD NGHPLMN]], [[UNC@AtomTask@ADD NGSRVPLMN]], [[UNC@AtomTask@ADD GUAMI]]
