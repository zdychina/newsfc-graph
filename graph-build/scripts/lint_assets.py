"""资产静态检查：结构 / 边白名单 / 命令闭包 + 产出待平台核实的外部引用清单。

用法：
    python lint_assets.py <batch>/ [--corpus <batch>/_work/corpus.json] [--scope 5.8.6 ...]

读取 <batch>/assets/ 下全部 md；闭包覆盖 corpus 中的配置语句 **与数据规划表行**。
    命令只有落到 Task（被 [[..@AtomTask@CMD]] 引用、是本批 atom、或在 CT command_set 中）才算覆盖；
    被本批引用的平台既有 Task 对象若已缓存在 <batch>/_work/cache/，其编排的命令同样计入；
    未入图清单只认行首条目：`- 章节: 3.4`（整节）、`- CMD：理由`（单命令）、
    `- 参数 CMD.PARAM：理由` / `- 局点数据：CMD.PARAM, ...`（参数级，WARNING 级闭包）。
产出 <batch>/_work/lint_report.md 与 <batch>/_work/external_refs.txt。
存在 CRITICAL/HIGH 时退出码 1。仅用 Python 3 标准库。
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

TASK_TYPES = {"AtomTask", "CompoundTask", "FeatureTask"}
BIZ_TYPES = {"BusinessDomain", "NetworkScenario", "ConfigurationSolution"}
ALL_TYPES = TASK_TYPES | BIZ_TYPES

REQUIRED_FIELDS = {
    "AtomTask": ["id", "type", "name", "name_zh", "nf", "ref", "status"],
    "CompoundTask": ["id", "type", "name", "name_zh", "nf", "command_set", "status"],
    "FeatureTask": ["id", "type", "name", "name_zh", "nf", "ref", "status"],
    "BusinessDomain": ["id", "type", "name", "name_zh", "domain", "status"],
    "NetworkScenario": ["id", "type", "name", "name_zh", "domain", "scenario", "status"],
    "ConfigurationSolution": ["id", "type", "name", "name_zh", "domain", "scenario", "status"],
}
REQUIRED_SECTIONS = {
    "AtomTask": ["配置方法", "决策点", "约束"],
    "CompoundTask": ["配置方法", "场景差异", "决策点", "约束"],
    "FeatureTask": ["配置概览", "配置流程", "激活方法与参数差异", "参数核对", "决策点", "约束"],
    "BusinessDomain": ["概览", "范围与边界"],
    "NetworkScenario": ["概览", "边界", "决策点"],
    "ConfigurationSolution": ["概览", "配置与协同", "决策点", "约束"],
}
# 源类型 → {关系: 允许的目标类型}
EDGE_RULES = {
    "AtomTask": {"对应命令": {"MMLCommand"}},
    "CompoundTask": {"组成": {"AtomTask"}, "引用步骤": {"CompoundTask"},
                     "上游": {"CompoundTask"}, "下游": {"CompoundTask"}},
    "FeatureTask": {"对应特性": {"Feature"}, "编排": {"CompoundTask", "AtomTask"}},
    "BusinessDomain": {"下游场景": {"NetworkScenario"}},
    "NetworkScenario": {"上游域": {"BusinessDomain"}, "下游方案": {"ConfigurationSolution"}},
    "ConfigurationSolution": {"上游场景": {"NetworkScenario"}, "编排特性": {"FeatureTask"},
                              "复用步骤": {"CompoundTask"}, "复用命令": {"AtomTask"}},
}
FORBIDDEN_TARGETS = {"ConfigObject", "License", "CommandParameter"}
BANNED_FIELDS = {"version", "source", "source_evidence_ids"}
QUERY_VERBS = ("DSP", "LST", "EXP", "CHK")
ORCH_SECTIONS = {"配置方法", "配置流程", "配置与协同"}

WIKI_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
EDGE_LINE_RE = re.compile(r"^-\s*([^:：]+?)\s*[:：]\s*(.*)$")


def parse_frontmatter(text):
    if not text.startswith("---"):
        return None, text
    end = text.find("\n---", 3)
    if end < 0:
        return None, text
    raw, body = text[3:end].strip("\n"), text[end + 4:].lstrip("\n")
    fm, key = {}, None
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            key, val = m.group(1), m.group(2).strip()
            if val.startswith("["):
                try:
                    fm[key] = json.loads(val)
                except json.JSONDecodeError:
                    fm[key] = [v.strip().strip("\"'") for v in val.strip("[]").split(",") if v.strip()]
            elif val in ("", "null", "~"):
                fm[key] = [] if val == "" else None
            else:
                fm[key] = val.strip("\"'")
        elif key and line.lstrip().startswith("- ") and isinstance(fm.get(key), list):
            fm[key].append(line.lstrip()[2:].strip().strip("\"'"))
    return fm, body


def id_type(obj_id):
    parts = obj_id.split("@")
    if len(parts) == 3:
        return parts[1]
    if len(parts) == 2:
        return parts[0]
    return None


def sections_of(body):
    """返回 [(标题, 起始行号, 内容)]（仅二级标题）。"""
    out, cur, start, buf = [], None, 0, []
    in_code = False
    for i, line in enumerate(body.splitlines(), 1):
        if line.strip().startswith("```"):
            in_code = not in_code
        m = None if in_code else re.match(r"^##\s+(.+?)\s*$", line)
        if m:
            if cur is not None:
                out.append((cur, start, "\n".join(buf)))
            cur, start, buf = m.group(1), i, []
        else:
            buf.append(line)
    if cur is not None:
        out.append((cur, start, "\n".join(buf)))
    return out


class Report:
    def __init__(self):
        self.items = []

    def add(self, level, obj, msg):
        self.items.append((level, obj, msg))

    def count(self, *levels):
        return sum(1 for lv, _, _ in self.items if lv in levels)


def lint(batch: Path, corpus: Path = None, scope=None, cmdline=""):
    assets, work = batch / "assets", batch / "_work"
    rep = Report()
    objs = {}
    for p in sorted(assets.rglob("*.md")) if assets.exists() else []:
        text = p.read_text(encoding="utf-8-sig")
        fm, body = parse_frontmatter(text)
        rel = p.relative_to(assets)
        if fm is None:
            rep.add("CRITICAL", str(rel), "缺少 YAML frontmatter")
            continue
        oid = fm.get("id") or str(rel)
        if oid in objs:
            rep.add("CRITICAL", oid, f"ID 重复：{objs[oid]['rel']} 与 {rel}")
        objs[oid] = {"fm": fm, "body": body, "rel": rel, "text": text}

    local_ids = set(objs)
    external = set()
    cmd_mentions = set()

    for oid, o in objs.items():
        fm, body, rel = o["fm"], o["body"], o["rel"]
        t = fm.get("type")
        if t not in ALL_TYPES:
            rep.add("CRITICAL", oid, f"type 非本 skill 产出类型：{t}")
            continue
        for f in REQUIRED_FIELDS[t]:
            if fm.get(f) in (None, "", []):
                rep.add("CRITICAL", oid, f"缺必填字段 {f}")
        for f in BANNED_FIELDS & set(fm):
            rep.add("CRITICAL", oid, f"禁止字段 {f}（Task/业务层无版本、无证据）")
        if id_type(oid) != t:
            rep.add("CRITICAL", oid, f"ID 类型段与 type={t} 不一致")
        segs = len(oid.split("@"))
        if (t in TASK_TYPES and segs != 3) or (t in BIZ_TYPES and segs != 2):
            rep.add("CRITICAL", oid, f"ID 段数错误（{segs} 段）")
        if rel.name != f"{oid}.md":
            rep.add("CRITICAL", oid, f"文件名 ≠ ID：{rel.name}")
        parts = rel.parts[:-1]
        if t in TASK_TYPES:
            exp = (t, fm.get("nf", ""))
            if tuple(parts) != exp:
                rep.add("CRITICAL", oid, f"存储路径应为 {'/'.join(exp)}/，实为 {'/'.join(parts)}/")
            if fm.get("nf") and not oid.startswith(f"{fm['nf']}@"):
                rep.add("CRITICAL", oid, "ID 的 nf 段与 nf 字段不一致")
        else:
            dom, scn = fm.get("domain"), fm.get("scenario")
            exp = ("Business", dom) if t == "BusinessDomain" else ("Business", dom, scn)
            if tuple(parts) != exp:
                rep.add("CRITICAL", oid, f"存储路径应为 {'/'.join(map(str, exp))}/，实为 {'/'.join(parts)}/")
            slug = oid.split("@", 1)[1]
            if t == "BusinessDomain" and slug != dom:
                rep.add("CRITICAL", oid, "domain 与 ID slug 不一致")
            if t == "NetworkScenario" and slug != scn:
                rep.add("CRITICAL", oid, "scenario 与 ID slug 不一致")
            if t == "ConfigurationSolution" and scn and not slug.startswith(f"{scn}-"):
                rep.add("CRITICAL", oid, "CS 的 ID 须以 {scenario}- 开头")
            if t == "ConfigurationSolution" and fm.get("operator") and "## 运营商规范" not in body:
                rep.add("HIGH", oid, "带 operator 的 CS 缺 `## 运营商规范` 段")
        if "{{" in o["text"]:
            rep.add("CRITICAL", oid, "残留模板占位 {{…}}")
        if re.search(r"\[\[\s*(\.\.\.|…)?\s*\]\]", o["text"]):
            rep.add("CRITICAL", oid, "残留空引用 [[...]]")
        if re.search(r"^##\s+证据\s*$", body, re.M):
            rep.add("CRITICAL", oid, "禁止 `## 证据` 段")

        secs = sections_of(body)
        names = [s[0] for s in secs]
        for need in REQUIRED_SECTIONS[t]:
            if need not in names:
                rep.add("HIGH", oid, f"缺必备段 `## {need}`")
        if t == "AtomTask" and "### 引用约束" not in body:
            rep.add("HIGH", oid, "约束段缺 `### 引用约束` 子节")
        if not names or names[-1] != "边":
            rep.add("CRITICAL", oid, "`## 边` 缺失或不是最后一段")
            edges_text = ""
        else:
            edges_text = secs[-1][2]
        for sec_name, _, content in secs:
            if sec_name in ORCH_SECTIONS:
                for q in re.findall(r"`((?:%s) [A-Z][A-Z0-9_]*)`" % "|".join(QUERY_VERBS), content):
                    rep.add("HIGH", oid, f"`## {sec_name}` 中出现查询/调测命令 {q}（调测剥离）")

        # 边
        edges = defaultdict(list)
        for line in edges_text.splitlines():
            s = line.strip()
            if not s or s.startswith(">"):
                continue
            m = EDGE_LINE_RE.match(s)
            if not m:
                rep.add("CRITICAL", oid, f"`## 边` 中无法解析的行：{s[:60]}")
                continue
            rel_name, targets = m.group(1), WIKI_RE.findall(m.group(2))
            if not targets:
                rep.add("CRITICAL", oid, f"边「{rel_name}」无 [[目标]]")
            if rel_name == "被引用于":
                rep.add("WARNING", oid, "含「被引用于」边：新写入已取消（既有对象原文保留则忽略）")
                continue
            allowed = EDGE_RULES[t].get(rel_name)
            for tg in targets:
                if id_type(tg.strip()) in FORBIDDEN_TARGETS:
                    rep.add("CRITICAL", oid, f"边指向禁用类型 {id_type(tg.strip())}：{tg}")
            if allowed is None:
                rep.add("CRITICAL", oid, f"关系「{rel_name}」不在 {t} 白名单")
                continue
            for tg in targets:
                tt = id_type(tg.strip())
                if tt in FORBIDDEN_TARGETS:
                    continue
                if tt not in allowed:
                    rep.add("CRITICAL", oid, f"「{rel_name}」目标类型应为 {'/'.join(sorted(allowed))}，实为 {tt}：{tg}")
                edges[rel_name].append(tg.strip())

        if t == "AtomTask":
            if edges.get("对应命令") != [fm.get("ref")]:
                rep.add("CRITICAL", oid, "「对应命令」须恰好 1 个且等于 ref")
        if t == "FeatureTask" and edges.get("对应特性") != [fm.get("ref")]:
            rep.add("CRITICAL", oid, "「对应特性」须恰好 1 个且等于 ref")
        if t == "CompoundTask":
            cs = set(fm.get("command_set") or [])
            comp = {e.split("@", 2)[2] for e in edges.get("组成", []) if e.count("@") == 2}
            if cs != comp:
                rep.add("CRITICAL", oid, f"command_set 与「组成」不一致：仅 command_set {sorted(cs - comp)}；仅组成 {sorted(comp - cs)}")
        if t == "NetworkScenario" and len(edges.get("上游域", [])) != 1:
            rep.add("CRITICAL", oid, "「上游域」须恰好 1 个")
        if t == "ConfigurationSolution" and len(edges.get("上游场景", [])) != 1:
            rep.add("CRITICAL", oid, "「上游场景」须恰好 1 个")

        for ref in WIKI_RE.findall(o["text"]):
            ref = ref.strip()
            if ref not in local_ids:
                external.add(ref)
        # 闭包口径：只认“落到 Task 的命令”——AtomTask 引用 / 本批 atom / CT command_set
        for m in re.finditer(r"\[\[[A-Z]+@AtomTask@([^\]]+)\]\]", o["text"]):
            cmd_mentions.add(m.group(1).strip())
        if t == "AtomTask":
            cmd_mentions.add(fm.get("name") or oid.split("@", 2)[-1])
        if t == "CompoundTask":
            cmd_mentions.update(fm.get("command_set") or [])

    # 复用的平台既有 Task 对象（P2 get_md 后存入 _work/cache/）：被本批引用的，其编排的命令也计入覆盖
    cache_dir = work / "cache"
    for f in sorted(cache_dir.glob("*.md")) if cache_dir.exists() else []:
        ctext = f.read_text(encoding="utf-8-sig")
        cfm, _ = parse_frontmatter(ctext)
        cid, ctype = (cfm or {}).get("id"), (cfm or {}).get("type")
        if ctype not in TASK_TYPES or cid in objs or cid not in external:
            continue
        for m in re.finditer(r"\[\[[A-Z]+@AtomTask@([^\]]+)\]\]", ctext):
            cmd_mentions.add(m.group(1).strip())
        if ctype == "AtomTask":
            cmd_mentions.add(cid.split("@", 2)[-1])
        if ctype == "CompoundTask":
            cmd_mentions.update(cfm.get("command_set") or [])

    # CS 配置编排：引用 FT 必须写激活方法，且名称须在 FT 激活方法表中（FT 在本批或缓存中时可判定）
    def ft_methods(text):
        names, in_tbl = set(), False
        for line in text.splitlines():
            if line.startswith("## "):
                in_tbl = line.strip() == "## 激活方法与参数差异"
                continue
            if in_tbl and line.startswith("|") and not re.match(r"^\|\s*[-:]", line):
                first = line.strip("|").split("|")[0].strip()
                if first and first != "激活方法/条件":
                    names.add(first)
        return names

    known_ft = {oid: o["text"] for oid, o in objs.items() if o["fm"].get("type") == "FeatureTask"}
    for f in sorted(cache_dir.glob("*.md")) if cache_dir.exists() else []:
        ctext = f.read_text(encoding="utf-8-sig")
        cfm, _ = parse_frontmatter(ctext)
        if (cfm or {}).get("type") == "FeatureTask":
            known_ft.setdefault(cfm.get("id"), ctext)
    for oid, o in objs.items():
        if o["fm"].get("type") != "ConfigurationSolution":
            continue
        m = re.search(r"^### 配置编排\s*$(.*?)(?=^##)", o["body"], re.M | re.S)
        if not m:
            rep.add("HIGH", oid, "`## 配置与协同` 下缺 `### 配置编排` 有序列表")
            continue
        for line in m.group(1).splitlines():
            for ft in re.findall(r"\[\[([A-Z]+@FeatureTask@[^\]]+)\]\]", line):
                if not re.match(r"^\s*\d+\.", line):
                    continue
                mm = re.search(r"激活方法[:：]\s*([^；;）)]+)", line)
                if not mm:
                    rep.add("HIGH", oid, f"配置编排引用 {ft} 未写「激活方法：<名称>」（对象规范 §6）")
                elif ft in known_ft and mm.group(1).strip() not in ft_methods(known_ft[ft]):
                    rep.add("HIGH", oid, f"配置编排引用 {ft} 的激活方法「{mm.group(1).strip()}」在该 FT 的激活方法表中不存在")

    # 业务包含树（本批内可判定部分）
    for oid, o in objs.items():
        t = o["fm"].get("type")
        if t == "NetworkScenario":
            bd = f"BusinessDomain@{o['fm'].get('domain')}"
            if bd in objs and f"[[{oid}]]" not in objs[bd]["text"]:
                rep.add("CRITICAL", bd, f"BD 未在「下游场景」列出本批 NS {oid}")
        if t == "ConfigurationSolution":
            ns = f"NetworkScenario@{o['fm'].get('scenario')}"
            if ns in objs and f"[[{oid}]]" not in objs[ns]["text"]:
                rep.add("CRITICAL", ns, f"NS 未列出本批 CS {oid}（下游方案 + 路由表）")
            elif ns not in objs:
                rep.add("INFO", oid, f"所属 {ns} 不在本批：须确认已更新平台 NS 的下游方案与路由表")

    # 命令闭包 + 参数闭包（语句与数据规划表都纳入；未入图清单只认行首条目）
    uncovered = []
    if corpus:
        data = json.loads(corpus.read_text(encoding="utf-8"))
        excl_file = work / "未入图清单.md"
        excl_text = excl_file.read_text(encoding="utf-8") if excl_file.exists() else ""
        excl_secs = [m.group(1) for m in re.finditer(r"^-\s*章节[:：]\s*(\S+)", excl_text, re.M)]
        excl_cmds = {m.group(1) for m in re.finditer(r"^-\s*(?:命令[:：]\s*)?([A-Z]{3} [A-Z][A-Z0-9_]*)", excl_text, re.M)}
        excl_params = {(m.group(1), m.group(2)) for m in re.finditer(
            r"([A-Z]{3} [A-Z][A-Z0-9_]*)\.([A-Z][A-Z0-9_]*)", "\n".join(
                l for l in excl_text.splitlines() if re.match(r"^-\s*(参数|局点数据)", l)))}
        asset_text = "\n".join(o["text"] for o in objs.values())

        def in_scope(path):
            return not scope or any(seg == tok or seg.startswith(tok + " ") or seg.startswith(tok + ".")
                                    for tok in scope for seg in path.split(" / "))

        def sec_excluded(path):
            return any(seg == tok or seg.startswith(tok + " ") or seg.startswith(tok + ".")
                       for tok in excl_secs for seg in path.split(" / "))

        where = defaultdict(list)   # cmd -> [(来源, 行)]
        pwhere = defaultdict(list)  # (cmd, param) -> [行]
        for sec in data["sections"]:
            if not in_scope(sec["path"]) or sec_excluded(sec["path"]):
                continue
            for st in sec["statements"]:
                if st["class"] == "config":
                    where[st["command"]].append(("语句", st["line"]))
                    for k in st["params"]:
                        pwhere[(st["command"], k)].append(st["line"])
            for pl in sec["plans"]:
                for c in pl["commands"]:
                    where[c].append(("规划表", pl["line"]))
                    pwhere[(c, pl["param"])].append(pl["line"])
        for cmd, locs in sorted(where.items()):
            if cmd in cmd_mentions or cmd in excl_cmds:
                continue
            uncovered.append(cmd)
            src = "；".join(sorted({f"{a}L{b}" for a, b in locs}))[:120]
            rep.add("CRITICAL", "闭包", f"{cmd} 未落到任何 Task（Atom 引用 / command_set），也未在未入图清单行首登记（{src}）")
        for (cmd, k), lines in sorted(pwhere.items()):
            if cmd in uncovered or cmd in excl_cmds or (cmd, k) in excl_params:
                continue
            if not re.search(r"(?<![A-Z0-9_])%s(?![A-Z0-9_])" % re.escape(k), asset_text):
                rep.add("WARNING", "参数闭包", f"{cmd}.{k}（L{lines[0]}）未出现在任何资产中，也未登记为「- 参数 / - 局点数据」条目")

    work.mkdir(parents=True, exist_ok=True)
    (work / "external_refs.txt").write_text("\n".join(sorted(external)) + ("\n" if external else ""), encoding="utf-8")
    order = {"CRITICAL": 0, "HIGH": 1, "WARNING": 2, "INFO": 3}
    lines = [f"# lint 报告：{batch.name}", "", f"- 命令行：`{cmdline}`" if cmdline else "- 命令行：（未记录）",
             f"- 对象 {len(objs)}；外部引用 {len(external)}（见 external_refs.txt，须 get_md 核实）",
             f"- CRITICAL {rep.count('CRITICAL')} / HIGH {rep.count('HIGH')} / WARNING {rep.count('WARNING')} / INFO {rep.count('INFO')}",
             "", "| 级别 | 对象 | 问题 |", "|---|---|---|"]
    for lv, obj, msg in sorted(rep.items, key=lambda x: (order[x[0]], x[1])):
        lines.append(f"| {lv} | {obj} | {msg.replace('|', '/')} |")
    (work / "lint_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return rep, objs, external


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("batch", type=Path)
    ap.add_argument("--corpus", type=Path)
    ap.add_argument("--scope", nargs="+", help="闭包校验只覆盖这些章节（标题编号，如 5.8.6；含子节）；不传=全文")
    a = ap.parse_args()
    rep, objs, external = lint(a.batch, a.corpus, a.scope,
                               "python lint_assets.py " + " ".join(sys.argv[1:]))
    for lv, obj, msg in rep.items:
        if lv in ("CRITICAL", "HIGH"):
            print(f"[{lv}] {obj}: {msg}")
    print(f"[核查] 对象 {len(objs)} / 外部引用 {len(external)} / CRITICAL {rep.count('CRITICAL')} "
          f"/ HIGH {rep.count('HIGH')} / WARNING {rep.count('WARNING')} → {a.batch / '_work' / 'lint_report.md'}")
    return 1 if rep.count("CRITICAL", "HIGH") else 0


if __name__ == "__main__":
    sys.exit(main())
