---
id: "UNC@Feature@WSFD-900106"
type: Feature
name: "网络切片选择"
nf: UNC
version: 20.15.2
feature_code: WSFD-900106
doc_type: 概述
mock: true
---

# 网络切片选择

【模拟数据】特性概述。

## 特性定义

AMF 根据签约与请求 NSSAI 选择切片；漫游场景下通过切片映射将 HPLMN S-NSSAI 映射到 VPLMN S-NSSAI。

## 边

- 使用命令: [[UNC@MMLCommand@ADD PLMNNS]], [[UNC@MMLCommand@ADD NFNS]], [[UNC@MMLCommand@SET NSMAPPARA]], [[UNC@MMLCommand@ADD AMFDFTNSMAP]]
