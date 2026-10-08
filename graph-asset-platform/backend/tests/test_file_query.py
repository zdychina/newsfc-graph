"""search_files_core 单元测试（spec §4.3：find/ls 语义 + 游标全量 + 封顶计数）。"""
import pytest

from app.file_query import search_files_core
from app.graph_query.contracts import GraphQueryError

MD = ("---\nid: UDG@MMLCommand@ADD URR\ntype: MMLCommand\nnf: UDG\n"
      "version: 20.15.2\nname: ADD URR\n---\nbody\n")


@pytest.fixture
def populated(tmp_data_dir, monkeypatch):
    import app.service as svc_mod
    from app.repos import files_repo
    s = _bare_service(tmp_data_dir)  # 文件底部定义（下划线开头防误收集为测试）
    monkeypatch.setattr(svc_mod, "_service", s)
    s.store.write("Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md", MD)
    s.store.write("Command/UDG/20.16.0/UDG@MMLCommand@ADD URR.md",
                  MD.replace("20.15.2", "20.16.0"))
    s.store.write("Feature/UDG/F1/概述.md",
                  "---\nid: UDG@Feature@F1\ntype: Feature\nnf: UDG\n---\nf")
    s.store.write_bytes("Feature/UDG/F1/assets/x.png", b"\x89PNG")
    s.reindex_path("Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md")
    s.reindex_path("Command/UDG/20.16.0/UDG@MMLCommand@ADD URR.md")
    s.reindex_path("Feature/UDG/F1/概述.md")
    files_repo.rebuild_all(s.db, s.store)
    return s


def _bare_service(tmp_data_dir):
    import app.service as svc_mod
    from app.store import Store
    import app.db as dbmod
    from app.registry import Registry
    from app.index import Index
    s = svc_mod.Service.__new__(svc_mod.Service)
    s.store = Store(tmp_data_dir)
    s.db = dbmod.get_db(tmp_data_dir.parent / "t.db")
    dbmod.init_schema(s.db)
    s.registry = Registry.load_default()
    s.index = Index.load_from_db(s.db, s.registry)
    s.files_building = False
    return s


def test_query_matches_filename_normalized(populated):
    out = search_files_core(query="add urr")
    assert out["total"] == 2
    assert "Command/UDG/20.15.2/UDG@MMLCommand@ADD URR.md" in \
        [f["path"] for f in out["files"]]


def test_query_returns_obj_id_and_version(populated):
    out = search_files_core(query="ADD URR", ext="md")
    by_ver = {f["version"]: f for f in out["files"]}
    assert by_ver["20.15.2"]["obj_id"] == "UDG@MMLCommand@ADD URR"
    # 旧版本目录文件回带自己的 version（get_md 精确读该文件内容的前提）


def test_two_char_query_like_path(populated):
    """2 字符走 LIKE 路径（spec D6 修订：前缀短语实证否决；trigram 索引对
    <3 字符与 ESCAPE 均不生效——语义正确，性能见 file_query 模块 docstring）。"""
    out = search_files_core(query="概述")
    assert [f["path"] for f in out["files"]] == ["Feature/UDG/F1/概述.md"]


def test_path_direct_children_ls_semantics(populated):
    out = search_files_core(path="Command/UDG")
    assert {f["path"]: f["is_dir"] for f in out["files"]} == {
        "Command/UDG/20.15.2": True, "Command/UDG/20.16.0": True}


def test_path_filter_normalizes_windows_separator(populated):
    out = search_files_core(path=r"Command\UDG")
    assert {f["path"] for f in out["files"]} == {
        "Command/UDG/20.15.2", "Command/UDG/20.16.0"}


def test_path_recursive_files_only(populated):
    out = search_files_core(path="Feature/UDG", recursive=True)
    assert [f["path"] for f in out["files"]] == [
        "Feature/UDG/F1/assets/x.png", "Feature/UDG/F1/概述.md"]  # 字典序+仅文件


def test_cursor_full_traversal(populated):
    seen, after = [], None
    for _ in range(20):
        out = search_files_core(ext="md", limit=1, after=after)
        seen.extend(f["path"] for f in out["files"])
        if not out["has_more"]:
            break
        after = out["next_cursor"]
    assert len(seen) == 3 and len(set(seen)) == 3  # 全量无重复无遗漏


def test_query_matches_dirname_too(populated):
    out = search_files_core(query="F1")
    assert [(f["name"], f["is_dir"]) for f in out["files"]] == [("F1", True)]


def test_short_query_rejected(populated):
    with pytest.raises(GraphQueryError) as ei:
        search_files_core(query="a")
    assert ei.value.error.code == "INVALID_ARGUMENT"


def test_no_filter_rejected(populated):
    with pytest.raises(GraphQueryError) as ei:
        search_files_core()
    assert ei.value.error.code == "INVALID_ARGUMENT"


def test_bad_path_structured_error(populated):
    with pytest.raises(GraphQueryError) as ei:
        search_files_core(path="NoSuchDir")
    assert ei.value.error.code == "INVALID_FILTER"
    assert ei.value.error.details.get("field") == "path"


def test_missing_path_while_catalog_building_is_retryable(populated):
    """首启扫描未走到合法目录时不能谎报非法筛选，调用方应收到可重试语义。"""
    populated.files_building = True
    try:
        with pytest.raises(GraphQueryError) as ei:
            search_files_core(path="Command/NotScannedYet")
    finally:
        populated.files_building = False
    assert ei.value.error.code == "INDEX_REBUILDING"
    assert ei.value.error.retryable is True


def test_glob_metachar_dir(populated):
    populated.store.write("Command/weird[1]/child.md", "c")
    populated.store.write("Command/weird1/sibling.md", "s")
    from app.repos import files_repo
    files_repo.rebuild_all(populated.db, populated.store)
    out = search_files_core(path="Command/weird[1]", recursive=True)
    assert [f["path"] for f in out["files"]] == ["Command/weird[1]/child.md"]
    # 未转义时 GLOB 'weird[1]/*' 会匹配 weird1/ → sibling 混入；转义后精确


def test_index_building_flag(populated):
    populated.files_building = True
    try:
        out = search_files_core(query="ADD URR")
        assert out["index_building"] is True
    finally:
        populated.files_building = False


def test_after_beyond_end_returns_empty(populated):
    """after 越过结果集末端：空页、total=0（剩余口径）、无游标。"""
    out = search_files_core(ext="md", after="zzzz")
    assert out["files"] == [] and out["total"] == 0
    assert out["has_more"] is False and out["next_cursor"] is None


def test_total_is_bounded_when_over_cap(populated, monkeypatch):
    """计数封顶：超 cap 时 total 钳在 cap、total_is_bounded=True（页不受影响）。"""
    import app.file_query as fq
    monkeypatch.setattr(fq, "TOTAL_CAP", 2)
    out = search_files_core(ext="md")  # 3 个 md
    assert out["total"] == 2 and out["total_is_bounded"] is True
    assert len(out["files"]) == 3  # 页大小与计数封顶解耦


def test_query_mode_cursor_full_traversal(populated):
    """query 模式游标翻页：全量遍历无重复无遗漏（MATCH 与 path>after 叠加）。"""
    seen, after = [], None
    for _ in range(20):
        out = search_files_core(query="ADD URR", limit=1, after=after)
        seen.extend(f["path"] for f in out["files"])
        if not out["has_more"]:
            break
        after = out["next_cursor"]
    assert len(seen) == 2 and len(set(seen)) == 2
