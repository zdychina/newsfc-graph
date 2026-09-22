---
id: "UNC@Feature@WSFD-900105"
type: Feature
name: "漫游区域限制"
nf: UNC
version: 20.15.2
feature_code: WSFD-900105
doc_type: 概述
mock: true
---

# 漫游区域限制

【模拟数据】特性概述。

## 特性定义

AMF 按用户群组（IMSI 号段）限制漫游用户可接入的区域，区域由区域码与 TA 成员定义。

## 边
- 所需License: [[UNC@License@LKV2RRR02]]
- 使用命令: [[UNC@MMLCommand@ADD NGACCAREALST]], [[UNC@MMLCommand@ADD AREACODE]], [[UNC@MMLCommand@ADD AREAMEM]], [[UNC@MMLCommand@ADD NGUSRGRP]], [[UNC@MMLCommand@ADD NGUSRGRPMEM]]
