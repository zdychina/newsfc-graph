"""模拟 Graph MCP 服务（本地测试 graph-build skill 用；契约对齐 图谱平台接口文档.md §1–§2）。

- 传输：MCP Streamable HTTP（stateless + JSON 响应），端点 POST /mcp
- 工具：get_domains / search_graph / get_md（参数、返回、错误码与平台一致）
- 数据：启动时扫描 --data 目录（可多个）下全部 md；文件变化自动重载
- 调用日志：logs/calls.jsonl（审查 Agent 是否按 skill 要求查询）

用法：
    python server.py [--port 8765] [--data data] [--data <另一目录>] [--key KEY]
仅用 Python 3 标准库。
"""
import argparse
import json
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
SEARCH_BUDGET = 2000


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
    nterms = list(dict.fromkeys(norm(t.strip()) for t in terms))
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

    hits, term_counts = [], {t: 0 for t in nterms}
    for r in pool:
        body_n = norm(r["body"])
        matched, level, body_score, where = [], 0, 0, set()
        for t in nterms:
            ml = meta_level(t, r)
            bc = body_n.count(t)
            if ml or bc:
                matched.append(t)
                term_counts[t] += 1
                level = max(level, ml)
                body_score += bc
                if ml:
                    for f in ("id", "name", "name_zh"):
                        if t in norm(r.get(f)):
                            where.add(f)
                if bc:
                    where.add("body")
        if not matched or (match == "all" and len(matched) < len(nterms)):
            continue
        hits.append((len(matched), level, body_score, r, matched, where))
    if len(hits) > SEARCH_BUDGET:
        raise ToolError("SEARCH_TOO_BROAD", f"命中 {len(hits)} 超过预算，请加过滤", {"total": len(hits)})
    hits.sort(key=lambda h: (-h[0], -h[1], -h[2], h[3]["type"], h[3]["id"]))
    total = len(hits)
    facets = {"layers": {}, "types": {}, "nfs": {}, "versions": {}}
    for h in hits:
        r = h[3]
        for key, val in (("layers", r["layer"]), ("types", r["type"]), ("nfs", r["nf"]), ("versions", r["version"])):
            if val:
                facets[key][val] = facets[key].get(val, 0) + 1
    out = []
    for n, level, bs, r, matched, where in hits[(page - 1) * size: page * size]:
        snips = [{"term": t, "text": s} for t in matched if (s := snippet(r["body"], t))][:3]
        reasons = [f"命中 {n}/{len(nterms)} 词"] + (["元数据等级 %d" % level] if level else []) + \
                  (["正文出现 %d 次" % bs] if bs else [])
        out.append({"id": r["id"], "type": r["type"], "layer": r["layer"], "name": r["name"], "nf": r["nf"],
                    "domain": r["domain"], "scenario": r["scenario"], "version": r["version"],
                    "versions": store.versions(r["id"]), "matched_terms": matched,
                    "matched_in": sorted(where), "snippets": snips, "rank_reasons": reasons})
    recovery = []
    if total == 0:
        recovery = (["USE_MATCH_ANY"] if match == "all" else []) + ["REMOVE_OR_REPHRASE_TERM"] + \
                   (["RELAX_FILTERS"] if filters else [])
    has_more = page * size < total
    return {"terms": terms, "match": match, "applied_filters": filters, "total": total, "page": page,
            "size": size, "has_more": has_more, "next_page": page + 1 if has_more else None, "hits": out,
            "facets": facets, "diagnostics": {"term_counts": term_counts, "recovery_codes": recovery},
            "suggestions": []}


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
]
IMPL = {"get_domains": tool_get_domains, "get_md": tool_get_md, "search_graph": tool_search_graph}
INSTRUCTIONS = ("【模拟 Graph MCP】业务方案定位：get_domains → get_md；不知道 ID：search_graph → get_md；"
                "已知 ID：get_md。每次调用必传 AGENT_USERNAME / AGENT_SESSION_ID。")


def summarize(name, args, result, err):
    if err:
        return {"error": err["code"]}
    if name == "get_md":
        return {"ok": [k for k, v in result.items() if v["ok"]],
                "not_found": [k for k, v in result.items() if not v["ok"]]}
    if name == "search_graph":
        return {"total": result["total"], "top": [h["id"] for h in result["hits"][:5]]}
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
