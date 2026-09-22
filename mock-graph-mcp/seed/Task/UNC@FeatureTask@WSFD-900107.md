---
id: "UNC@FeatureTask@WSFD-900107"
type: "FeatureTask"
name: "WSFD-900107"
name_zh: "位置定位服务（LCS）"
nf: "UNC"
ref: "UNC@Feature@WSFD-900107"
status: "active"
---

# 位置定位服务（LCS）（WSFD-900107）

> 【模拟数据】特性静态知识见 [[UNC@Feature@WSFD-900107]]。

## 配置概览

MT-LR 与 NI-LR 两种激活方法，均需配置对端 NF。

## 配置流程

1. **LCS 参数**：`SET NGLCSPARA` → [[UNC@AtomTask@SET NGLCSPARA]]
2. **对端 LMF/GMLC**：`ADD PNFPROFILE` + `ADD PNFSERVICE` + `ADD PNFSRVNTFSUBS` → [[UNC@CompoundTask@peer-nf-profile]]

## 激活方法与参数差异

| 激活方法/条件 | 配置相位 | 执行的 Task | 省略的 Task | 关联 AtomTask | 相对基线的参数差异（参数=值） | 目标对象与生效说明 |
|---|---|---|---|---|---|---|
| MT-LR | 参数+对端 | [[UNC@AtomTask@SET NGLCSPARA]] / [[UNC@CompoundTask@peer-nf-profile]] | 无 | [[UNC@AtomTask@SET NGLCSPARA]] | `LCSSW`=ON | LMF |

## 参数核对

| 场景/命令 | 关联 AtomTask | 实际参数=值 | 核对结论 |
|---|---|---|---|
| MT-LR / SET NGLCSPARA | [[UNC@AtomTask@SET NGLCSPARA]] | `LCSSW`=ON | 通过 |

## 决策点

本特性配置用法单一，无分支。

## 约束

- 无特殊约束

## 边
- 对应特性: [[UNC@Feature@WSFD-900107]]
- 编排: [[UNC@AtomTask@SET NGLCSPARA]], [[UNC@CompoundTask@peer-nf-profile]]
