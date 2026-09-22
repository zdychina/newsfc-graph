"""参数核对：语料中每条语句 / 规划表行的“参数=值” × 平台命令定义（CommandParameter）。

用法：
    python check_params.py <batch>/ [--scope 5.8.6 ...] [--nf UNC]

输入：
- <batch>/_work/corpus.json（extract_corpus.py 产出）
- <batch>/_work/cache/*.md：P2 中 get_md 取回的 MMLCommand 原文，逐个原样保存（文件名任意，以 frontmatter id 为准）
- <batch>/_work/cache/_not_found.txt（可选）：get_md 返回 OBJECT_NOT_FOUND 的 ID，一行一个
产出 <batch>/_work/参数核对.md：逐项问题 + 无法解析的命令定义清单。只做机械比对；结论须人工写入 02 版本差异表。

检查项：命令不存在 / 命令定义未缓存 / 参数不存在 / 枚举不含取值 / 整数越界或非整数 /
必选参数缺失（ADD 类为缺参；SET/MOD 类为“须带当前值或规划值”）。
命令定义里的参数表按表头容错识别（参数标识 / 参数ID / 参数名 …）；识别不了的命令会单独列出，**不按通过处理**。
仅用 Python 3 标准库。
"""
import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

ID_HEAD_RE = re.compile(r"参数标识|参数ID|参数 ID|参数名|Parameter|参数$", re.I)
ENUM_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-&.]*$")


def parse_fm_id(text):
    m = re.search(r"^id:\s*\"?([^\"\n]+)\"?\s*$", text, re.M)
    return m.group(1).strip() if m else None


def parse_params(md):
    """从命令 md 中找参数表；返回 {参数: {must, type, enum, range, raw}} 或 None（识别失败）。"""
    lines = md.splitlines()
    best = None
    i = 0
    while i < len(lines):
        if lines[i].lstrip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:\-|]+\|\s*$", lines[i + 1]):
            head = [c.strip() for c in lines[i].strip().strip("|").split("|")]
            idcol = next((k for k, h in enumerate(head) if ID_HEAD_RE.search(h)), None)
            rows = []
            j = i + 2
            while j < len(lines) and lines[j].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[j].strip().strip("|").split("|")])
                j += 1
            if idcol is not None:
                params = {}
                for r in rows:
                    if idcol >= len(r):
                        continue
                    pid = re.sub(r"[`*]", "", r[idcol]).strip()
                    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", pid):
                        continue
                    raw = " | ".join(r)
                    must = "必选" if re.search(r"必选|Mandatory", raw) else ("可选" if re.search(r"可选|Optional", raw) else None)
                    typ = "枚举" if re.search(r"枚举|Enum", raw) else ("整数" if re.search(r"整数|数值|Integer", raw) else None)
                    enum, rng = None, None
                    m = re.search(r"取值范围[:：]\s*([^；;|]*)", raw)
                    if m:
                        body = m.group(1).strip()
                        rm = re.fullmatch(r"(-?\d+)\s*[~～\-]\s*(-?\d+)\s*(个字符)?", body)
                        if rm and not rm.group(3):
                            rng = (int(rm.group(1)), int(rm.group(2)))
                            typ = typ or "整数"
                        elif typ == "枚举" or (not rm and body):
                            toks = [re.split(r"[（(]", t.strip())[0].strip() for t in re.split(r"[、,，/]", body)]
                            toks = [t for t in toks if t]
                            if toks and all(ENUM_TOKEN_RE.match(t) for t in toks):
                                enum = toks
                                typ = typ or "枚举"
                    params[pid] = {"must": must, "type": typ, "enum": enum, "range": rng, "raw": raw}
                if params and (best is None or len(params) > len(best)):
                    best = params
            i = j
        else:
            i += 1
    return best


def in_scope(path, scope):
    return not scope or any(seg == t or seg.startswith(t + " ") or seg.startswith(t + ".")
                            for t in scope for seg in path.split(" / "))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("batch", type=Path)
    ap.add_argument("--scope", nargs="+")
    ap.add_argument("--nf", default="UNC", help="命令 ID 的网元段（默认 UNC）")
    a = ap.parse_args()
    work = a.batch / "_work"
    corpus = json.loads((work / "corpus.json").read_text(encoding="utf-8"))
    cache = work / "cache"
    defs, unparsed = {}, []
    for f in sorted(cache.glob("*.md")) if cache.exists() else []:
        text = f.read_text(encoding="utf-8-sig")
        oid = parse_fm_id(text)
        if not oid or "@MMLCommand@" not in oid:
            continue
        ps = parse_params(text)
        if ps is None:
            unparsed.append(oid)
        else:
            defs[oid.split("@", 2)[2]] = ps
    nf_file = cache / "_not_found.txt"
    not_found = {l.strip().split("@", 2)[-1] for l in nf_file.read_text(encoding="utf-8").splitlines()
                 if l.strip()} if nf_file.exists() else set()
    unparsed_cmds = {u.split("@", 2)[2] for u in unparsed}

    items = []  # (级别, 行, 来源, 命令, 参数, 问题)
    uses = []  # (line, source, cmd, params)
    for s in corpus["sections"]:
        if not in_scope(s["path"], a.scope):
            continue
        for st in s["statements"]:
            if st["class"] == "config":
                uses.append((st["line"], "语句", st["command"], dict(st["params"])))
        for p in s["plans"]:
            for c in p["commands"]:
                uses.append((p["line"], "规划表", c, {p["param"]: p["value"]}))
    seen_cmd_issue = set()
    for line, src, cmd, params in uses:
        if cmd not in defs:
            if cmd in seen_cmd_issue:
                continue
            seen_cmd_issue.add(cmd)
            if cmd in not_found:
                items.append(("CRITICAL", line, src, cmd, "-", "平台上不存在该命令（版本差异：命令不存在）"))
            elif cmd in unparsed_cmds:
                items.append(("HIGH", line, src, cmd, "-", "命令定义中识别不到参数表，须人工核对"))
            else:
                items.append(("HIGH", line, src, cmd, "-", f"命令定义未缓存：须 get_md {a.nf}@MMLCommand@{cmd} 并存入 _work/cache/"))
            continue
        d = defs[cmd]
        for k, v in params.items():
            if k not in d:
                items.append(("CRITICAL", line, src, cmd, k, f"参数不存在（文档值 {v}）"))
                continue
            pd, vals = d[k], [x.strip() for x in re.split(r"<br\s*/?>", v) if x.strip()]
            for val in vals:
                if pd["enum"] and val not in pd["enum"]:
                    items.append(("HIGH", line, src, cmd, k, f"取值 {val} 不在枚举 {pd['enum']} 内"))
                elif pd["range"]:
                    if not re.fullmatch(r"-?\d+", val):
                        items.append(("HIGH", line, src, cmd, k, f"取值 {val} 不是整数（定义 {pd['range'][0]}~{pd['range'][1]}）"))
                    elif not pd["range"][0] <= int(val) <= pd["range"][1]:
                        items.append(("HIGH", line, src, cmd, k, f"取值 {val} 超出范围 {pd['range'][0]}~{pd['range'][1]}"))
        if src == "语句":
            for k, pd in d.items():
                if pd["must"] == "必选" and k not in params:
                    if cmd.startswith(("SET ", "MOD ")):
                        items.append(("WARNING", line, src, cmd, k, "必选参数未给出：修改类命令须带当前值或 <规划值>，在资产中注明“文档未给出”"))
                    else:
                        items.append(("HIGH", line, src, cmd, k, "必选参数缺失：资产中写 <规划值> 并注明“文档未给出，需规划”"))

    # 同一 (命令, 参数, 问题) 只保留首行，附出现次数
    agg = defaultdict(list)
    for lv, line, src, cmd, k, msg in items:
        agg[(lv, cmd, k, msg, src)].append(line)
    order = {"CRITICAL": 0, "HIGH": 1, "WARNING": 2}
    rows = sorted(agg.items(), key=lambda x: (order[x[0][0]], x[0][1], x[0][2]))
    out = [f"# 参数核对：{a.batch.name}", "",
           f"- 命令行：`python check_params.py {a.batch.name}/" + (f" --scope {' '.join(a.scope)}" if a.scope else "") + "`",
           f"- 核对的使用点 {len(uses)}；已解析命令定义 {len(defs)}；无法解析 {len(unparsed)}；平台不存在 {len(not_found)}",
           f"- CRITICAL {sum(1 for r in rows if r[0][0] == 'CRITICAL')} / HIGH {sum(1 for r in rows if r[0][0] == 'HIGH')} / WARNING {sum(1 for r in rows if r[0][0] == 'WARNING')}",
           "", "> 机械比对结果。每一项都要在 02 版本差异表 / 参数核对中给出处理结论；平台定义本身可能有误，不得据此擅改文档取值。", "",
           "| 级别 | 命令 | 参数 | 问题 | 来源 | 行（次数） |", "|---|---|---|---|---|---|"]
    for (lv, cmd, k, msg, src), lines in rows:
        out.append(f"| {lv} | {cmd} | {k} | {msg.replace('|', '/')} | {src} | L{lines[0]}" + (f"（×{len(lines)}）" if len(lines) > 1 else "") + " |")
    if unparsed:
        out += ["", "## 无法解析参数表的命令定义（须人工核对）", ""] + [f"- {u}" for u in unparsed]
    work.mkdir(parents=True, exist_ok=True)
    (work / "参数核对.md").write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"[参数核对] 使用点 {len(uses)} / 定义 {len(defs)} / 问题 {len(rows)}（CRITICAL "
          f"{sum(1 for r in rows if r[0][0] == 'CRITICAL')}）→ {work / '参数核对.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
