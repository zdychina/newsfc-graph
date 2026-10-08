"""统一搜索核心 search_graph_core（需求 §7）。

职责：terms[]（any/all）同时搜元数据（id/name/name_zh）与正文（body），
输出候选 ID + 匹配原因 + facets；不返回完整 md，不能作为配置字段权威来源。

执行顺序（§7.5，禁止乱序）：
1. 规范化并验证 terms/filters（非法值 → INVALID_FILTER，不是普通 0）；
2. 组合校验（值合法但组合无对象 → INVALID_FILTER_COMBINATION）；
3. filters/latest（object_latest join）在 SQL 阶段前置——不存在先截断候选再过滤；
4. 每 term 元数据 LIKE + 正文（≥3 FTS5 trigram 短语 ORDER BY bm25 / 2 字符按档
   LIKE 或跳正文 / 1 字符恒只搜元数据，见 SHORT_TERM_META_KEY）；
5. 合并 any/all（同一 term 多字段只计一次）；
6. 排序（matched_terms_count ↓, metadata_level ↓, body_rrf ↓, type/id ↑）；
7. 池上限（每 term 每来源 LIMIT POOL_CAP；触顶 → total_is_bounded，不报错）；
8. 分页（外层单页 ≤50）。

排序说明（§7.6）：跨 term 不比 raw BM25，用 RRF：body_rrf = Σ 1/(60+rank)；
短 LIKE term 与纯元数据命中不贡献 body_rrf。对外不暴露浮点 score，只给
rank_reasons。
"""
from collections import Counter

from . import contracts
from .contracts import (
    INDEX_REBUILDING,
    INVALID_ARGUMENT,
    INVALID_FILTER,
    INVALID_FILTER_COMBINATION,
    GraphError,
    GraphQueryError,
    SearchGraphResponse,
)
from ..repos.graph_search_repo import normalize_search_text
from ..service import get_service
from ..ui_layers import UI_LAYER_TYPES, ui_layer_of

# 候选池上限（每 term 每来源）：排序合并只在小池内做——宽泛词不再把百万行拉回
# Python（2026-09-29 超时治理，spec §5.2）。触顶 → total_is_bounded=true，
# 不再报 SEARCH_TOO_BROAD（错误码保留在 contracts 标注 deprecated-unused）。
POOL_CAP = 2_000

# 输入护栏
MAX_TERMS = 10
MAX_TERM_LEN = 80
MAX_SHORT_TERMS = 3
MAX_PAGE_SIZE = 50

# 短词两档开关（spec §5.2.4/D6 修订版，meta 表，每请求读取，读失败回退上次
# 成功值——同 mcp_server._load_config_safe 模式）：body_like=两字词正文走
# LIKE（⚠️ 实测为正文语料级扫描：trigram 索引对 <3 字符模式与 ESCAPE 子句
# 均不生效，1M 行 ~250ms；常见词被 POOL_CAP 早停兜住，罕见两字词在大正文
# 库下仍慢——此时切 metadata_only 档）；metadata_only=两字词只搜元数据。
# 1 字符恒只搜元数据。
SHORT_TERM_META_KEY = "search_short_term_mode"
_last_good_mode = "body_like"


def _load_short_term_mode(conn) -> str:
    global _last_good_mode
    try:
        r = conn.execute(
            "SELECT value FROM meta WHERE key=?", (SHORT_TERM_META_KEY,)).fetchone()
        v = ((r["value"] if r else "") or "body_like").strip()
        if v not in ("body_like", "metadata_only"):
            v = "body_like"
        _last_good_mode = v
        return v
    except Exception:  # noqa: BLE001 配置面故障不放宽也不炸搜索
        return _last_good_mode

_RRF_K = 60
_SNIPPET_PAD_BEFORE = 20
_SNIPPET_PAD_AFTER = 28

_FIELD_ORDER = ("id", "name", "name_zh", "body")


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_phrase(s: str) -> str:
    return '"' + s.replace('"', '""') + '"'


def _from_sql(join_latest: bool) -> str:
    latest = ("JOIN object_latest ol ON ol.id = graph_search_fts.obj_id "
              "AND ol.version = graph_search_fts.version ") if join_latest else ""
    return ("FROM graph_search_fts " + latest +
            "JOIN objects o ON o.id = graph_search_fts.obj_id "
            "AND o.version = graph_search_fts.version")


def _scope_frags(layer, type_, nf, version, domain, scenario):
    """filters → WHERE 片段（作用于 objects 别名 o）。layer+type 同传=交集：
    校验已保证 type ∈ layer 的类型集，SQL 用 type 收窄（layer 隐含）。"""
    frags: list = []
    params: list = []
    if type_ is not None:
        frags.append("o.type = ?")
        params.append(type_)
    elif layer is not None:
        tl = UI_LAYER_TYPES.get(layer, [])
        frags.append("o.type IN (%s)" % ",".join("?" * len(tl)))
        params.extend(tl)
    if nf is not None:
        frags.append("o.nf = ?")
        params.append(nf)
    if domain is not None:
        frags.append("o.domain = ?")
        params.append(domain)
    if scenario is not None:
        frags.append("o.scenario = ?")
        params.append(scenario)
    if version is not None:
        frags.append("o.version = ?")
        params.append(version)
    return frags, params


def _meta_level(norm_term: str, nid: str, nname: str, nname_zh: str):
    """元数据匹配等级（§7.6）：4=id 全等 / 3=name|name_zh 全等 / 2=前缀 /
    1=包含。返回 (level, 命中字段集合)——**全字段扫描**：一个 term 可同时
    精确命中 name 又包含命中 id，两个字段都进集合，等级取最高。"""
    fields = {"id": nid, "name": nname, "name_zh": nname_zh}
    hit: set = set()
    level = 0
    for f, v in fields.items():
        if not v:
            continue
        if v == norm_term:
            hit.add(f)
            level = max(level, 4 if f == "id" else 3)
        elif v.startswith(norm_term):
            hit.add(f)
            level = max(level, 2)
        elif norm_term in v:
            hit.add(f)
            level = max(level, 1)
    return level, hit


def _validate_terms(terms) -> list:
    """→ [(display, normalized)]：trim 空项/数量/长度校验；规范化后去重保序，
    保留首个原始 term 为展示值（§7.3）。"""
    if not isinstance(terms, list) or not terms:
        raise GraphQueryError(GraphError(
            code=INVALID_ARGUMENT, message="terms 必填：1~10 个关键词"))
    out: list = []
    seen: set = set()
    for t in terms:
        if not isinstance(t, str):
            raise GraphQueryError(GraphError(
                code=INVALID_ARGUMENT, message="terms 每项须为字符串"))
        disp = t.strip()
        n = normalize_search_text(t)
        if not disp or not n:
            raise GraphQueryError(GraphError(
                code=INVALID_ARGUMENT, message="terms 每项 trim 后须非空"))
        if len(n) > MAX_TERM_LEN:
            raise GraphQueryError(GraphError(
                code=INVALID_ARGUMENT,
                message=f"term 规范化后最长 {MAX_TERM_LEN} 字符: {disp[:40]!r}"))
        if n not in seen:
            seen.add(n)
            out.append((disp, n))
    if not (1 <= len(out) <= MAX_TERMS):
        raise GraphQueryError(GraphError(
            code=INVALID_ARGUMENT,
            message=f"terms 数量须在 1~{MAX_TERMS}（规范化去重后 {len(out)}）"))
    short = [n for _, n in out if len(n) < 3]
    if len(short) > MAX_SHORT_TERMS:
        raise GraphQueryError(GraphError(
            code=INVALID_ARGUMENT,
            message=(f"不足 3 字符的短词最多 {MAX_SHORT_TERMS} 个"
                     f"（当前 {len(short)}）——短词无法走索引，请组合更长的关键词")))
    return out


def _validate_filters(conn, layer, type_, nf, version, domain, scenario):
    """动态校验（§12.3）：单值全局不存在 → INVALID_FILTER（带 available_values）；
    layer+type 冲突 → INVALID_FILTER。返回规范化后的 (layer, type_, nf, version,
    domain, scenario)。"""
    from . import catalog

    if layer is not None and layer not in catalog.layers():
        raise GraphQueryError(GraphError(
            code=INVALID_FILTER, message=f"未知 layer: {layer}",
            details={"field": "layer", "value": layer,
                     "available_values": catalog.layers()}))
    types_map = catalog.types(conn)
    if type_ is not None and type_ not in types_map:
        raise GraphQueryError(GraphError(
            code=INVALID_FILTER, message=f"未知 type: {type_}",
            details={"field": "type", "value": type_,
                     "available_values": sorted(types_map)}))
    if layer is not None and type_ is not None and types_map.get(type_) != layer:
        in_layer = sorted(t for t, l in types_map.items() if l == layer)
        raise GraphQueryError(GraphError(
            code=INVALID_FILTER,
            message=f"type {type_} 不属于 {layer}（交集语义，不会静默忽略任一过滤）",
            details={"field": "type", "value": type_, "layer": layer,
                     "available_values": in_layer}))
    if nf is not None:
        nf = nf.strip().upper()
        nfs = catalog.nfs(conn)
        if nf not in nfs:
            raise GraphQueryError(GraphError(
                code=INVALID_FILTER, message=f"未知 nf: {nf}",
                details={"field": "nf", "value": nf,
                         "available_values": nfs}))
    if version is not None:
        version = version.strip()
        versions = catalog.versions(conn)
        if version not in versions:
            raise GraphQueryError(GraphError(
                code=INVALID_FILTER, message=f"未知 version: {version}",
                details={"field": "version", "value": version,
                         "available_values": versions}))
    if domain is not None:
        domains = catalog.domains(conn)
        if domain not in domains:
            raise GraphQueryError(GraphError(
                code=INVALID_FILTER, message=f"未知 domain: {domain}",
                details={"field": "domain", "value": domain,
                         "available_values": domains}))
    if scenario is not None:
        scenarios = catalog.scenarios(conn)
        if scenario not in scenarios:
            raise GraphQueryError(GraphError(
                code=INVALID_FILTER, message=f"未知 scenario: {scenario}",
                details={"field": "scenario", "value": scenario,
                         "available_values": scenarios}))
    return layer, type_, nf, version, domain, scenario


def _check_combination(conn, layer, type_, nf, version, domain, scenario):
    """值各自合法但组合无对象 → INVALID_FILTER_COMBINATION（§12.3），返回
    条件化 available_values：每个已传字段在其他过滤条件下的可选值。"""
    frags, params = _scope_frags(layer, type_, nf, version, domain, scenario)
    join_latest = version is None
    where = (" AND ".join(frags)) if frags else "1=1"
    sql = (f"SELECT COUNT(*) FROM objects o "
           + ("JOIN object_latest ol ON ol.id=o.id AND ol.version=o.version "
              if join_latest else "") + f"WHERE {where}")
    n = conn.execute(sql, params).fetchone()[0]
    if n:
        return
    # 条件化 available_values：逐字段在“其余过滤”约束下的 distinct 值
    available: dict = {}
    given = {"layer": layer, "type": type_, "nf": nf, "version": version,
             "domain": domain, "scenario": scenario}
    for field in given:
        if given[field] is None:
            continue
        others = {k: (v if k != field else None) for k, v in given.items()}
        ofrags, oparams = _scope_frags(others["layer"], others["type"],
                                       others["nf"], others["version"],
                                       others["domain"], others["scenario"])
        owhere = (" AND ".join(ofrags)) if ofrags else "1=1"
        col = {"layer": "o.layer", "type": "o.type", "nf": "o.nf",
               "version": "o.version", "domain": "o.domain",
               "scenario": "o.scenario"}[field]
        rows = conn.execute(
            f"SELECT DISTINCT {col} AS v FROM objects o "
            + ("JOIN object_latest ol ON ol.id=o.id AND ol.version=o.version "
               if join_latest else "")
            + f"WHERE {owhere} AND {col} IS NOT NULL AND {col} != ''",
            oparams).fetchall()
        if field == "layer":
            # registry 层名（Command/Feature/...）→ UI 层名（命令层/...）——
            # available_values 必须与入参词表一致，否则按值重试会再次报错
            # （代码审查 HIGH）
            available[field] = sorted({ui_layer_of(r["v"]) for r in rows})
        else:
            available[field] = [r["v"] for r in rows]
    raise GraphQueryError(GraphError(
        code=INVALID_FILTER_COMBINATION,
        message=("过滤条件组合无对象：请从 available_values 中选择或移除部分过滤"
                 "（单值均合法，组合为空）"),
        details={"available_values": available}))


def _probe_without_filters(conn, norm_term: str, *, probe_body: bool) -> bool:
    """移除过滤后的有界 EXISTS 探针；沿用主查询的短词正文降级策略。"""
    like = f"%{_like_escape(norm_term)}%"
    if len(norm_term) >= 3:
        metadata_hit = conn.execute(
            "SELECT 1 FROM graph_search_fts WHERE graph_search_fts MATCH ? LIMIT 1",
            (f"metadata_text : {_fts_phrase(norm_term)}",),
        ).fetchone()
    else:
        metadata_hit = conn.execute(
            "SELECT 1 FROM graph_search_fts "
            "WHERE metadata_text LIKE ? ESCAPE '\\' LIMIT 1", (like,),
        ).fetchone()
    if metadata_hit:
        return True
    if not probe_body:
        return False
    if len(norm_term) >= 3:
        return conn.execute(
            "SELECT 1 FROM graph_search_fts WHERE graph_search_fts MATCH ? "
            "LIMIT 1", (f"body_text : {_fts_phrase(norm_term)}",)).fetchone() \
            is not None
    return conn.execute(
        "SELECT 1 FROM graph_search_fts WHERE body_text LIKE ? ESCAPE '\\' "
        "LIMIT 1", (like,)).fetchone() is not None


def _snippet(body: str, norm_term: str, disp: str) -> str:
    """原文窗口摘要：normalized 定位（NFKC/casefold 对中英文长度保持；极端
    字符下窗口近似——snippet 非权威来源，§7.6/§7.8）。"""
    norm_body = normalize_search_text(body)
    pos = norm_body.find(norm_term)
    if pos < 0:
        return body[:48]
    start = max(0, pos - _SNIPPET_PAD_BEFORE)
    end = min(len(body), pos + len(norm_term) + _SNIPPET_PAD_AFTER)
    text = body[start:end].replace("\n", " ")
    return ("…" if start > 0 else "") + text + ("…" if end < len(body) else "")


def search_graph_core(*, terms, match: str = "any", layer=None, type=None,
                      nf=None, version=None, domain=None, scenario=None,
                      page: int = 1, size: int = 20) -> dict:
    """统一搜索唯一业务实现（MCP search_graph 调用；REST 不暴露）。返回
    SearchGraphResponse.model_dump()。"""
    svc = get_service()
    if getattr(svc, "fts_rebuilding", False):
        raise GraphQueryError(GraphError(
            code=INDEX_REBUILDING, retryable=True,
            message="全文索引重建中，请稍后重试"))
    if match not in ("any", "all"):
        raise GraphQueryError(GraphError(
            code=INVALID_ARGUMENT, message="match 只支持 any / all"))
    if not isinstance(page, int) or page < 1:
        raise GraphQueryError(GraphError(
            code=INVALID_ARGUMENT, message="page 须为 >=1 的整数"))
    if not isinstance(size, int) or not (1 <= size <= MAX_PAGE_SIZE):
        raise GraphQueryError(GraphError(
            code=INVALID_ARGUMENT,
            message=f"size 须在 1~{MAX_PAGE_SIZE}（防止单页撑爆上下文）"))

    norm_terms = _validate_terms(terms)
    conn = svc.db
    layer, type_, nf, version, domain, scenario = _validate_filters(
        conn, layer, type, nf, version, domain, scenario)
    if any(v is not None for v in (layer, type_, nf, version, domain, scenario)):
        _check_combination(conn, layer, type_, nf, version, domain, scenario)

    # ---------- SQL 阶段：filters/latest 前置 + 每 term 元数据/正文搜索 ----------
    frags, params = _scope_frags(layer, type_, nf, version, domain, scenario)
    scope_and = (" AND " + " AND ".join(frags)) if frags else ""
    from_sql = _from_sql(join_latest=(version is None))
    meta_sel = ("graph_search_fts.obj_id, graph_search_fts.version, "
                "o.type, o.layer, o.nf, o.domain, o.scenario, o.name, "
                "json_extract(o.frontmatter_json, '$.name_zh') AS name_zh")

    agg: dict = {}            # (id, version) → 累积命中
    term_stats: dict = {}     # norm_term → {"hit": bool, "capped": bool}
    any_capped = False
    body_skipped: list = []   # 跳过正文搜索的短词展示值（诊断回显，Task 10）
    short_mode = _load_short_term_mode(conn)

    def _entry(key, row):
        if key not in agg:
            agg[key] = {
                "type": row["type"], "layer": row["layer"], "nf": row["nf"],
                "domain": row["domain"], "scenario": row["scenario"],
                "name": row["name"],
                "terms": set(), "fields": set(), "meta_level": 0,
                "body_terms": set(), "rrf": 0.0,
            }
        return agg[key]

    for _disp, norm in norm_terms:
        seen_keys: set = set()
        capped = False
        # 先取精确/前缀候选，再补普通包含候选。二者合成一条 priority 查询，
        # 避免为每个 term 额外全表扫描两次；>=3 字符同时用 MATCH 先缩候选。
        norm_like = _like_escape(norm)
        prefix_patterns = (norm_like + "%", "%\n" + norm_like + "%")
        exact_patterns = (norm_like, norm_like + "\n%",
                          "%\n" + norm_like + "\n%", "%\n" + norm_like)
        prefix_predicate = (
            "(graph_search_fts.metadata_text LIKE ? ESCAPE '\\' "
            "OR graph_search_fts.metadata_text LIKE ? ESCAPE '\\')")
        exact_order = (
            "CASE WHEN graph_search_fts.metadata_text LIKE ? ESCAPE '\\' "
            "OR graph_search_fts.metadata_text LIKE ? ESCAPE '\\' "
            "OR graph_search_fts.metadata_text LIKE ? ESCAPE '\\' "
            "OR graph_search_fts.metadata_text LIKE ? ESCAPE '\\' "
            "THEN 0 ELSE 1 END")
        if len(norm) >= 3:
            metadata_predicate = "graph_search_fts MATCH ?"
            metadata_param = f"metadata_text : {_fts_phrase(norm)}"
            priority_guard = "AND graph_search_fts MATCH ? "
            priority_params = [*params, metadata_param, *prefix_patterns,
                               *exact_patterns]
        else:
            metadata_predicate = "graph_search_fts.metadata_text LIKE ? ESCAPE '\\'"
            metadata_param = f"%{_like_escape(norm)}%"
            priority_guard = ""
            priority_params = [*params, *prefix_patterns, *exact_patterns]
        priority_rows = conn.execute(
            f"SELECT {meta_sel} {from_sql} WHERE 1=1{scope_and} "
            f"{priority_guard}AND {prefix_predicate} "
            f"ORDER BY {exact_order} LIMIT {POOL_CAP + 1}",
            priority_params).fetchall()
        broad_rows = conn.execute(
            f"SELECT {meta_sel} {from_sql} WHERE 1=1{scope_and} "
            f"AND {metadata_predicate} LIMIT {POOL_CAP + 1}",
            [*params, metadata_param]).fetchall()
        capped = len(priority_rows) > POOL_CAP or len(broad_rows) > POOL_CAP
        rows = []
        metadata_keys = set()
        candidates = [*priority_rows[:POOL_CAP], *broad_rows[:POOL_CAP]]
        for r in candidates:
            key = (r["obj_id"], r["version"])
            if key in metadata_keys:
                continue
            metadata_keys.add(key)
            rows.append(r)
            if len(rows) == POOL_CAP:
                break
        if any((r["obj_id"], r["version"]) not in metadata_keys
               for r in [*priority_rows[:POOL_CAP + 1],
                         *broad_rows[:POOL_CAP + 1]]):
            capped = True
        for r in rows:
            key = (r["obj_id"], r["version"])
            level, fields = _meta_level(
                norm, normalize_search_text(r["obj_id"]),
                normalize_search_text(r["name"]),
                normalize_search_text(r["name_zh"]))
            e = _entry(key, r)
            e["terms"].add(norm)
            e["fields"] |= fields
            e["meta_level"] = max(e["meta_level"], level)
            seen_keys.add(key)
        # 正文：≥3 走 FTS5 trigram 短语（ORDER BY bm25，1-based rank → RRF），池内限量
        if len(norm) >= 3:
            rows = conn.execute(
                f"SELECT {meta_sel} {from_sql} WHERE 1=1{scope_and} "
                "AND graph_search_fts MATCH ? "
                f"ORDER BY bm25(graph_search_fts) LIMIT {POOL_CAP + 1}",
                [*params, f"body_text : {_fts_phrase(norm)}"]).fetchall()
            if len(rows) > POOL_CAP:
                capped = True
            rows = rows[:POOL_CAP]  # 截回后再 enumerate：rank 恒为 1..POOL_CAP
            for rank, r in enumerate(rows, 1):
                key = (r["obj_id"], r["version"])
                e = _entry(key, r)
                e["terms"].add(norm)
                e["fields"].add("body")
                e["body_terms"].add(norm)
                e["rrf"] += 1.0 / (_RRF_K + rank)
                seen_keys.add(key)
        elif len(norm) == 1 or short_mode == "metadata_only":
            # 1 字符（恒跳），或 2 字符 + metadata_only 降级档（大库两字词保命）
            body_skipped.append(_disp)
        else:  # 2 字符 + body_like 档：语料 LIKE（池内限量；成本见模块头注释）
            rows = conn.execute(
                f"SELECT {meta_sel} {from_sql} WHERE 1=1{scope_and} "
                "AND graph_search_fts.body_text LIKE ? ESCAPE '\\' "
                f"LIMIT {POOL_CAP + 1}",
                [*params, f"%{_like_escape(norm)}%"]).fetchall()
            if len(rows) > POOL_CAP:
                capped = True
            rows = rows[:POOL_CAP]
            for r in rows:
                key = (r["obj_id"], r["version"])
                e = _entry(key, r)
                e["terms"].add(norm)
                e["fields"].add("body")
                e["body_terms"].add(norm)
                seen_keys.add(key)
        term_stats[norm] = {
            "hit": bool(seen_keys), "capped": capped, "count": len(seen_keys)}
        any_capped = any_capped or capped

    # ---------- any/all 合并 + 排序 ----------
    n_terms = len(norm_terms)
    if match == "all":
        entries = [(k, e) for k, e in agg.items() if len(e["terms"]) == n_terms]
    else:
        entries = list(agg.items())
    entries.sort(key=lambda kv: (-len(kv[1]["terms"]), -kv[1]["meta_level"],
                                 -kv[1]["rrf"], kv[1]["type"], kv[0][0]))
    total = len(entries)

    # facets：any/all 合并后、分页前的精确计数（§7.7）
    facets = {"layers": Counter(), "types": Counter(),
              "nfs": Counter(), "versions": Counter()}
    for (id_, ver), e in entries:
        facets["layers"][ui_layer_of(e["layer"])] += 1
        facets["types"][e["type"]] += 1
        if e["nf"]:
            facets["nfs"][e["nf"]] += 1
        if ver:
            facets["versions"][ver] += 1
    facets = {k: dict(v) for k, v in facets.items()}

    # ---------- 分页 + 页内字段补全 ----------
    start = (page - 1) * size
    page_entries = entries[start:start + size]
    has_more = total > start + size
    page_ids = [key[0] for key, _e in page_entries]
    versions_map: dict = {}
    body_map: dict = {}
    if page_ids:
        qmarks = ",".join("?" * len(page_ids))
        for r in conn.execute(
            f"SELECT id, version, body_md FROM objects WHERE id IN ({qmarks})",
            page_ids).fetchall():
            ver = r["version"] or None
            if ver is not None:
                versions_map.setdefault(r["id"], []).append(ver)
            body_map[(r["id"], r["version"] or "")] = r["body_md"]
        for vlist in versions_map.values():
            vlist.sort()

    hits: list = []
    for (id_, ver), e in page_entries:
        body = body_map.get((id_, ver or ""), "")
        snippets = []
        for disp, norm in norm_terms:
            if norm in e["body_terms"] and len(snippets) < 3:
                snippets.append({"term": disp, "text": _snippet(body, norm, disp)})
        reasons: list = []
        if e["meta_level"] >= 4:
            reasons.append("ID 精确匹配")
        elif e["meta_level"] == 3:
            reasons.append("名称精确匹配")
        elif e["meta_level"] == 2:
            reasons.append("元数据前缀匹配")
        elif e["meta_level"] == 1:
            reasons.append("元数据包含匹配")
        if e["body_terms"]:
            reasons.append(f"正文命中{len(e['body_terms'])}个关键词")
        reasons.append(f"共命中{len(e['terms'])}/{n_terms}个关键词")
        hits.append({
            "id": id_, "type": e["type"], "layer": ui_layer_of(e["layer"]),
            "name": e["name"], "nf": e["nf"], "domain": e["domain"],
            "scenario": e["scenario"], "version": ver or None,
            "versions": versions_map.get(id_, []),
            "matched_terms": [disp for disp, norm in norm_terms if norm in e["terms"]],
            "matched_in": [f for f in _FIELD_ORDER if f in e["fields"]],
            "snippets": snippets,
            "rank_reasons": reasons,
        })

    # ---------- 诊断与建议 ----------
    # term_counts 保持旧整数形状；新增 term_stats 承载明确的 hit/capped。
    term_counts = {disp: term_stats[norm]["count"] for disp, norm in norm_terms}
    term_status = {
        disp: {"hit": term_stats[norm]["hit"],
               "capped": term_stats[norm]["capped"]}
        for disp, norm in norm_terms
    }
    recovery_codes: list = []
    if total == 0:
        if match == "all" and all(st["hit"] for st in term_stats.values()) \
                and len(term_status) > 1:
            recovery_codes.append("USE_MATCH_ANY")
        if any(not st["hit"] for st in term_stats.values()):
            recovery_codes.append("REMOVE_OR_REPHRASE_TERM")
        if any(v is not None for v in (layer, type_, nf, version, domain, scenario)) \
                and any(_probe_without_filters(
                    conn, norm,
                    probe_body=not (
                        len(norm) == 1
                        or (len(norm) == 2 and short_mode == "metadata_only")),
                ) for _, norm in norm_terms):
            recovery_codes.append("RELAX_FILTERS")
    if total == 0:
        suggestions = [
            "移除 nf/version 等可选过滤后重试",
            "减少 terms 或使用 match=any",
            "命令名、对象名和编号也由 search_graph 自动搜索",
        ]
        if body_skipped:  # 短词未搜正文——可能是零结果主因（T10 评审 Minor）
            suggestions.append(
                "以下短词未搜正文（长度或档位限制）："
                + "、".join(body_skipped))
    else:
        suggestions = ["选择候选 ID 后调用 get_md 获取完整原文"]
    if any_capped:
        suggestions.append(
            "命中量过大已按相关度截断（宽词 match=all 的交集可能不含池外命中）："
            "增加 nf/type/layer 等过滤或减少 terms 可提升排序质量")

    applied = {k: v for k, v in {
        "layer": layer, "type": type_, "nf": nf, "version": version,
        "domain": domain, "scenario": scenario}.items() if v is not None}
    resp = SearchGraphResponse(
        terms=[disp for disp, _ in norm_terms], match=match,
        applied_filters=applied, total=total, total_is_bounded=any_capped,
        page=page, size=size,
        has_more=has_more, next_page=(page + 1) if has_more else None,
        hits=hits,
        facets=facets,
        diagnostics={"term_counts": term_counts,
                     "term_stats": term_status,
                     "recovery_codes": recovery_codes,
                     "body_skipped_short_terms": body_skipped},
        suggestions=suggestions,
    )
    return resp.model_dump()
