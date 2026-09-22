---
id: "UNC@Feature@WSFD-900104"
type: Feature
name: "基于位置的地址分配"
nf: UNC
version: 20.15.2
feature_code: WSFD-900104
doc_type: 概述
mock: true
---

# 基于位置的地址分配

【模拟数据】特性概述。

## 特性定义

AMF 按用户所在 TA 划分 IP 区域群，用户跨 IP 区域移动时触发会话重建（跨区下线），以便重新分配本区域地址。

## 边
- 所需License: [[UNC@License@LKV2AABOL01]]
- 使用命令: [[UNC@MMLCommand@ADD NGIPAREAGRP]], [[UNC@MMLCommand@ADD NGIPAREAGRPMEM]]
