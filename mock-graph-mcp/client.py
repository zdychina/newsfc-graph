"""模拟 Graph MCP 的命令行客户端（走完整 MCP 握手：initialize → tools/call）。

用法：
    python client.py domains
    python client.py md "UNC@MMLCommand@ADD PNFPROFILE" "UNC@AtomTask@ADD PNFPROFILE" [--version 20.15.2] [--raw]
    python client.py search "SET NGPAGINGCTRL" "ADD NGPAGINGRULE" [--match all] [--type Feature] [--nf UNC] [--layer 特性层]
    python client.py call get_md '{"ids": ["..."]}'        # 任意工具 + 原始参数（校验未知参数等）

归因字段取环境变量 _AGENT_USERNAME / _AGENT_SESSION_ID（缺省 mock-tester / mock-session）。
默认输出：md 命令打印每个对象的 md 原文（失败项打印错误）；其余打印 JSON。--raw 一律打印 JSON。
仅用 Python 3 标准库。
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

URL = os.environ.get("MOCK_MCP_URL", "http://127.0.0.1:8765/mcp")
KEY = os.environ.get("MOCK_MCP_KEY", "")
# 本地服务不走系统代理（HTTP_PROXY 会把 127.0.0.1 也转发出去 → 502）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def rpc(method, params=None, mid=1):
    body = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}}).encode()
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
        **({"X-API-Key": KEY} if KEY else {})})
    try:
        with OPENER.open(req, timeout=30) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        sys.exit(f"连接失败（{URL}）：{e.reason}——先启动 server.py")


def call(tool, args):
    init = rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                              "clientInfo": {"name": "mock-client", "version": "1"}})
    if "error" in init:
        sys.exit(f"initialize 失败: {init['error']}")
    args = dict(args)
    args.setdefault("AGENT_USERNAME", os.environ.get("_AGENT_USERNAME", "mock-tester"))
    args.setdefault("AGENT_SESSION_ID", os.environ.get("_AGENT_SESSION_ID", "mock-session"))
    resp = rpc("tools/call", {"name": tool, "arguments": args}, mid=2)
    if "error" in resp:
        sys.exit(f"JSON-RPC 错误: {resp['error']}")
    res = resp["result"]
    return json.loads(res["content"][0]["text"]), res.get("isError", False)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("domains")
    p = sub.add_parser("md")
    p.add_argument("ids", nargs="+")
    p.add_argument("--version")
    p.add_argument("--raw", action="store_true")
    p = sub.add_parser("search")
    p.add_argument("terms", nargs="+")
    for f in ("match", "layer", "type", "nf", "version", "domain", "scenario"):
        p.add_argument(f"--{f}")
    p.add_argument("--page", type=int)
    p.add_argument("--size", type=int)
    p = sub.add_parser("call")
    p.add_argument("tool")
    p.add_argument("args", nargs="?", default="{}")
    a = ap.parse_args()

    if a.cmd == "domains":
        out, err = call("get_domains", {})
        if not err:
            for d in out["domains"]:
                print(f"=================== {d['id']}\n{d['md']}")
            return
    elif a.cmd == "md":
        args = {"ids": a.ids, **({"version": a.version} if a.version else {})}
        out, err = call("get_md", args)
        if not err and not a.raw:
            for oid, item in out.items():
                if item["ok"]:
                    print(f"=================== {oid}  (version={item['version']}, versions={item['versions']})\n{item['md']}")
                else:
                    print(f"=================== {oid}  ✗ {item['error_code']}: {item['error']} "
                          f"available_versions={item['available_versions']}")
            return
    elif a.cmd == "search":
        args = {"terms": a.terms}
        for f in ("match", "layer", "type", "nf", "version", "domain", "scenario", "page", "size"):
            if getattr(a, f) is not None:
                args[f] = getattr(a, f)
        out, err = call("search_graph", args)
        if not err:
            print(f"total={out['total']} has_more={out['has_more']} facets={json.dumps(out['facets'], ensure_ascii=False)}")
            print(f"term_counts={json.dumps(out['diagnostics']['term_counts'], ensure_ascii=False)} "
                  f"recovery={out['diagnostics']['recovery_codes']}")
            for h in out["hits"]:
                print(f"- {h['id']}  [{h['type']}] {h['name']}  matched={h['matched_terms']} in={h['matched_in']}")
                for s in h["snippets"]:
                    print(f"    · {s['text'][:140]}")
            return
    else:
        out, err = call(a.tool, json.loads(a.args))
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if err:
        sys.exit(1)


if __name__ == "__main__":
    main()
