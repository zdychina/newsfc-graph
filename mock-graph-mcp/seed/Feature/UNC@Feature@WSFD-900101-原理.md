---
id: "UNC@Feature@WSFD-900101-原理"
type: Feature
name: "5G SA国际漫游原理"
nf: UNC
version: 20.15.2
feature_code: WSFD-900101
doc_type: 原理
mock: true
---

# 5G SA国际漫游原理

【模拟数据】漫游用户注册时，AMF 根据 SUPI 判断用户归属 PLMN；需要访问归属网络 NF（UDM/AUSF/H-SMF）时，经 SEPP 发起跨 PLMN 服务发现与服务调用。HR 模式下会话锚定在 H-SMF，LBO 模式下锚定在 V-SMF。

## 边
- 属于特性: [[UNC@Feature@WSFD-900101]]
