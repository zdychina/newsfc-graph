---
id: "BusinessDomain@mobility-management"
type: "BusinessDomain"
name: "移动性管理"
name_zh: "移动性管理"
domain: "mobility-management"
status: "active"
---

# 移动性管理

> 【模拟数据】AMF 控制面的注册、接入、移动性相关业务。含 1 个场景。

## 概览

覆盖用户注册接入、寻呼、切换等移动性管理业务。

## 范围与边界

- 含场景：[[NetworkScenario@registration-access]]（用户注册与接入控制）
- 不属于本域：用户面业务感知

## 边
- 下游场景: [[NetworkScenario@registration-access]]
