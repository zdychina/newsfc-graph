"""语料抽取：md 文档 → corpus.json + corpus_summary.md（确定性抽取，不做语义判断）。

用法：
    python extract_corpus.py <文档.md> -o <batch>/_work/

抽取内容（按章节）：
- statements：代码块内的 MML 语句（verb / command / params / 前置 // 注释 / 行号）
- plans：数据规划表行（命令 / 参数码 / 取值样例）
- licenses：LKV License 码
- mentions：正文（非代码块）中提到的命令名
- conditions：条件型注释 / 正文（语句级作用域附在 statement.conditions）
- 全局：orphan_plans（规划表有、语句无）、anomalies（解析异常）
仅用 Python 3 标准库。
"""
import argparse
import json
import re
import sys
from collections import Counter, OrderedDict
from pathlib import Path

CONFIG_VERBS = {"ADD", "MOD", "SET", "RMV", "DEL", "LOD", "ACT", "DEA"}
QUERY_VERBS = {"DSP", "LST", "EXP", "CHK", "SHOW"}

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
FENCE_RE = re.compile(r"^\s*(```|~~~)")
STMT_START_RE = re.compile(r"^\s*([A-Z]{2,6})\s+([A-Z][A-Z0-9_]*)\s*[:：](.*)$")
CMD_TOKEN_RE = re.compile(r"\b([A-Z]{3})\s+([A-Z][A-Z0-9_]{2,})\b")
LKV_RE = re.compile(r"\bLKV[0-9A-Z]{4,}\b")
PARAM_CODE_RE = re.compile(r"[（(]\s*([A-Z][A-Z0-9_]*)\s*[)）]")
CELL_CMD_RE = re.compile(r"^([A-Z]{3})\s+([A-Z][A-Z0-9_]*)$")
# 条件型注释 / 正文：限定后续语句是否执行（作用域须逐语句还原，见 语料处理.md §3）
CONDITION_RE = re.compile(r"只有|仅|如果|若|如需|按需|可选|测试|商用|试商用|场景下|涉及|不需要|无需|不用|当前不|以下|如下|除外|除非")


def verb_class(verb: str) -> str:
    if verb in CONFIG_VERBS:
        return "config"
    if verb in QUERY_VERBS:
        return "query"
    return "other"


def split_params(body: str, anomalies: list = None) -> "OrderedDict[str, str]":
    """按引号外的逗号切参数；值去引号、去首尾空白。无法解析的片段、值内首尾空白记入 anomalies。"""
    parts, buf, quote = [], [], None
    for ch in body:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
        elif ch in "\"'“”":
            quote = "”" if ch == "“" else ch
            buf.append(ch)
        elif ch in ",，":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    params = OrderedDict()
    for p in parts:
        if not p.strip():
            continue
        if "=" not in p:
            if anomalies is not None:
                anomalies.append(f"无法解析的参数片段：{p.strip()[:60]}")
            continue
        k, v = p.split("=", 1)
        k = k.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", k):
            if anomalies is not None:
                anomalies.append(f"参数名不合法（疑似笔误）：{p.strip()[:60]}")
            continue
        vs = v.strip()
        inner = vs.strip("\"'“”")
        if anomalies is not None and inner != inner.strip():
            anomalies.append(f"{k} 的取值带首尾空白：{vs[:40]}")
        if anomalies is not None and k in params:
            anomalies.append(f"参数 {k} 重复出现")
        params[k] = inner.strip()
    return params


def split_row(line: str) -> list:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def parse(text: str) -> dict:
    lines = text.splitlines()
    sections = []
    stack = []  # [(level, title)]
    cur = None

    def new_section(level, title, lineno):
        nonlocal cur
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        cur = {"path": " / ".join(t for lv, t in stack if lv > 0) or title, "title": title,
               "level": level, "line": lineno, "statements": [], "plans": [],
               "licenses": [], "mentions": [], "conditions": []}
        sections.append(cur)

    new_section(0, "(文档开头)", 1)
    in_code = False
    pending_comment = None
    block_conditions = []  # 当前代码块内已出现的条件型注释 [{line, text}]
    stmt = None  # 跨行语句累积
    table_header = None

    def flush_stmt(terminated=True):
        nonlocal stmt
        if stmt is None:
            return
        body = stmt["raw"].split(":", 1)[1] if ":" in stmt["raw"] else ""
        body = body.rstrip().rstrip(";").strip()
        anomalies = []
        stmt["params"] = split_params(body, anomalies)
        if anomalies:
            stmt["anomalies"] = anomalies
        if block_conditions and not stmt.get("inline"):
            stmt["conditions"] = [dict(c) for c in block_conditions]
        if not terminated:
            stmt["unterminated"] = True
        cur["statements"].append(stmt)
        stmt = None

    for i, line in enumerate(lines, 1):
        if FENCE_RE.match(line):
            if in_code:
                flush_stmt(terminated=False)
                pending_comment = None
            block_conditions = []
            in_code = not in_code
            continue
        if in_code:
            s = line.strip()
            if stmt is not None:
                stmt["raw"] += " " + s
                if s.endswith(";"):
                    flush_stmt()
                continue
            if s.startswith("//"):
                pending_comment = s.lstrip("/").strip()
                if CONDITION_RE.search(pending_comment):
                    block_conditions.append({"line": i, "text": pending_comment[:120]})
                continue
            m = STMT_START_RE.match(line)
            if m:
                verb, cmd = m.group(1), m.group(2)
                raw = f"{verb} {cmd}:" + m.group(3).strip()
                stmt = {"verb": verb, "command": f"{verb} {cmd}",
                        "class": verb_class(verb), "line": i,
                        "comment": pending_comment, "raw": raw}
                pending_comment = None
                if raw.rstrip().endswith(";"):
                    flush_stmt()
            for code in LKV_RE.findall(line):
                cur["licenses"].append(code)
            continue

        # ---- 代码块外 ----
        hm = HEADING_RE.match(line)
        if hm:
            new_section(len(hm.group(1)), hm.group(2), i)
            table_header = None
            continue
        for code in LKV_RE.findall(line):
            cur["licenses"].append(code)
        if line.lstrip().startswith("|"):
            cells = split_row(line)
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if table_header is None:
                table_header = cells
                continue
            cmds = []
            for c in cells:
                for piece in re.split(r"<br\s*/?>", c):
                    cm = CELL_CMD_RE.match(piece.strip())
                    if cm:
                        cmds.append(f"{cm.group(1)} {cm.group(2)}")
            pm = None
            for c in cells:
                pm = PARAM_CODE_RE.search(c)
                if pm:
                    break
            if cmds and pm:
                val_idx = next((k for k, h in enumerate(table_header)
                                if "取值" in h or "样例" in h or h in ("值", "取值")), None)
                value = cells[val_idx] if val_idx is not None and val_idx < len(cells) else ""
                value = value.strip("\"'“”")
                cur["plans"].append({"commands": cmds, "param": pm.group(1),
                                     "value": value, "line": i})
            continue
        table_header = None
        # 代码块外整行的 MML 语句（文档围栏缺失 / 错位时常见）
        m = STMT_START_RE.match(line)
        if m and line.rstrip().endswith(";"):
            verb, cmd = m.group(1), m.group(2)
            stmt = {"verb": verb, "command": f"{verb} {cmd}", "class": verb_class(verb), "line": i,
                    "comment": None, "raw": f"{verb} {cmd}:" + m.group(3).strip(), "inline": True}
            flush_stmt()
            continue
        if CONDITION_RE.search(line) and not line.lstrip().startswith(("#", "|")) and len(line.strip()) < 200:
            cur["conditions"].append({"line": i, "text": line.strip()[:120]})
        for vm in CMD_TOKEN_RE.finditer(line):
            verb = vm.group(1)
            if verb in CONFIG_VERBS or verb in QUERY_VERBS:
                cur["mentions"].append(f"{verb} {vm.group(2)}")
    flush_stmt(terminated=False)

    for s in sections:
        s["licenses"] = list(OrderedDict.fromkeys(s["licenses"]))
        s["mentions"] = list(OrderedDict.fromkeys(s["mentions"]))
    sections = [s for s in sections if s["statements"] or s["plans"]
                or s["licenses"] or s["mentions"] or s["level"] > 0]

    all_cfg = Counter(st["command"] for s in sections for st in s["statements"]
                      if st["class"] == "config")
    stmt_cmds = {st["command"] for s in sections for st in s["statements"]}
    stmt_params = {(st["command"], k) for s in sections for st in s["statements"] for k in st["params"]}
    orphan_plans = [{"section": s["path"], "line": p["line"], "command": c, "param": p["param"], "value": p["value"],
                     "kind": "命令" if c not in stmt_cmds else "参数"}
                    for s in sections for p in s["plans"] for c in p["commands"]
                    if c not in stmt_cmds or (c, p["param"]) not in stmt_params]
    anomalies = [{"section": s["path"], "line": st["line"], "command": st["command"], "issues": st["anomalies"]}
                 for s in sections for st in s["statements"] if st.get("anomalies")]
    return {
        "sections": sections,
        "totals": {
            "sections": len(sections),
            "statements": sum(len(s["statements"]) for s in sections),
            "config_commands": sorted(all_cfg),
            "config_command_count": len(all_cfg),
            "licenses": sorted({c for s in sections for c in s["licenses"]}),
            "unterminated": sum(1 for s in sections for st in s["statements"]
                                if st.get("unterminated")),
            "inline_statements": sum(1 for s in sections for st in s["statements"] if st.get("inline")),
            "conditional_statements": sum(1 for s in sections for st in s["statements"] if st.get("conditions")),
            "orphan_plan_commands": sorted({o["command"] for o in orphan_plans}),
        },
        "orphan_plans": orphan_plans,
        "anomalies": anomalies,
    }


def summary_md(doc_name: str, data: dict) -> str:
    t = data["totals"]
    out = [f"# 语料抽取汇总：{doc_name}", "",
           f"- 章节 {t['sections']}；MML 语句 {t['statements']}；"
           f"不同配置类命令 {t['config_command_count']}；LKV 码 {len(t['licenses'])}；"
           f"未以分号结尾的语句 {t['unterminated']}", "",
           "| 行 | 章节 | 配置类命令（次数） | 查询/其他命令 | 数据规划行 | LKV |",
           "|---|---|---|---|---|---|"]
    for s in data["sections"]:
        cfg = Counter(st["command"] for st in s["statements"] if st["class"] == "config")
        oth = Counter(st["command"] for st in s["statements"] if st["class"] != "config")
        if not (cfg or oth or s["plans"] or s["licenses"]):
            continue
        fmt = lambda c: "<br>".join(f"{k}×{v}" if v > 1 else k for k, v in c.items())
        out.append(f"| {s['line']} | {s['path']} | {fmt(cfg)} | {fmt(oth)} | "
                   f"{len(s['plans']) or ''} | {'<br>'.join(s['licenses'])} |")
    out += ["", "## 全部配置类命令", "", ", ".join(t["config_commands"]), ""]
    out += ["## 条件注释与逐语句作用域（须在时间线中逐语句标注，见 语料处理.md §3）", "",
            "| 语句行 | 命令 | 生效的条件注释（行号：内容） |", "|---|---|---|"]
    for s in data["sections"]:
        for st in s["statements"]:
            if st.get("conditions"):
                cs = "<br>".join(f"L{c['line']}：{c['text']}" for c in st["conditions"])
                out.append(f"| {st['line']} | {st['command']} | {cs} |")
    out += ["", "## 正文中的条件描述（章节级）", "", "| 行 | 章节 | 内容 |", "|---|---|---|"]
    for s in data["sections"]:
        for c in s["conditions"]:
            out.append(f"| {c['line']} | {s['path']} | {c['text'].replace('|', '/')} |")
    out += ["", f"## 规划表孤立项（数据规划表有、配置语句无：{len(data['orphan_plans'])} 行，须在 P2/P3 逐项处理）", "",
            "> 类型=命令：该命令在全文配置语句中从未出现；类型=参数：命令出现过，但该参数从未被任何语句使用（规划了语句中没有的变体）。", "",
            "| 行 | 章节 | 类型 | 命令 | 参数 | 取值 |", "|---|---|---|---|---|---|"]
    for o in data["orphan_plans"]:
        out.append(f"| {o['line']} | {o['section']} | {o['kind']} | {o['command']} | {o['param']} | {o['value'][:40]} |")
    out += ["", f"## 解析异常 / 待核对（{len(data['anomalies'])} 条语句）", "", "| 行 | 命令 | 问题 |", "|---|---|---|"]
    for a in data["anomalies"]:
        out.append(f"| {a['line']} | {a['command']} | {'；'.join(a['issues'])} |")
    if t["inline_statements"]:
        out += ["", f"> 注意：{t['inline_statements']} 条语句位于代码块外（可能是围栏缺失 / 错位），请人工确认其归属与上下文。"]
    return "\n".join(out) + "\n"


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("doc", type=Path)
    ap.add_argument("-o", "--out", type=Path, required=True, help="输出目录（通常为 <batch>/_work/）")
    a = ap.parse_args()
    text = a.doc.read_text(encoding="utf-8-sig")
    data = parse(text)
    data["source"] = a.doc.name
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "corpus.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    (a.out / "corpus_summary.md").write_text(summary_md(a.doc.name, data), encoding="utf-8")
    t = data["totals"]
    print(f"[抽取] 章节 {t['sections']} / 语句 {t['statements']} / 配置类命令 {t['config_command_count']} "
          f"/ LKV {len(t['licenses'])} / 未结尾 {t['unterminated']} / 代码块外 {t['inline_statements']} "
          f"/ 带条件注释 {t['conditional_statements']} / 规划表孤立命令 {len(t['orphan_plan_commands'])} "
          f"/ 解析异常 {len(data['anomalies'])} → {a.out}")


if __name__ == "__main__":
    sys.exit(main())
