---
id: "UNC@CompoundTask@peer-nf-profile"
type: "CompoundTask"
name: "peer-nf-profile"
name_zh: "对端NF静态配置（Local NRF）"
nf: "UNC"
command_set: ["ADD PNFPROFILE", "ADD PNFSERVICE", "ADD PNFSRVNTFSUBS"]
status: "active"
---

# 对端NF静态配置（Local NRF）

> 【模拟数据】在 AMF 本地静态配置对端 NF 的 Profile、服务实例及通知订阅，用于 NRF 不支持相关 Info 匹配、或需要本地指定对端的场景。

## 配置方法

| 步骤 | 命令 | 关键参数 |
|---|---|---|
| 对端 NF Profile | `ADD PNFPROFILE` → [[UNC@AtomTask@ADD PNFPROFILE]] | `NFINSTANCEID`=<对端实例ID>，`NFTYPE`=<NF类型>，`IPADDRESSTYPE`/`IPV4ADDRESS1`/`IPV6ADDRESS1`=<规划值>，`PORT`=<规划值> |
| 对端服务实例 | `ADD PNFSERVICE` → [[UNC@AtomTask@ADD PNFSERVICE]] | `NFINSTANCEID`=引用步骤 1，`SRVINSTANCEID`=<规划值>，`SERVICENAME`=<服务名> |
| 通知订阅 | `ADD PNFSRVNTFSUBS` → [[UNC@AtomTask@ADD PNFSRVNTFSUBS]] | `NTFICATIONTYPE`=<通知类型>，`CALLBACKURI`=<对端回调地址> |

**典型脚本**：

```
ADD PNFPROFILE: NFINSTANCEID="<实例ID>", NFTYPE=NfLMF, NFSTATUS=Registered, IPADDRESSTYPE=IPTypeV4, IPV4ADDRESS1="<IPv4>", PORT=<端口>;
ADD PNFSERVICE: NFINSTANCEID="<实例ID>", SRVINSTANCEID="<服务实例ID>", SERVICENAME=NlmfLoc, SCHEMA=http, NFSERVICESTATUS=REGISTERED;
ADD PNFSRVNTFSUBS: NFINSTANCEID="<实例ID>", SRVINSTANCEID="<服务实例ID>", NTFICATIONTYPE=LocNty, CALLBACKURI="<回调URI>";
```

**步骤位置**：在 SBI 接口配置之后；先于使用该对端 NF 的业务开关。

## 场景差异

| 引用方 / 场景 | 执行命令 / 省略命令 | 相对基线参数差异（参数=值） | 对象与步骤位置 |
|---|---|---|---|
| [[UNC@FeatureTask@WSFD-900107]] / MT-LR | 全部执行 | `NFTYPE`=NfLMF，`SERVICENAME`=NlmfLoc，`NTFICATIONTYPE`=LocNty | LMF 对端；在 SET NGLCSPARA 之后 |
| [[UNC@FeatureTask@WSFD-900107]] / NI-LR | 全部执行 | `NFTYPE`=NfGMLC，`SERVICENAME`=NgmlcLoc | GMLC 对端；在 ADD NGNILRPARA 之后 |

## 决策点

| 选项/场景 | 影响（参数/命令/联动） |
|---|---|
| 对端需要回调通知 | 执行 ADD PNFSRVNTFSUBS |
| 对端无需通知 | 省略 ADD PNFSRVNTFSUBS |

## 约束

- **实例ID一致**（critical）：三条命令的 NFINSTANCEID 必须一致 — 否则服务与订阅挂不到 Profile 上

## 边
- 组成: [[UNC@AtomTask@ADD PNFPROFILE]], [[UNC@AtomTask@ADD PNFSERVICE]], [[UNC@AtomTask@ADD PNFSRVNTFSUBS]]
- 被引用于: [[UNC@FeatureTask@WSFD-900107]]
