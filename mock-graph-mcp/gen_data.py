"""生成模拟图谱数据：data/ = 自动生成的命令层 + 存量 AtomTask + seed/ 手写数据。

用法：
    python gen_data.py --corpus <batch>/_work/corpus.json [--out data]

- MMLCommand：语料中出现的全部命令（配置类 + 查询类），版本 20.15.2；参数定义由语料中
  实际出现的参数自动推断（类型 / 枚举候选 / 引用关系），**非产品原文**，仅供流程测试。
- 故意埋入的测试点见 INJECT 与 README.md「测试点」。
- 存量 AtomTask：EXISTING_ATOMS 列表中的命令（模板生成）+ seed/Task 下手写对象。
仅用 Python 3 标准库。
"""
import argparse
import json
import re
import shutil
import sys
from collections import OrderedDict, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
NF, VER = "UNC", "20.15.2"
VERB_ZH = {"ADD": "增加", "MOD": "修改", "SET": "设置", "RMV": "删除", "DEL": "删除", "LST": "查询",
           "DSP": "显示", "ACT": "激活", "LOD": "加载"}
PAIRS = {"YES": "NO", "NO": "YES", "ON": "OFF", "OFF": "ON", "ENABLE": "DISABLE", "DISABLE": "ENABLE",
         "TRUE": "FALSE", "FALSE": "TRUE"}

# ---- 故意埋入的测试点（与 README 对应）----
INJECT = {
    # T1 命令不存在（版本差异：命令不存在）
    "drop_commands": ["ADD SBIFQDNPORTPLCY"],
    # T2 参数不存在（版本差异：参数不存在）
    "drop_params": {"SET AMFROAMFUNC": ["VGMLCSW"]},
    # T3 枚举不含文档写法（写法差异 / 冲突待确认）
    "force_enum": {("ADD SCTPLE", "CROSSIPFLG"): ["YES", "NO"],
                   ("SET CACHEPLCY", "CACHEPOLICY"): ["BEST_EFFORT", "ALWAYS"]},
    # T4 平台新增必选参数，文档未给出（待规划）
    "add_params": {"SET DNNCMPT": [("SMFHRUSRFMT", "枚举", True, ["NI", "NIANDOI"],
                                    "AMF 与 H-SMF 交互时 HR 漫游会话的 DNN 格式（20.15.2 新增，必选）")]},
    # T5 多版本：旧版 20.13.2 另存一份（缺 SEPPGRPSELMODE），最新版 20.15.2 新增可选参数
    "multi_version": {"SET ROAMCOMMPLCY": ("20.13.2", [("SEPPGRPSELMODE", "枚举", False,
                                                         ["PRIORITY", "LOADBALANCE"], "SEPP 组选择方式（20.15.2 新增）")])},
}
# 已存在于平台的 AtomTask（模板生成；SET NGMMFUNC 为 seed 手写的“字典不全”版本）
EXISTING_ATOMS = ["ADD PNFPROFILE", "ADD PNFSERVICE", "ADD PNFSRVNTFSUBS", "SET LICENSESWITCH",
                  "ADD SMFSELPLCY", "SET SMFCACHEFUNC", "ADD NGHPLMN", "ADD NGSRVPLMN", "ADD GUAMI",
                  "ADD EPLMNGRP", "ADD EPLMNGRPMEM", "SET NGLCSPARA", "MOD AMFINFO", "MOD NFSERVICE"]


def common3(a, b):
    return any(a[i:i + 3] in b for i in range(len(a) - 2))


def infer(values, name=""):
    vals = [v for v in values if v != ""]
    if re.search(r"(NAME|DESC|ID)$", name) and not all(re.fullmatch(r"[A-Z][A-Z0-9_]*", v) for v in vals):
        return "字符串", None
    if vals and all(re.fullmatch(r"\d+", v) for v in vals):
        return "整数", None
    if vals and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_&\-]*", v) for v in vals) and \
            not any(re.search(r"\d{3,}", v) for v in vals):
        enum = list(OrderedDict.fromkeys(vals))
        for v in list(enum):
            if v.upper() in PAIRS and PAIRS[v.upper()] not in enum:
                enum.append(PAIRS[v.upper()])
        return "枚举", enum
    return "字符串", None


def collect(corpus):
    cmds = OrderedDict()  # cmd -> {params: OrderedDict(name -> [values]), cls}
    for s in corpus["sections"]:
        for st in s["statements"]:
            c = cmds.setdefault(st["command"], {"params": OrderedDict(), "cls": st["class"]})
            for k, v in st["params"].items():
                c["params"].setdefault(k, []).append(v)
    # 引用关系：ADD X 的第一个参数视为 X 的主键；其他命令同名参数 → 引用 ADD X
    keyof = {}
    for cmd, c in cmds.items():
        if cmd.startswith("ADD ") and c["params"]:
            obj = cmd.split(" ", 1)[1]
            key = next((k for k in c["params"]
                        if re.search(r"(IDX|INDEX|ID|NAME|GROUPID)$", k) and common3(k, obj)), None)
            if key:
                keyof.setdefault(key, cmd)
    return cmds, keyof


def command_md(cmd, params, keyof, version, extra_params=(), drop=()):
    verb, obj = cmd.split(" ", 1)
    rows = []
    for name, values in params.items():
        if name in drop:
            continue
        typ, enum = infer(values, name)
        forced = INJECT["force_enum"].get((cmd, name))
        if forced:
            typ, enum = "枚举", forced
        ref = keyof.get(name)
        desc = []
        if typ == "枚举":
            desc.append("取值范围：" + "、".join(enum))
        elif typ == "整数":
            desc.append("取值范围：0~65535")
        else:
            desc.append("取值范围：1~64 个字符")
        if ref and ref != cmd:
            desc.append(f"该参数引用 {ref} 命令配置的对象，须先执行 {ref}")
        mandatory = "必选" if list(params).index(name) == 0 or (ref and ref != cmd) else "可选"
        rows.append(f"| {name} | {name.lower()} | {mandatory} | {typ} | {'；'.join(desc)} |")
    for name, typ, must, enum, note in extra_params:
        rows.append(f"| {name} | {name.lower()} | {'必选' if must else '可选'} | {typ} | "
                    f"{'取值范围：' + '、'.join(enum) + '；' if enum else ''}{note} |")
    example = ", ".join(f"{n}={infer(v, n)[1][0] if infer(v, n)[1] else '<取值>'}"
                        for n, v in list(params.items())[:4] if n not in drop)
    zh = f"{VERB_ZH.get(verb, verb)}{obj}"
    return f"""---
id: "{NF}@MMLCommand@{cmd}"
type: MMLCommand
name: "{cmd}"
name_zh: "{zh}"
nf: {NF}
version: {version}
source: "mock"
status: active
mock: true
---

# {cmd}（{zh}）

> 【模拟数据】参数定义由测试语料自动推断，非产品原文，仅用于 graph-build 流程测试。

## 命令功能

{VERB_ZH.get(verb, verb)} {obj} 配置。

## 注意事项

- 本命令执行后立即生效。

## 参数说明（CommandParameter）

| 参数标识 | 参数名称 | 必选/可选 | 类型 | 参数说明 |
|---|---|---|---|---|
{chr(10).join(rows) if rows else '| - | - | - | - | 本命令无参数 |'}

## 使用实例

```
{cmd}: {example};
```

## 边
"""


def atom_md(cmd, params, keyof):
    verb, obj = cmd.split(" ", 1)
    zh = f"配置{obj}"
    dims, refs = [], []
    for name, values in list(params.items()):
        typ, enum = infer(values, name)
        ref = keyof.get(name)
        if ref and ref != cmd:
            refs.append(f"| {name} | `{ref}` 创建的对象 | 须先执行 {ref} |")
        elif enum and len(dims) < 2:
            rows = "\n".join(f"| {v} | {name}={v} | 无 | - |" for v in enum)
            dims.append(f"### 配置维度 {len(dims) + 1}：{name}（参数 {name}）\n\n| 取值 | 作用 | 配套必选参数 | 典型场景 |\n|---|---|---|---|\n{rows}")
    refs_txt = ("| 参数 | 只能引用 | 说明 |\n|---|---|---|\n" + "\n".join(refs)) if refs else "本命令无引用型参数。"
    return f"""---
id: "{NF}@AtomTask@{cmd}"
type: "AtomTask"
name: "{cmd}"
name_zh: "{zh}"
nf: "{NF}"
ref: "{NF}@MMLCommand@{cmd}"
status: "active"
---

# {zh}（{cmd}）

> 【模拟数据·存量】命令静态知识见 [[{NF}@MMLCommand@{cmd}]]。

## 配置方法

{VERB_ZH.get(verb, verb)} {obj}。

{chr(10).join(dims) if dims else '本命令仅需按规划填写标识类参数，无配置维度分支。'}

## 决策点

本命令用法单一，无分支。

## 约束

- **规划一致**（warning）：标识类参数须与规划一致 — 否则后续引用失败

### 引用约束

{refs_txt}

## 边
- 对应命令: [[{NF}@MMLCommand@{cmd}]]
"""


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corpus", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=HERE / "data")
    a = ap.parse_args()
    corpus = json.loads(a.corpus.read_text(encoding="utf-8"))
    cmds, keyof = collect(corpus)
    if a.out.exists():
        shutil.rmtree(a.out)
    cdir = a.out / "Command" / NF / VER
    cdir.mkdir(parents=True)
    n_cmd = 0
    for cmd, c in cmds.items():
        if cmd in INJECT["drop_commands"]:
            continue
        extra = INJECT["add_params"].get(cmd, [])
        mv = INJECT["multi_version"].get(cmd)
        if mv:
            old_ver, new_params = mv
            odir = a.out / "Command" / NF / old_ver
            odir.mkdir(parents=True, exist_ok=True)
            (odir / f"{NF}@MMLCommand@{cmd}.md").write_text(
                command_md(cmd, c["params"], keyof, old_ver), encoding="utf-8")
            extra = list(extra) + new_params
        (cdir / f"{NF}@MMLCommand@{cmd}.md").write_text(
            command_md(cmd, c["params"], keyof, VER, extra, INJECT["drop_params"].get(cmd, ())), encoding="utf-8")
        n_cmd += 1
    tdir = a.out / "AtomTask" / NF
    tdir.mkdir(parents=True)
    n_atom = 0
    for cmd in EXISTING_ATOMS:
        if cmd in cmds:
            (tdir / f"{NF}@AtomTask@{cmd}.md").write_text(atom_md(cmd, cmds[cmd]["params"], keyof), encoding="utf-8")
            n_atom += 1
    shutil.copytree(HERE / "seed", a.out / "seed")
    n_seed = len(list((HERE / "seed").rglob("*.md")))
    print(f"[gen] MMLCommand {n_cmd}（+{len(INJECT['multi_version'])} 旧版本，缺 {len(INJECT['drop_commands'])} 条）"
          f" / 存量 AtomTask {n_atom} / seed {n_seed} → {a.out}")


if __name__ == "__main__":
    main()
