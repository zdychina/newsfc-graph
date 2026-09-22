---
id: "UNC@Feature@WSFD-900107"
type: Feature
name: "位置定位服务（LCS）"
nf: UNC
version: 20.15.2
feature_code: WSFD-900107
doc_type: 概述
mock: true
---

# 位置定位服务（LCS）

【模拟数据】特性概述。

## 特性定义

AMF 支持 MT-LR 与 NI-LR 定位，需要配置 LMF/GMLC 对端 NF 信息。

## 边

- 使用命令: [[UNC@MMLCommand@SET NGLCSPARA]], [[UNC@MMLCommand@ADD NGNILRPARA]], [[UNC@MMLCommand@ADD PNFPROFILE]], [[UNC@MMLCommand@ADD PNFSERVICE]], [[UNC@MMLCommand@ADD PNFSRVNTFSUBS]]
