---
id: "UNC@Feature@WSFD-900101"
type: Feature
name: "5G SA国际漫游接入"
nf: UNC
version: 20.15.2
feature_code: WSFD-900101
doc_type: 概述
mock: true
---

# 5G SA国际漫游接入

【模拟数据】特性概述。

## 特性定义

AMF 支持拜访地（VPLMN）用户在本网 5G SA 网络注册并使用业务，支持 Home Routed（HR）与 Local Breakout（LBO）两种漫游会话模式。跨 PLMN 的 NF 间信令经 SEPP 转发。

## 可获得性

| License 项 | 说明 |
|---|---|
| LKV2SAIRA01 | 5G SA国际漫游接入用户数-UAM |

依赖特性：基于服务区域的SMF选择（WSFD-900102），用于为漫游用户选择 H-SMF / V-SMF。

## 边
- 所需License: [[UNC@License@LKV2SAIRA01]]
- 依赖特性: [[UNC@Feature@WSFD-900102]]
- 包含子文档: [[UNC@Feature@WSFD-900101-激活5G SA国际漫游]], [[UNC@Feature@WSFD-900101-原理]]
- 使用命令: [[UNC@MMLCommand@SET NGMMFUNC]], [[UNC@MMLCommand@SET ROAMCOMMPLCY]]
