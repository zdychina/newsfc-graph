---
id: "UNC@FeatureTask@WSFD-900102"
type: "FeatureTask"
name: "WSFD-900102"
name_zh: "基于服务区域的SMF选择"
nf: "UNC"
ref: "UNC@Feature@WSFD-900102"
status: "active"
---

# 基于服务区域的SMF选择（WSFD-900102）

> 【模拟数据】特性静态知识见 [[UNC@Feature@WSFD-900102]]。License 前置：LKV2SDSC01。

## 配置概览

按用户范围配置 SMF 选择策略，可选开启 SMF 本地缓存。

## 配置流程

1. **SMF 选择策略**：`ADD SMFSELPLCY` → [[UNC@AtomTask@ADD SMFSELPLCY]]
   - 关键参数：`SUBRANGE`=<用户范围>，`ROAMTAISW`=<是否携带TAI>
2. **SMF 缓存**：`SET SMFCACHEFUNC` → [[UNC@AtomTask@SET SMFCACHEFUNC]]
   - 关键参数：`SMFCACHESW`=YES

## 激活方法与参数差异

| 激活方法/条件 | 配置相位 | 执行的 Task | 省略的 Task | 关联 AtomTask | 相对基线的参数差异（参数=值） | 目标对象与生效说明 |
|---|---|---|---|---|---|---|
| 本网用户 | 策略 | [[UNC@AtomTask@ADD SMFSELPLCY]] | 无 | [[UNC@AtomTask@ADD SMFSELPLCY]] | `SUBRANGE`=LOCAL_USER | 立即生效 |
| 漫游用户回归属 H-SMF | 策略 | [[UNC@AtomTask@ADD SMFSELPLCY]] | 无 | [[UNC@AtomTask@ADD SMFSELPLCY]] | `SUBRANGE`=FOREIGN_USER，`ROAMTAISW`=YES | 立即生效 |

## 参数核对

| 场景/命令 | 关联 AtomTask | 实际参数=值 | 核对结论 |
|---|---|---|---|
| 漫游 / ADD SMFSELPLCY | [[UNC@AtomTask@ADD SMFSELPLCY]] | `SUBRANGE`=FOREIGN_USER | 通过 |

## 决策点

| 选项/场景 | 走法 | 关键联动 | 影响 |
|---|---|---|---|
| 是否开启缓存 | SET SMFCACHEFUNC | SMFCACHESW | 减少服务发现次数 |

## 约束

- **License**（critical）：需开启 LKV2SDSC01 — 否则策略不生效

## 边
- 对应特性: [[UNC@Feature@WSFD-900102]]
- 编排: [[UNC@AtomTask@ADD SMFSELPLCY]], [[UNC@AtomTask@SET SMFCACHEFUNC]]
