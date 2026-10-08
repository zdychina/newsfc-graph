"""模拟 Graph MCP 服务（本地测试 graph-build skill 用；契约对齐 图谱平台接口文档.md §1–§2）。

- 传输：MCP Streamable HTTP（stateless + JSON 响应），端点 POST /mcp
- 工具：get_domains / search_graph / get_md / search_files（参数、返回、错误码与平台一致）
- 数据：启动时扫描 --data 目录（可多个）下全部 md；文件变化自动重载
- 调用日志：logs/calls.jsonl（审查 Agent 是否按 skill 要求查询）

用法：
    python server.py [--port 8765] [--data data] [--data <另一目录>] [--key KEY]
仅用 Python 3 标准库。
"""
import argparse
import json
import os
import re
import sys
import threading
import time
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAYER_OF = {
    "MMLCommand": "命令层", "ConfigObject": "命令层",
    "Feature": "特性层", "License": "特性层",
    "AtomTask": "任务层", "CompoundTask": "任务层", "FeatureTask": "任务层",
    "BusinessDomain": "业务层", "NetworkScenario": "业务层", "ConfigurationSolution": "业务层",
}
LAYERS = ["命令层", "特性层", "任务层", "业务层"]
WIKI_RE = re.compile(r"\[\[([^\[\]]+)\]\]")
MAX_BYTES = 2 * 1024 * 1024
# 每词候选池上限（平台 POOL_CAP=2000）：触顶按相关度截断并置 total_is_bounded，
# 不再报 SEARCH_TOO_BROAD（该错误已退场）。环境变量调小可演练截断/短词路径。
SEARCH_BUDGET = int(os.environ.get("MOCK_POOL_CAP", "2000"))
# 2 字词正文档位（平台 meta 表 search_short_term_mode 可切 metadata_only；1 字词恒跳正文）
SHORT_TERM_MODE = os.environ.get("MOCK_SHORT_TERM_MODE", "body_like")


class ToolError(Exception):
    def __init__(self, code, message, details=None, retryable=False):
        super().__init__(message)
        self.payload = {"code": code, "message": message, "retryable": retryable, "details": details or {}}


def norm(s):
    return unicodedata.normalize("NFKC", s or "").casefold()


def vkey(v):
    return tuple(int(x) if x.isdigit() else x for x in re.split(r"[.\-]", v or "0"))


def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    fm = {}
    for line in text[3:end].splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*)\s*:\s*(.*)$", line)
        if m:
            v = m.group(2).strip()
            if v.startswith("["):
                try:
                    v = json.loads(v)
                except json.JSONDecodeError:
                    v = [x.strip().strip("\"'") for x in v.strip("[]").split(",") if x.strip()]
            elif v in ("null", "~", ""):
                v = None
            else:
                v = v.strip("\"'")
            fm[m.group(1)] = v
    return fm


class Store:
    def __init__(self, dirs):
        self.dirs = [Path(d) for d in dirs]
        self.lock = threading.Lock()
        self.sig = None
        self.objs = {}  # id -> {version(str|None): rec}
        self.reload()

    def _signature(self):
        files = []
        for d in self.dirs:
            if d.exists():
                files += [(str(p), p.stat().st_mtime) for p in d.rglob("*.md")]
        return tuple(sorted(files))

    def reload(self):
        sig = self._signature()
        if sig == self.sig:
            return
        objs = {}
        for d in self.dirs:
            for p in sorted(d.rglob("*.md")) if d.exists() else []:
                text = p.read_text(encoding="utf-8-sig")
                fm = parse_frontmatter(text)
                oid, typ = fm.get("id"), fm.get("type")
                if not oid or typ not in LAYER_OF:
                    continue
                ver = fm.get("version") if LAYER_OF[typ] in ("命令层", "特性层") else None
                body = text[text.find("\n---", 3) + 4:] if text.startswith("---") else text
                objs.setdefault(oid, {})[ver] = {
                    "id": oid, "type": typ, "layer": LAYER_OF[typ], "name": fm.get("name") or oid,
                    "name_zh": fm.get("name_zh"), "nf": fm.get("nf"), "domain": fm.get("domain"),
                    "scenario": fm.get("scenario"), "version": ver, "md": text, "body": body,
                    "references": list(dict.fromkeys(r.strip() for r in WIKI_RE.findall(text))),
                }
        self.objs, self.sig = objs, sig
        print(f"[mock] 已加载 {len(objs)} 个对象（{sum(len(v) for v in objs.values())} 个版本）", flush=True)

    def versions(self, oid):
        vs = [v for v in self.objs.get(oid, {}) if v]
        return sorted(vs, key=vkey)

    def latest(self, oid):
        vers = self.objs.get(oid)
        if not vers:
            return None
        if None in vers:
            return vers[None]
        return vers[self.versions(oid)[-1]]


def check_ctx(args):
    for k, lim in (("AGENT_USERNAME", 64), ("AGENT_SESSION_ID", 128)):
        v = args.get(k)
        if not isinstance(v, str) or not v.strip():
            raise ToolError("INVALID_ARGUMENT", f"{k} 必填且非空")
        if len(v.strip()) > lim:
            raise ToolError("INVALID_ARGUMENT", f"{k} 超过 {lim} 字符")


def check_unknown(args, allowed):
    extra = set(args) - set(allowed) - {"AGENT_USERNAME", "AGENT_SESSION_ID"}
    if extra:
        raise ToolError("INVALID_ARGUMENT", f"未知参数: {sorted(extra)}", {"unknown": sorted(extra)})


def tool_get_domains(store, args):
    check_unknown(args, [])
    check_ctx(args)
    out = []
    for oid in sorted(store.objs):
        r = store.latest(oid)
        if r["type"] == "BusinessDomain":
            out.append({"id": oid, "type": r["type"], "name": r["name"], "version": None,
                        "md": r["md"], "references": r["references"]})
    return {"domains": out}


def md_item(store, r):
    return {"ok": True, "id": r["id"], "type": r["type"], "name": r["name"], "nf": r["nf"],
            "domain": r["domain"], "scenario": r["scenario"], "version": r["version"],
            "versions": store.versions(r["id"]), "md": r["md"], "references": r["references"]}


def tool_get_md(store, args):
    check_unknown(args, ["ids", "version"])
    check_ctx(args)
    ids = args.get("ids")
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i.strip() for i in ids):
        raise ToolError("INVALID_ARGUMENT", "ids 须为 1~100 个非空字符串")
    ids = list(dict.fromkeys(i.strip() for i in ids))
    if len(ids) > 100:
        raise ToolError("INVALID_ARGUMENT", "ids 去重后超过 100")
    ver = args.get("version")
    res = {}
    for oid in ids:
        vers = store.objs.get(oid)
        if not vers:
            res[oid] = {"ok": False, "id": oid, "error_code": "OBJECT_NOT_FOUND", "error": f"对象不存在: {oid}",
                        "requested_version": ver, "available_versions": []}
        elif ver and ver not in vers and None not in vers:
            res[oid] = {"ok": False, "id": oid, "error_code": "VERSION_NOT_FOUND",
                        "error": f"版本不存在: {oid}@{ver}", "requested_version": ver,
                        "available_versions": store.versions(oid)}
        else:
            r = vers.get(ver) if ver and ver in vers else store.latest(oid)
            res[oid] = md_item(store, r)
    if len(json.dumps(res, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_BYTES:
        raise ToolError("RESULT_TOO_LARGE", "响应超过 2MB，请分批", {"limit_bytes": MAX_BYTES})
    return res


def meta_level(term, r):
    best = 0
    for field, full, pre, sub in (("id", 4, 2, 1), ("name", 3, 2, 1), ("name_zh", 3, 2, 1)):
        v = norm(r.get(field))
        if not v:
            continue
        if v == term:
            best = max(best, full)
        elif v.startswith(term) or v.split("@")[-1].startswith(term):
            best = max(best, pre)
        elif term in v:
            best = max(best, sub)
    return best


def snippet(body, term):
    b = norm(body)
    i = b.find(term)
    if i < 0:
        return None
    s = max(0, i - 40)
    return body[s:i + len(term) + 60].replace("\n", " ").strip()


def tool_search_graph(store, args):
    allowed = ["terms", "match", "layer", "type", "nf", "version", "domain", "scenario", "page", "size"]
    check_unknown(args, allowed)
    check_ctx(args)
    terms = args.get("terms")
    if not isinstance(terms, list) or not (1 <= len(terms) <= 10) or \
            not all(isinstance(t, str) and 1 <= len(t.strip()) <= 80 for t in terms):
        raise ToolError("INVALID_ARGUMENT", "terms 须为 1~10 个 1~80 字符的字符串")
    seen_t, pairs = set(), []  # (展示值, 规范化值) 去重——回显与诊断用展示值
    for t in terms:
        n = norm(t.strip())
        if n not in seen_t:
            seen_t.add(n)
            pairs.append((t.strip(), n))
    nterms = [n for _, n in pairs]
    if sum(1 for t in nterms if len(t) < 3) > 3:
        raise ToolError("INVALID_ARGUMENT", "短词（<3 字符）最多 3 个")
    match = args.get("match") or "any"
    if match not in ("any", "all"):
        raise ToolError("INVALID_ARGUMENT", "match 须为 any / all")
    page, size = args.get("page") or 1, args.get("size") or 20
    if not isinstance(page, int) or page < 1 or not isinstance(size, int) or not 1 <= size <= 50:
        raise ToolError("INVALID_ARGUMENT", "page ≥1，size 1~50")

    all_latest = [store.latest(oid) for oid in store.objs]
    filters = {k: args.get(k) for k in ("layer", "type", "nf", "version", "domain", "scenario") if args.get(k)}
    if "nf" in filters:
        filters["nf"] = filters["nf"].upper()
    avail = {
        "layer": LAYERS,
        "type": sorted({r["type"] for r in all_latest}),
        "nf": sorted({r["nf"] for r in all_latest if r["nf"]}),
        "version": sorted({v for oid in store.objs for v in store.versions(oid)}, key=vkey),
        "domain": sorted({r["domain"] for r in all_latest if r["domain"]}),
        "scenario": sorted({r["scenario"] for r in all_latest if r["scenario"]}),
    }
    for k, v in filters.items():
        if v not in avail[k]:
            raise ToolError("INVALID_FILTER", f"未知 {k}: {v}", {"field": k, "value": v, "available_values": avail[k]})
    if "layer" in filters and "type" in filters and LAYER_OF.get(filters["type"]) != filters["layer"]:
        raise ToolError("INVALID_FILTER_COMBINATION", "layer 与 type 冲突",
                        {"layer": filters["layer"], "type": filters["type"]})

    if "version" in filters:
        pool = [vers[filters["version"]] for vers in store.objs.values() if filters["version"] in vers]
    else:
        pool = all_latest
    pool = [r for r in pool if all(
        (r["layer"] if k == "layer" else r[k]) == v for k, v in filters.items() if k != "version")]
    if not pool:
        raise ToolError("INVALID_FILTER_COMBINATION", "过滤组合下无对象", {"filters": filters})

    # 每词候选池（对齐平台 2000/词 上限：触顶按相关度截断置 capped、total_is_bounded，
    # 不报错——平台按来源分段截断，mock 近似为单池按 元数据等级↓/正文命中↓ 截断）
    def skips_body(t):
        return len(t) == 1 or (len(t) == 2 and SHORT_TERM_MODE == "metadata_only")

    body_skipped = [disp for disp, t in pairs if skips_body(t)]
    per_term, capped = {}, {}
    for _, t in pairs:
        cand = []
        for r in pool:
            ml = meta_level(t, r)
            bc = 0 if skips_body(t) else norm(r["body"]).count(t)
            if ml or bc:
                cand.append((ml, bc, r))
        if len(cand) > SEARCH_BUDGET:
            capped[t] = True
            cand = sorted(cand, key=lambda c: (-c[0], -c[1], c[2]["type"], c[2]["id"]))[:SEARCH_BUDGET]
        per_term[t] = cand
    any_capped = any(capped.values())

    agg = {}  # id → {r, terms{t: (元数据等级, 正文次数)}, level, body}
    for t, cand in per_term.items():
        for ml, bc, r in cand:
            e = agg.setdefault(r["id"], {"r": r, "terms": {}, "level": 0, "body": 0})
            e["terms"][t] = (ml, bc)
            e["level"] = max(e["level"], ml)
            e["body"] += bc
    entries = [e for e in agg.values() if match == "any" or len(e["terms"]) == len(nterms)]
    entries.sort(key=lambda e: (-len(e["terms"]), -e["level"], -e["body"], e["r"]["type"], e["r"]["id"]))
    total = len(entries)
    facets = {"layers": {}, "types": {}, "nfs": {}, "versions": {}}
    for e in entries:
        r = e["r"]
        for key, val in (("layers", r["layer"]), ("types", r["type"]), ("nfs", r["nf"]), ("versions", r["version"])):
            if val:
                facets[key][val] = facets[key].get(val, 0) + 1
    out = []
    for e in entries[(page - 1) * size: page * size]:
        r = e["r"]
        matched = [t for t in nterms if t in e["terms"]]
        where, snips = set(), []
        for t in matched:
            ml, bc = e["terms"][t]
            if ml:
                for f in ("id", "name", "name_zh"):
                    if t in norm(r.get(f)):
                        where.add(f)
            if bc:
                where.add("body")
            if (s := snippet(r["body"], t)):
                snips.append({"term": t, "text": s})
        reasons = [f"命中 {len(matched)}/{len(nterms)} 词"] + (["元数据等级 %d" % e["level"]] if e["level"] else []) + \
                  (["正文出现 %d 次" % e["body"]] if e["body"] else [])
        out.append({"id": r["id"], "type": r["type"], "layer": r["layer"], "name": r["name"], "nf": r["nf"],
                    "domain": r["domain"], "scenario": r["scenario"], "version": r["version"],
                    "versions": store.versions(r["id"]), "matched_terms": matched,
                    "matched_in": sorted(where), "snippets": snips[:3], "rank_reasons": reasons})
    recovery = []
    if total == 0:
        recovery = (["USE_MATCH_ANY"] if match == "all" else []) + ["REMOVE_OR_REPHRASE_TERM"] + \
                   (["RELAX_FILTERS"] if filters else [])
    suggestions = (["移除 nf/version 等可选过滤后重试", "减少 terms 或使用 match=any",
                    "命令名、对象名和编号也由 search_graph 自动搜索"] if total == 0
                   else ["选择候选 ID 后调用 get_md 获取完整原文"])
    if total == 0 and body_skipped:
        suggestions.append("以下短词未搜正文（长度或档位限制）：" + "、".join(body_skipped))
    if any_capped:
        suggestions.append("命中量过大已按相关度截断（宽词 match=all 的交集可能不含池外命中）："
                           "增加 nf/type/layer 等过滤或减少 terms 可提升排序质量")
    has_more = page * size < total
    return {"terms": [disp for disp, _ in pairs], "match": match, "applied_filters": filters,
            "total": total, "total_is_bounded": any_capped, "page": page,
            "size": size, "has_more": has_more, "next_page": page + 1 if has_more else None, "hits": out,
            "facets": facets, "diagnostics": {"term_counts": {t: len(per_term[t]) for t in nterms},
                                              "term_stats": {t: {"hit": bool(per_term[t]), "capped": t in capped}
                                                             for t in nterms},
                                              "recovery_codes": recovery,
                                              "body_skipped_short_terms": body_skipped},
            "suggestions": suggestions}


FILES_TOTAL_CAP = 10_000  # search_files 计数封顶（与平台 TOTAL_CAP 一致）


def norm_relpath(rel):
    """path 规范化（对齐平台 normalize_relpath 主要规则）：反斜杠转正斜杠、
    去空段；绝对路径 / `.` / `..` 段拒绝。空串表示根。"""
    raw = rel.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ToolError("INVALID_ARGUMENT", f"非法路径（绝对路径）: {rel}", {"field": "path"})
    parts = raw.split("/")
    if any(p in (".", "..") for p in parts):
        raise ToolError("INVALID_ARGUMENT", f"非法路径（不允许 . 或 .. 路径段）: {rel}", {"field": "path"})
    return "/".join(p for p in parts if p)


def virtual_tree(store):
    """各 --data 目录并集展开为 (相对路径, Path, is_dir)，按 path 升序——
    search_files 的确定性排序与 keyset 游标基准（多目录同路径按先出现者去重）。"""
    rows, seen = [], set()
    for root in store.dirs:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            rel = p.relative_to(root).as_posix()
            if rel not in seen:
                seen.add(rel)
                rows.append((rel, p, p.is_dir()))
    rows.sort(key=lambda r: r[0])
    return rows


def md_link(p):
    """md 文件行 → (obj_id, version)，对齐平台 LEFT JOIN objects：无有效
    frontmatter（或类型不在图层内）→ (None, None)；非命令/特性层 version 恒 None。"""
    if p.suffix.lstrip(".").lower() != "md":
        return None, None
    try:
        fm = parse_frontmatter(p.read_text(encoding="utf-8-sig"))
    except OSError:
        return None, None
    oid, typ = fm.get("id"), fm.get("type")
    if not oid or typ not in LAYER_OF:
        return None, None
    return oid, (fm.get("version") if LAYER_OF[typ] in ("命令层", "特性层") else None)


def tool_search_files(store, args):
    allowed = ["query", "path", "ext", "recursive", "limit", "after"]
    check_unknown(args, allowed)
    check_ctx(args)
    query = args.get("query")
    if query is not None and not isinstance(query, str):
        raise ToolError("INVALID_ARGUMENT", "query 须为字符串")
    nq = norm(query.strip()) if isinstance(query, str) else ""
    if query is not None and not nq:
        raise ToolError("INVALID_ARGUMENT", "query 不能为空白")
    if len(query or "") > 200:
        raise ToolError("INVALID_ARGUMENT", "query 原始长度最长 200 字符")
    if nq and len(nq) < 2:
        raise ToolError("INVALID_ARGUMENT", "文件名搜索词规范化后至少 2 个字符（1 字符无法走索引且无意义）")
    if len(nq) > 80:
        raise ToolError("INVALID_ARGUMENT", "query 规范化后最长 80 字符")
    path = args.get("path")
    if path is not None and not isinstance(path, str):
        raise ToolError("INVALID_ARGUMENT", "path 须为字符串")
    if len((path or "").strip()) > 1024:
        raise ToolError("INVALID_ARGUMENT", "path 最长 1024 字符")
    path_n = norm_relpath((path or "").strip()) if (path or "").strip() else None
    ext = args.get("ext")
    if ext is not None and not isinstance(ext, str):
        raise ToolError("INVALID_ARGUMENT", "ext 须为字符串")
    ext_n = (ext or "").strip().lstrip(".").lower() or None
    if len(ext_n or "") > 64:
        raise ToolError("INVALID_ARGUMENT", "ext 最长 64 字符")
    after = args.get("after")
    if after is not None and not isinstance(after, str):
        raise ToolError("INVALID_ARGUMENT", "after 须为字符串")
    if len(after or "") > 1024:
        raise ToolError("INVALID_ARGUMENT", "after 游标最长 1024 字符")
    if not (nq or path_n or ext_n):
        raise ToolError("INVALID_ARGUMENT", "query / path / ext 至少给一个：query=按文件名搜；path=列目录；组合=交集")
    limit = args.get("limit")
    if limit is None:
        limit = 100
    if not isinstance(limit, int) or not 1 <= limit <= 500:
        raise ToolError("INVALID_ARGUMENT", "limit 须在 1~500")
    recursive = bool(args.get("recursive"))

    rows = virtual_tree(store)
    if path_n is not None and not any(r[2] and r[0] == path_n for r in rows):
        raise ToolError("INVALID_FILTER", f"path 不存在或不是目录: {path_n[:200]}",
                        {"field": "path", "value": path_n[:200]})
    base = path_n + "/" if path_n else ""

    def keep(rel, p, is_dir):
        if base:
            if not rel.startswith(base):
                return False
            if recursive:
                if is_dir:        # find <dir> -type f 语义：递归只留文件行
                    return False
            elif "/" in rel[len(base):]:  # ls 语义：排除更深层 → 直接子项（含子目录行）
                return False
        if nq and nq not in norm(p.name):
            return False
        if ext_n is not None and (is_dir or p.suffix.lstrip(".").lower() != ext_n):
            return False
        return True

    matches = [r for r in rows if r[0] > (after or "") and keep(*r)]
    files = []
    for rel, p, is_dir in matches[:limit]:
        st = p.stat()
        oid, ver = (None, None) if is_dir else md_link(p)
        files.append({"path": rel, "name": p.name, "ext": "" if is_dir else p.suffix.lstrip(".").lower(),
                      "is_dir": is_dir, "size": 0 if is_dir else st.st_size,
                      "mtime": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
                      "obj_id": oid, "version": ver})
    n = len(matches)
    has_more = n > limit
    q_echo = (query.strip() or None) if isinstance(query, str) else None
    applied = {k: v for k, v in {"query": q_echo, "path": path_n, "ext": ext_n,
                                 "recursive": True if (recursive and path_n) else None}.items()
               if v is not None}
    return {"files": files, "total": min(n, FILES_TOTAL_CAP), "total_is_bounded": n > FILES_TOTAL_CAP,
            "has_more": has_more, "next_cursor": matches[limit - 1][0] if has_more else None,
            "index_building": False, "applied_filters": applied}


CTX_PROPS = {
    "AGENT_USERNAME": {"type": "string", "minLength": 1, "maxLength": 64, "description": "工号（环境变量 _AGENT_USERNAME）"},
    "AGENT_SESSION_ID": {"type": "string", "minLength": 1, "maxLength": 128, "description": "会话ID（环境变量 _AGENT_SESSION_ID）"},
}
TOOLS = [
    {"name": "get_domains", "description": "【模拟】返回全部业务域完整 md + references。",
     "inputSchema": {"type": "object", "properties": dict(CTX_PROPS),
                     "required": ["AGENT_USERNAME", "AGENT_SESSION_ID"], "additionalProperties": False}},
    {"name": "get_md", "description": "【模拟】按逻辑 ID 批量取完整 md（1~100 个，权威原文唯一来源）。不传 version 取各 ID 最新版本。",
     "inputSchema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 256}, "minItems": 1, "maxItems": 100},
         "version": {"type": ["string", "null"]}, **CTX_PROPS},
         "required": ["ids", "AGENT_USERNAME", "AGENT_SESSION_ID"], "additionalProperties": False}},
    {"name": "search_graph", "description": "【模拟】统一搜索（元数据 + 正文）。terms 每项是一个字面词或短语；snippet 不是权威依据，选定后须 get_md。",
     "inputSchema": {"type": "object", "properties": {
         "terms": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "minItems": 1, "maxItems": 10},
         "match": {"type": "string", "enum": ["any", "all"]},
         "layer": {"type": ["string", "null"], "enum": LAYERS + [None]},
         "type": {"type": ["string", "null"]}, "nf": {"type": ["string", "null"]},
         "version": {"type": ["string", "null"]}, "domain": {"type": ["string", "null"]},
         "scenario": {"type": ["string", "null"]},
         "page": {"type": "integer", "minimum": 1}, "size": {"type": "integer", "minimum": 1, "maximum": 50},
         **CTX_PROPS},
         "required": ["terms", "AGENT_USERNAME", "AGENT_SESSION_ID"], "additionalProperties": False}},
    {"name": "search_files", "description": "【模拟】文件名搜索/目录浏览（find/ls 语义，不搜内容、不折叠最新版）。"
     "md 命中回带 obj_id+version，可直通 get_md 读该文件对应对象（不带 version 会取最新版，可能不是这个文件）。",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": ["string", "null"], "maxLength": 200},
         "path": {"type": ["string", "null"], "maxLength": 1024},
         "ext": {"type": ["string", "null"], "maxLength": 64},
         "recursive": {"type": "boolean"},
         "limit": {"type": "integer", "minimum": 1, "maximum": 500},
         "after": {"type": ["string", "null"], "maxLength": 1024},
         **CTX_PROPS},
         "required": ["AGENT_USERNAME", "AGENT_SESSION_ID"], "additionalProperties": False}},
]
IMPL = {"get_domains": tool_get_domains, "get_md": tool_get_md, "search_graph": tool_search_graph,
        "search_files": tool_search_files}
INSTRUCTIONS = ("【模拟 Graph MCP】业务方案定位：get_domains → get_md；不知道 ID：search_graph → get_md；"
                "已知 ID：get_md；按文件名/路径找文件：search_files。每次调用必传 AGENT_USERNAME / AGENT_SESSION_ID。")


def summarize(name, args, result, err):
    if err:
        return {"error": err["code"]}
    if name == "get_md":
        return {"ok": [k for k, v in result.items() if v["ok"]],
                "not_found": [k for k, v in result.items() if not v["ok"]]}
    if name == "search_graph":
        return {"total": result["total"], "top": [h["id"] for h in result["hits"][:5]]}
    if name == "search_files":
        return {"total": result["total"], "top": [f["path"] for f in result["files"][:5]]}
    return {"domains": [d["id"] for d in result["domains"]]}


class Handler(BaseHTTPRequestHandler):
    store = None
    key = None
    log_path = None
    log_lock = threading.Lock()

    def log_message(self, fmt, *a):
        pass

    def _send(self, code, obj=None, headers=None):
        body = b"" if obj is None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        if obj is not None:
            self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        self._send(405, {"error": "GET SSE 流未实现（stateless JSON 模式）"})

    def do_DELETE(self):
        self._send(200, {})

    def do_POST(self):
        if self.path.split("?")[0].rstrip("/") != "/mcp":
            return self._send(404, {"detail": "Not Found"})
        if self.key and self.headers.get("X-API-Key") != self.key:
            return self._send(401, {"error": {"code": "UNAUTHENTICATED", "message": "X-API-Key 缺失或无效",
                                              "retryable": False, "details": {}}})
        try:
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"null")
        except json.JSONDecodeError:
            return self._send(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        batch = msg if isinstance(msg, list) else [msg]
        replies = [r for r in (self.handle_rpc(m) for m in batch) if r is not None]
        if not replies:
            return self._send(202)
        self._send(200, replies if isinstance(msg, list) else replies[0])

    def handle_rpc(self, m):
        if not isinstance(m, dict) or "method" not in m:
            return None
        mid, method, params = m.get("id"), m["method"], m.get("params") or {}
        if mid is None:  # notification
            return None
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "mock-graph-mcp", "version": "1.0.0"},
                "instructions": INSTRUCTIONS}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in IMPL:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"未知工具 {name}"}}
            with self.store.lock:
                self.store.reload()
                t0 = time.time()
                try:
                    result, err = IMPL[name](self.store, args), None
                except ToolError as e:
                    result, err = None, e.payload
            self.write_log(name, args, result, err, time.time() - t0)
            text = json.dumps({"error": err} if err else result, ensure_ascii=False)
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": text}], "isError": bool(err)}}
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    def write_log(self, name, args, result, err, dt):
        rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tool": name,
               "operator": args.get("AGENT_USERNAME"), "session": args.get("AGENT_SESSION_ID"),
               "args": {k: v for k, v in args.items() if k not in ("AGENT_USERNAME", "AGENT_SESSION_ID")},
               "result": summarize(name, args, result, err), "ms": round(dt * 1000, 1)}
        with self.log_lock:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[mock] {name} {json.dumps(rec['args'], ensure_ascii=False)[:120]} → "
              f"{json.dumps(rec['result'], ensure_ascii=False)[:160]}", flush=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--data", action="append", type=Path, help="数据目录，可重复（默认 ./data）")
    ap.add_argument("--key", help="要求的 X-API-Key（不传则不校验）")
    ap.add_argument("--log", type=Path, default=HERE / "logs" / "calls.jsonl")
    a = ap.parse_args()
    Handler.store = Store(a.data or [HERE / "data"])
    Handler.key, Handler.log_path = a.key, a.log
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"[mock] 模拟 Graph MCP 就绪 → http://127.0.0.1:{a.port}/mcp（日志 {a.log}）", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
