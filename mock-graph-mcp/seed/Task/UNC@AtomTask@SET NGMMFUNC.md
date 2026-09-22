---
id: "UNC@AtomTask@SET NGMMFUNC"
type: "AtomTask"
name: "SET NGMMFUNC"
name_zh: "设置5G移动性管理功能"
nf: "UNC"
ref: "UNC@MMLCommand@SET NGMMFUNC"
status: "active"
---

# 设置5G移动性管理功能（SET NGMMFUNC）

> 【模拟数据】命令静态知识见 [[UNC@MMLCommand@SET NGMMFUNC]]。

## 配置方法

设置 AMF 移动性管理相关的功能开关。

### 配置维度 1：AMF 重选方式（参数 RESELTYPE）

| 取值 | 作用 | 配套必选参数 | 典型场景 |
|---|---|---|---|
| RE_DIRECT | 通过 gNB 重定向到目标 AMF | 无 | AMF 重定向（当前唯一可端到端使用的方式） |
| RE_ROUTE | AMF 间直接转发 | 无 | 协议原因暂不可端到端使用 |

### 配置维度 2：紧急业务（参数 EMG）

| 取值 | 作用 | 配套必选参数 | 典型场景 |
|---|---|---|---|
| EMFNR-1&EMCNR-1 | 支持紧急业务 | 无 | 开启 VoNR 紧急呼叫 |
| EMFNR-0&EMCNR-0 | 不支持紧急业务 | 无 | 默认 |

## 决策点

| 选项 | 影响（联动参数/场景） |
|---|---|
| 是否开启紧急业务 | 需 VoNR 紧急呼叫 License |

## 约束

- **全局开关**（warning）：修改后对全部用户生效 — 需在低话务时段执行

### 引用约束

本命令无引用型参数。

## 边
- 对应命令: [[UNC@MMLCommand@SET NGMMFUNC]]
