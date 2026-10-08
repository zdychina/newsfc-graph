"""统一搜索 search_graph 测试（M2，需求 §7/§15.1）。

覆盖：terms/any/all、短语 term、短词（<3 字符）、元数据等级排序、RRF、
filter/latest 前置、池上限（POOL_CAP 触顶截断）、facets、分页、
recovery_codes、错误码区分。
"""
import io
import zipfile

import pytest

import app.db as dbmod
import app.service as svc
from app.registry import Registry
from app.store import Store

from app.graph_query import contracts as gq
from app.graph_query.search import search_graph_core

CMD_ADD_URR_V1 = """---
id: UDG@MMLCommand@ADD URR
type: MMLCommand
version: 20.15.2
name: ADD URR
---

在线计费的使用量上报规则配置命令。参数 RG 表示计费组。
旧版正文含N2接口配置说明。

## 边

- 参见 [[UDG@MMLCommand@LST URR]]
"""

CMD_ADD_URR_V2 = """---
id: UDG@MMLCommand@ADD URR
type: MMLCommand
version: 20.16.0
name: ADD URR
---

在线计费的使用量上报规则配置命令（新版）。参数 RG 表示计费组。

## 边

- 参见 [[UDG@MMLCommand@LST URR]]
"""

CMD_LST = """---
id: UDG@MMLCommand@LST URR
type: MMLCommand
version: 20.15.2
name: LST URR
---

查询使用量上报规则。
"""

CO_DETECT = """---
id: UDG@ConfigObject@AFUSRDETECT
type: ConfigObject
version: 20.15.2
name: AFUSRDETECT
name_zh: 计费欺诈用户检测
---

防欺诈检测配置对象。
"""

FEAT_BILLING = """---
id: UDG@Feature@GWFD-020300
type: Feature
name: 在线计费特性
version: 20.15.2
---

支持在线计费的配额管理与用量上报。免费流量与免费RG、免费RatingGroup，
用于计费欺诈场景的配额控制。
"""

FEAT_ANTIFRAUD = """---
id: UDG@Feature@GWFD-020301
type: Feature
name: 计费防欺诈
version: 20.15.2
---

计费防欺诈特性：基于检测规则发现欺诈用户。
"""

DOMAIN = """---
id: BusinessDomain@charging-fraud
type: BusinessDomain
domain: charging-fraud
name: 计费欺诈治理
---

业务域：计费欺诈与免费流量治理。
"""

UNC_CMD = """---
id: UNC@MMLCommand@SET N2MODE
type: MMLCommand
version: 23.1.0
name: SET N2MODE
---

N2接口配置命令。
"""

ALL = {
    "a1.md": CMD_ADD_URR_V1, "a2.md": CMD_ADD_URR_V2, "lst.md": CMD_LST,
    "co.md": CO_DETECT, "fb.md": FEAT_BILLING, "fa.md": FEAT_ANTIFRAUD,
    "dom.md": DOMAIN, "unc.md": UNC_CMD,
}


def _setup(tmp_data_dir, monkeypatch, files=None):
    s = svc.Service.__new__(svc.Service)
    s.store = Store(tmp_data_dir)
    s.registry = Registry.load_default()
    s.db = dbmod.get_db(tmp_data_dir.parent / "test.db")
    dbmod.init_schema(s.db)
    monkeypatch.setattr(dbmod, "_shared", s.db, raising=False)
    from app.bundle import import_bundle
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in (files or ALL).items():
            z.writestr(name, content)
    import_bundle(buf.getvalue(), s.store, s.registry)
    s.rebuild()
    s.fts_rebuilding = False
    monkeypatch.setattr(svc, "_service", s)
    return s


def _ids(out):
    return [h["id"] for h in out["hits"]]


def _err_of(**kw):
    with pytest.raises(gq.GraphQueryError) as ei:
        search_graph_core(**kw)
    return ei.value.error


# ---------------- 1-5 terms / any / all / 短语 / 短词 ----------------

def test_single_term_hit(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["计费防欺诈"])
    assert _ids(out) == ["UDG@Feature@GWFD-020301"]


def test_multi_terms_any_not_zero(tmp_data_dir, monkeypatch):
    """现场附件词组：any 模式不因关键词间无连续空格原文而归零。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(
        terms=["计费欺诈", "免费流量", "免费RG", "免费RatingGroup"], match="any")
    assert out["total"] >= 3  # FEAT_BILLING(4词全含) + DOMAIN + CO_DETECT
    ids = set(_ids(out))
    assert "UDG@Feature@GWFD-020300" in ids
    assert "BusinessDomain@charging-fraud" in ids
    feat = next(h for h in out["hits"] if h["id"] == "UDG@Feature@GWFD-020300")
    assert set(feat["matched_terms"]) == {"计费欺诈", "免费流量", "免费RG", "免费RatingGroup"}


def test_multi_terms_all_semantics(tmp_data_dir, monkeypatch):
    """all=全部 term 命中（位置可分散），不求连续。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(
        terms=["计费欺诈", "免费RG"], match="all")
    ids = set(_ids(out))
    assert "UDG@Feature@GWFD-020300" in ids
    assert "BusinessDomain@charging-fraud" not in ids  # 只含计费欺诈不含免费RG
    assert "UDG@Feature@GWFD-020301" not in ids


def test_phrase_term_with_space(tmp_data_dir, monkeypatch):
    """terms=['ADD URR'] 是一个带空格的完整短语（元数据 id 命中）。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["ADD URR"])
    ids = _ids(out)
    assert "UDG@MMLCommand@ADD URR" in ids
    assert "UDG@MMLCommand@LST URR" not in ids


def test_short_terms_mixed_with_long(tmp_data_dir, monkeypatch):
    """N2/RG 等不足 3 字符关键词可与长词混合参与搜索。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["N2", "接口配置"], match="any")
    assert _ids(out) == ["UNC@MMLCommand@SET N2MODE"]
    out2 = search_graph_core(terms=["RG"], match="any")
    assert "UDG@MMLCommand@ADD URR" in _ids(out2)  # 正文含 RG


def test_short_terms_max_three(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    e = _err_of(terms=["N2", "RG", "a", "b"])
    assert e.code == gq.INVALID_ARGUMENT


def test_short_term_like_escaped(tmp_data_dir, monkeypatch):
    r"""% _ \ 作为字面短词不炸（LIKE 转义）。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["%"])
    assert out["total"] == 0


# ---------------- 6-9 元数据/正文命中与合并去重 ----------------

def test_metadata_hit_afusrdetect(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["AFUSRDETECT"])
    ids = _ids(out)
    assert "UDG@ConfigObject@AFUSRDETECT" in ids
    hit = next(h for h in out["hits"] if h["id"] == "UDG@ConfigObject@AFUSRDETECT")
    assert "id" in hit["matched_in"]  # 命中字段集合：id/name/name_zh/body


def test_metadata_and_body_merged_single_term(tmp_data_dir, monkeypatch):
    """同一 term 同时命中元数据与正文：matched_terms 只出现一次，matched_in 合并。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["计费防欺诈"])
    hit = next(h for h in out["hits"] if h["id"] == "UDG@Feature@GWFD-020301")
    assert hit["matched_terms"] == ["计费防欺诈"]
    assert set(hit["matched_in"]) >= {"name", "body"}  # name=计费防欺诈 + 正文


def test_dedup_terms_after_nfkc_casefold(tmp_data_dir, monkeypatch):
    """规范化后重复 term 去重且保留首个原始展示值。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["ＡＤＤ ＵＲＲ", "add urr"])
    assert out["terms"] == ["ＡＤＤ ＵＲＲ"]
    assert "UDG@MMLCommand@ADD URR" in _ids(out)


def test_terms_count_and_blank(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    assert _err_of(terms=[]).code == gq.INVALID_ARGUMENT
    assert _err_of(terms=[" "]).code == gq.INVALID_ARGUMENT
    assert _err_of(terms=[f"t{i}" for i in range(11)]).code == gq.INVALID_ARGUMENT


# ---------------- 9/18 排序：元数据等级 + 稳定 tuple ----------------

def test_ranking_metadata_levels(tmp_data_dir, monkeypatch):
    """name 前缀(2) > 正文(0)；matched_terms 同数时按等级，再按 (type,id)。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["在线计费"])
    ids = _ids(out)
    # FEAT_BILLING name="在线计费特性" 前缀命中(level2) → 排在正文命中的 CMD 之前
    assert ids.index("UDG@Feature@GWFD-020300") < ids.index("UDG@MMLCommand@ADD URR")
    feat = next(h for h in out["hits"] if h["id"] == "UDG@Feature@GWFD-020300")
    assert feat["rank_reasons"]


def test_ranking_id_exact_top(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["SET N2MODE"])
    assert _ids(out)[0] == "UNC@MMLCommand@SET N2MODE"


def test_all_intersection_common_and_rare_no_false_zero(tmp_data_dir, monkeypatch):
    """common term 命中多对象 + rare term 少对象：all 交集不因中间截断假 0。"""
    _setup(tmp_data_dir, monkeypatch)
    out_any = search_graph_core(terms=["计费", "免费RG"], match="any")
    out_all = search_graph_core(terms=["计费", "免费RG"], match="all")
    assert out_any["total"] > out_all["total"] >= 1


# ---------------- 10-13/24 过滤校验与顺序 ----------------

def test_invalid_nf_returns_available_values(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    e = _err_of(terms=["计费"], nf="uac3000")
    assert e.code == gq.INVALID_FILTER
    assert e.details["field"] == "nf"
    assert set(e.details["available_values"]) >= {"UDG", "UNC"}
    assert e.details["value"] == "UAC3000"  # 自动 trim+大写后的值


def test_nf_auto_uppercase(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["N2"], nf="unc")
    assert _ids(out) == ["UNC@MMLCommand@SET N2MODE"]
    assert out["applied_filters"]["nf"] == "UNC"


def test_layer_type_conflict_invalid_filter(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    e = _err_of(terms=["计费"], layer="命令层", type="Feature")
    assert e.code == gq.INVALID_FILTER


def test_layer_type_intersection(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["URR"], layer="命令层", type="MMLCommand")
    assert set(_ids(out)) == {"UDG@MMLCommand@ADD URR", "UDG@MMLCommand@LST URR"}


def test_invalid_version_and_domain(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    assert _err_of(terms=["计费"], version="99.9.9").code == gq.INVALID_FILTER
    assert _err_of(terms=["计费"], domain="no-such").code == gq.INVALID_FILTER


def test_invalid_combination_vs_invalid_filter(tmp_data_dir, monkeypatch):
    """值合法但组合无对象 → INVALID_FILTER_COMBINATION（条件化 available_values）。

    nf=UDG 合法（有对象）、domain=charging-fraud 合法（BusinessDomain），但
    交集为空（BusinessDomain 的 nf 为空）→ 组合错误，不是普通 0 结果。
    """
    _setup(tmp_data_dir, monkeypatch)
    e = _err_of(terms=["N2"], nf="UDG", domain="charging-fraud")
    assert e.code == gq.INVALID_FILTER_COMBINATION
    assert "available_values" in e.details


def test_combination_layer_available_values_use_ui_names(tmp_data_dir, monkeypatch):
    """layer 字段的 available_values 用 UI 层名（命令层/...）——与入参词表一致，
    按值重试必须可成功（代码审查 HIGH：曾返回 registry 英文层名）。"""
    _setup(tmp_data_dir, monkeypatch)
    e = _err_of(terms=["计费"], layer="业务层", nf="UDG")  # 业务层对象 nf 均空 → 组合空
    assert e.code == gq.INVALID_FILTER_COMBINATION
    layer_values = e.details["available_values"]["layer"]
    assert layer_values  # nf=UDG 下可选的层（非空——UDG 有命令/特性层对象）
    from app.graph_query.catalog import layers
    assert all(v in layers() for v in layer_values)  # 全是可重试的合法 UI 层名
    assert "命令层" in layer_values
    # 按返回值重试确实成功
    out = search_graph_core(terms=["计费"], layer=layer_values[0], nf="UDG")
    assert out["total"] >= 1


# ---------------- 14 latest 语义 ----------------

def test_latest_default_and_explicit_old_version(tmp_data_dir, monkeypatch):
    """默认只搜最新版（旧版正文词不再命中）；显式 version 可搜旧版正文。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["N2接口配置"])
    assert out["total"] == 1  # 只有 UNC（ADD URR 旧版 N2 词在 20.15.2，非最新）
    assert _ids(out) == ["UNC@MMLCommand@SET N2MODE"]
    old = search_graph_core(terms=["N2接口配置"], version="20.15.2")
    assert "UDG@MMLCommand@ADD URR" in _ids(old)


def test_filters_applied_before_term_search(tmp_data_dir, monkeypatch):
    """filters/latest 在 term 搜索前生效（不存在先取全库候选再过滤的截断）。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["URR"], nf="UNC")
    assert out["total"] == 0  # UNC 域内无 URR——不是被截断掉


# ---------------- 15/21 分页与 facets ----------------

def test_pagination_and_facets(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["URR"], page=1, size=1)
    assert out["total"] == 2
    assert len(out["hits"]) == 1
    assert out["has_more"] is True and out["next_page"] == 2
    # facets 是分页前的精确计数，分页不改变 facets
    assert out["facets"]["types"] == {"MMLCommand": 2}
    page2 = search_graph_core(terms=["URR"], page=2, size=1)
    assert page2["has_more"] is False and page2["next_page"] is None
    assert page2["facets"] == out["facets"]


def test_facets_layers_nfs_versions(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["计费"])
    assert out["facets"]["layers"]
    assert "UDG" in out["facets"]["nfs"]
    assert "20.15.2" in out["facets"]["versions"]


def test_hit_fields_required_shape(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["免费RG"])
    hit = out["hits"][0]
    for f in ("id", "type", "layer", "name", "nf", "domain", "scenario",
              "version", "versions", "matched_terms", "matched_in",
              "snippets", "rank_reasons"):
        assert f in hit
    assert hit["layer"] == "特性层"
    assert hit["snippets"] and hit["snippets"][0]["term"] == "免费RG"
    assert "免费RG" in hit["snippets"][0]["text"]


# ---------------- 19 池上限（POOL_CAP 触顶截断，SEARCH_TOO_BROAD 退场） ----------------

def test_pool_cap_truncation_returns_bounded(populated, monkeypatch):
    """SEARCH_TOO_BROAD 退场：池触顶不再报错——正常返回 + total_is_bounded +
    截断建议。monkeypatch POOL_CAP=2 强制触顶，语义不依赖语料规模。"""
    from app.graph_query import search as search_mod
    monkeypatch.setattr(search_mod, "POOL_CAP", 2)
    out = search_graph_core(terms=["计费"])  # 种子里 ≥4 个对象元数据含"计费"
    assert out["total"] >= 1
    assert out["total_is_bounded"] is True
    assert out["diagnostics"]["term_stats"]["计费"]["capped"] is True
    assert any("截断" in s for s in out["suggestions"])


# ---------------- 22/23 零结果诊断 ----------------

def test_zero_result_recovery_use_match_any(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["计费防欺诈", "免费RG"], match="all")
    assert out["total"] == 0
    assert "USE_MATCH_ANY" in out["diagnostics"]["recovery_codes"]
    assert out["suggestions"]


def test_zero_result_recovery_remove_term(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["计费", "不存在的词xyz"], match="all")
    assert out["total"] == 0
    assert "REMOVE_OR_REPHRASE_TERM" in out["diagnostics"]["recovery_codes"]
    assert out["diagnostics"]["term_counts"]["不存在的词xyz"] == 0
    assert out["diagnostics"]["term_stats"]["不存在的词xyz"] == \
        {"hit": False, "capped": False}


def test_zero_result_recovery_relax_filters(tmp_data_dir, monkeypatch):
    """filters 合法（UDG 有对象）但无命中；去掉 filters 后有命中 → RELAX_FILTERS。"""
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["N2"], nf="UDG")
    assert out["total"] == 0
    assert "RELAX_FILTERS" in out["diagnostics"]["recovery_codes"]


def test_term_counts_present_on_success(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["计费", "免费RG"], match="any")
    assert out["diagnostics"]["term_counts"]["免费RG"] > 0
    assert out["diagnostics"]["term_stats"]["免费RG"]["hit"] is True


# ---------------- 输出契约 ----------------

def test_response_contract_fields(tmp_data_dir, monkeypatch):
    _setup(tmp_data_dir, monkeypatch)
    out = search_graph_core(terms=["URR"])
    for f in ("terms", "match", "applied_filters", "total", "total_is_bounded",
              "page", "size", "has_more", "next_page", "hits", "facets",
              "diagnostics", "suggestions"):
        assert f in out, f
    assert out["match"] == "any"
    assert out["applied_filters"] == {}
    assert isinstance(out["facets"]["layers"], dict)
    assert isinstance(out["diagnostics"]["recovery_codes"], list)


# ---------------- catalog（动态合法值目录） ----------------

def test_catalog_functions(tmp_data_dir, monkeypatch):
    from app.graph_query import catalog
    s = _setup(tmp_data_dir, monkeypatch)
    conn = s.db
    assert catalog.layers() == ["命令层", "特性层", "任务层", "业务层"]
    types_map = catalog.types(conn)
    assert types_map["MMLCommand"] == "命令层"
    assert types_map["BusinessDomain"] == "业务层"
    assert set(catalog.nfs(conn)) >= {"UDG", "UNC"}
    assert "20.15.2" in catalog.versions(conn)
    assert catalog.versions_by_nf(conn)["UDG"] == ["20.15.2", "20.16.0"]
    assert catalog.domains(conn) == ["charging-fraud"]
    assert catalog.scenarios(conn) == []
    assert catalog.scenarios_by_domain(conn) == {}


def test_catalog_nf_case_conflict_integrity_error(tmp_data_dir, monkeypatch):
    """nf 仅大小写不同的 canonical 值 → 数据完整性错误（不可任选，§12.3）。"""
    s = _setup(tmp_data_dir, monkeypatch)
    # 直插一条仅大小写不同的 nf 行（绕过写路径正常化）
    from app.repos import objects_repo
    objects_repo.upsert(
        s.db, id="udg@MMLCommand@LOWER CASE", version="20.15.2",
        type="MMLCommand", layer="Command", scope="nf", nf="udg",
        domain=None, scenario=None, source_path="x.md", name=None,
        frontmatter={"id": "udg@MMLCommand@LOWER CASE"}, body_md="x", raw_md="x",
        mtime=1.0)
    s.db.commit()
    from app.graph_query import catalog
    with pytest.raises(gq.GraphQueryError) as ei:
        catalog.nfs(s.db)
    assert ei.value.error.code == gq.INTERNAL_ERROR


# ---------------- 有界候选池（2026-09-29 超时治理 Task 9） ----------------

@pytest.fixture
def populated(tmp_data_dir, monkeypatch):
    """Task 10/11 共用：种子语料 + 全局 service 就绪。"""
    _setup(tmp_data_dir, monkeypatch)  # 该文件既有的种子辅助（各测试体内同款调用）
    from app.service import get_service
    return get_service()


def test_broad_term_returns_truncated_not_error(populated):
    """宽泛词不再 SEARCH_TOO_BROAD 报错：正常返回 + total_is_bounded。"""
    out = search_graph_core(terms=["配置"])  # 种子语料里的高频词（3 个对象正文含"配置"）
    assert out["total"] >= 1
    if out["total_is_bounded"]:
        assert out["total"] <= 10_000
        assert any("截断" in s for s in out["suggestions"])


def test_term_counts_remain_integer_compatible_with_explicit_term_stats(populated):
    out = search_graph_core(terms=["ADD URR", "不存在词xyz"])
    tc = out["diagnostics"]["term_counts"]
    stats = out["diagnostics"]["term_stats"]
    # term_counts 是既有公开契约，旧调用方仍可直接做 count > 0。
    assert tc["ADD URR"] > 0
    assert tc["不存在词xyz"] == 0
    assert stats["ADD URR"] == {"hit": True, "capped": False}
    assert stats["不存在词xyz"] == {"hit": False, "capped": False}


def test_total_is_bounded_field_present(populated):
    out = search_graph_core(terms=["ADD URR"])
    assert isinstance(out["total_is_bounded"], bool)


# ---------------- 短词两档（2026-09-29 超时治理 Task 10） ----------------

def test_two_char_term_body_like_mode(populated):
    """默认 body_like 档：两字词走 LIKE，能命中正文（沿用既有行为，回归锚）。"""
    out = search_graph_core(terms=["配额"])  # FEAT_BILLING 正文含「配额管理」
    assert any(h["id"] == "UDG@Feature@GWFD-020300" for h in out["hits"])
    assert "配额" not in out["diagnostics"].get("body_skipped_short_terms", [])


def test_two_char_term_metadata_only_mode(populated):
    """metadata_only 降级档：两字词只搜元数据（跳过正文）并写入诊断回显。"""
    svc = populated
    svc.db.execute(
        "INSERT INTO meta(key, value) VALUES('search_short_term_mode',"
        "'metadata_only') ON CONFLICT(key) DO UPDATE SET value='metadata_only'")
    svc.db.commit()
    import app.graph_query.search as s
    s._last_good_mode = "body_like"  # 强制下轮重读（当前每请求直读，防御缓存化）
    try:
        # 种子元数据（id/name/name_zh）均不含「配额」——只有正文含
        out = search_graph_core(terms=["配额"])
        assert all(h["id"] != "UDG@Feature@GWFD-020300" for h in out["hits"])
        assert "配额" in out["diagnostics"].get("body_skipped_short_terms", [])
    finally:
        svc.db.execute("DELETE FROM meta WHERE key='search_short_term_mode'")
        svc.db.commit()
        s._last_good_mode = "body_like"


def test_one_char_term_metadata_only(populated):
    """1 字符恒只搜元数据（与开关档位无关）。"""
    out = search_graph_core(terms=["配"])
    assert "配" in out["diagnostics"].get("body_skipped_short_terms", [])


# ---------------- catalog 校验缓存（2026-09-29 超时治理 Task 11） ----------------

def test_catalog_cached_until_invalidate(populated):
    from app.graph_query import catalog
    conn = populated.db
    v1 = catalog.versions(conn)
    conn.execute("INSERT INTO objects(id, version, type, layer, scope, "
                 "source_path, name, frontmatter_json, body_md, raw_md, mtime) "
                 "VALUES('zz@MMLCommand@NEW', '99.0.0', 'MMLCommand', 'Command',"
                 "'nf', 'x.md', 'NEW', '{}', '', '', 0.0)")
    conn.commit()
    assert catalog.versions(conn) == v1            # 未失效 → 命中缓存
    catalog.invalidate()
    assert "99.0.0" in catalog.versions(conn)      # 失效 → 重查
    catalog.invalidate()


def test_reload_index_invalidates_catalog(populated):
    import app.graph_query.catalog as catalog
    catalog.versions(populated.db)  # 先填缓存——证明 reload 真失效（非仅属性存在）
    assert catalog._cache
    populated.reload_index()
    assert catalog._cache == {}  # reload（写路径末尾）即失效


def test_rebuild_invalidates_catalog(populated):
    import app.graph_query.catalog as catalog
    catalog.versions(populated.db)
    populated.rebuild()
    assert catalog._cache == {}  # rebuild 锁内末尾失效


# ---------------- T10 评审 Minor（同笔提交） ----------------

def test_pool_cap_exact_boundary_not_capped(populated, monkeypatch):
    """恰好 POOL_CAP 条命中 → capped=False（LIMIT+1 探测消歧）。"""
    import app.graph_query.search as s
    monkeypatch.setattr(s, "POOL_CAP", 2)
    out = search_graph_core(terms=["在线计费"])
    # 正文恰 2 条命中（ADD URR 新版 + FEAT_BILLING），元数据另 1 条——均不触顶
    assert out["total"] == 2
    assert out["total_is_bounded"] is False
    assert out["diagnostics"]["term_stats"]["在线计费"]["capped"] is False


def test_zero_result_suggestion_mentions_skipped_short_terms(populated):
    """零结果建议回显未搜正文的短词（T10 评审 Minor：短词跳正文可能是零结果主因）。"""
    out = search_graph_core(terms=["配", "不存在的词xyz"])
    assert out["total"] == 0
    assert any("未搜正文" in s and "配" in s for s in out["suggestions"])


def test_exact_name_outside_broad_pool_is_preserved_and_ranked_first(
        tmp_data_dir, monkeypatch):
    """宽包含池触顶时，精确名称不得因插入顺序落在池外而消失。"""
    docs = {}
    for i, name in enumerate(("needle alpha", "needle beta", "needle gamma")):
        docs[f"broad-{i}.md"] = (
            "---\n"
            f"id: UDG@Feature@BROAD-{i}\n"
            "type: Feature\n"
            "version: 20.15.2\n"
            f"name: {name}\n"
            "---\n\nbody\n"
        )
    # 最后写入，保证旧实现的 LIMIT 2 任意池拿不到它。
    docs["exact.md"] = (
        "---\n"
        "id: UDG@Feature@EXACT\n"
        "type: Feature\n"
        "version: 20.15.2\n"
        "name: needle\n"
        "---\n\nbody\n"
    )
    _setup(tmp_data_dir, monkeypatch, docs)
    import app.graph_query.search as search_mod
    monkeypatch.setattr(search_mod, "POOL_CAP", 2)

    out = search_graph_core(terms=["needle"])

    assert out["hits"][0]["id"] == "UDG@Feature@EXACT"
    assert out["hits"][0]["rank_reasons"][0] == "名称精确匹配"
    assert out["total_is_bounded"] is True


def test_prefix_name_outside_contains_pool_is_preserved_and_ranked_first(
        tmp_data_dir, monkeypatch):
    """无精确项时，池外前缀项仍须优先于普通包含项。"""
    docs = {}
    for i, name in enumerate(("alpha needle", "beta needle", "gamma needle")):
        docs[f"contains-{i}.md"] = (
            "---\n"
            f"id: UDG@Feature@CONTAINS-{i}\n"
            "type: Feature\nversion: 20.15.2\n"
            f"name: {name}\n"
            "---\n\nbody\n"
        )
    docs["prefix.md"] = (
        "---\n"
        "id: UDG@Feature@PREFIX\n"
        "type: Feature\nversion: 20.15.2\n"
        "name: Needle target\n"
        "---\n\nbody\n"
    )
    _setup(tmp_data_dir, monkeypatch, docs)
    import app.graph_query.search as search_mod
    monkeypatch.setattr(search_mod, "POOL_CAP", 2)

    out = search_graph_core(terms=["NEEDLE"])

    assert out["hits"][0]["id"] == "UDG@Feature@PREFIX"
    assert out["hits"][0]["rank_reasons"][0] == "元数据前缀匹配"


@pytest.mark.parametrize(("special_name", "term", "reason"), [
    ("ＡＢＣ", "abc", "名称精确匹配"),
    ("Straße", "STRASSE", "名称精确匹配"),
    ("Ｓｔｒａße target", "strasse", "元数据前缀匹配"),
])
def test_normalized_priority_survives_pool_cap(
        special_name, term, reason, tmp_data_dir, monkeypatch):
    """Priority 沿用 NFKC+casefold，不能退化成 SQLite ASCII NOCASE。"""
    norm = "strasse" if "tra" in term.casefold() else "abc"
    docs = {
        f"broad-{i}.md": (
            "---\n"
            f"id: UDG@Feature@NORM-BROAD-{i}\n"
            "type: Feature\nversion: 20.15.2\n"
            f"name: x {norm} {i}\n"
            "---\n\nbody\n"
        )
        for i in range(3)
    }
    docs["special.md"] = (
        "---\n"
        "id: UDG@Feature@NORM-SPECIAL\n"
        "type: Feature\nversion: 20.15.2\n"
        f"name: {special_name}\n"
        "---\n\nbody\n"
    )
    _setup(tmp_data_dir, monkeypatch, docs)
    import app.graph_query.search as search_mod
    monkeypatch.setattr(search_mod, "POOL_CAP", 2)

    out = search_graph_core(terms=[term])

    assert out["hits"][0]["id"] == "UDG@Feature@NORM-SPECIAL"
    assert out["hits"][0]["rank_reasons"][0] == reason
    assert out["total_is_bounded"] is True


def test_three_plus_metadata_search_uses_fts_not_escaped_like(populated):
    """>=3 字符元数据包含必须走 trigram MATCH，避免罕见多 term 全表 LIKE。"""
    traced = []
    populated.db.set_trace_callback(traced.append)
    try:
        search_graph_core(terms=["ADD URR", "AFUSRDETECT"], match="any")
    finally:
        populated.db.set_trace_callback(None)

    selects = [sql.lower() for sql in traced if sql.lstrip().lower().startswith("select")]
    assert any("metadata_text :" in sql and " match " in sql for sql in selects)
    # priority 的行边界判断可用 LIKE，但必须由同一条 MATCH 先缩小候选；
    # 禁止退回不带 MATCH 的元数据全表 LIKE。
    metadata_like = [sql for sql in selects if "metadata_text like" in sql]
    assert metadata_like and all(" match " in sql for sql in metadata_like)


@pytest.mark.parametrize("term", ["配额", "配"])
def test_skipped_short_term_relax_probe_never_scans_body_like(
        populated, term):
    """metadata_only 两字词及恒跳的一字词，零结果诊断也不能从后门扫正文。"""
    populated.db.execute(
        "INSERT INTO meta(key, value) VALUES('search_short_term_mode', "
        "'metadata_only') ON CONFLICT(key) DO UPDATE SET value='metadata_only'")
    populated.db.commit()
    traced = []
    populated.db.set_trace_callback(traced.append)
    try:
        out = search_graph_core(terms=[term], nf="UNC")
    finally:
        populated.db.set_trace_callback(None)

    assert out["total"] == 0
    assert not any("body_text like" in sql.lower() for sql in traced)
