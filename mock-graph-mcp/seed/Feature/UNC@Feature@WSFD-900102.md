---
id: "UNC@Feature@WSFD-900102"
type: Feature
name: "基于服务区域的SMF选择"
nf: UNC
version: 20.15.2
feature_code: WSFD-900102
doc_type: 概述
mock: true
---

# 基于服务区域的SMF选择

【模拟数据】特性概述。

## 特性定义

AMF 根据用户位置（TAI）与用户类型选择 SMF；漫游用户可按 TAI 发现归属省份 H-SMF。支持本地缓存 SMF 信息以减少服务发现次数。

## 边
- 所需License: [[UNC@License@LKV2SDSC01]]
- 使用命令: [[UNC@MMLCommand@ADD SMFSELPLCY]], [[UNC@MMLCommand@SET SMFCACHEFUNC]]
