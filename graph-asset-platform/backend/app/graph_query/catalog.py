"""动态 filter 合法值目录（需求 §12.3）——内部服务，不新增公开工具。

source of truth：
1. objects 实际 distinct 值是 nf/version/domain/scenario/type 的主来源；
2. type→UI layer 用 objects.layer（写路径来自 registry）经 ``ui_layer_of``；
3. registry 只补充当前零实例但合法的扩展 type（不覆盖实际数据）；
4. nf 仅大小写不同却出现多个 canonical 值 → 数据完整性错误（INTERNAL_ERROR）。
"""
import sqlite3

from ..service import get_service
from ..ui_layers import UI_LAYERS, ui_layer_of
from .contracts import INTERNAL_ERROR, GraphQueryError, GraphError

# 目录值缓存（spec §5.2.5：_validate_filters 每请求最多 5 次 DISTINCT 全表扫，
# 大库下不可忽视）。键含 id(conn)——测试 monkeypatch 换连接自动失效。已知脆弱点：
# 连接被 GC 后 id 可能被新连接复用（生产单连接进程级存活，不构成实际风险——
# 此处注释留痕，出现怪异缓存命中优先查这里）。
# 写路径经 service.reload_index()/rebuild() 调 invalidate()。
_cache: dict = {}


def invalidate() -> None:
    _cache.clear()


def _memo(key, conn, fn):
    k = (key, id(conn))
    if k not in _cache:
        _cache[k] = fn()  # fn 抛错（如 nfs 完整性错误）不写入缓存，下次重查
    return _cache[k]


def layers() -> list:
    """canonical UI 层（命令层/特性层/任务层/业务层）——常量表，不用缓存。"""
    return list(UI_LAYERS)


def types(conn: sqlite3.Connection) -> dict:
    """{type: ui_layer}（objects 实际值 + registry 零实例补充）。"""
    def _load() -> dict:
        out = {r["type"]: ui_layer_of(r["layer"]) for r in conn.execute(
            "SELECT DISTINCT type, layer FROM objects").fetchall()}
        reg = get_service().registry
        for name, entry in reg._t.items():  # noqa: SLF001 registry 无公开枚举口
            if name not in out:
                out[name] = ui_layer_of(entry.get("layer"))
        return out
    return _memo("types", conn, _load)


def nfs(conn: sqlite3.Connection) -> list:
    def _load() -> list:
        rows = [r["nf"] for r in conn.execute(
            "SELECT DISTINCT nf FROM objects WHERE nf IS NOT NULL AND nf != '' "
            "ORDER BY nf").fetchall()]
        # 大小写冲突的 canonical 值 → 数据完整性错误（不可任选其一，§12.3）
        seen: dict = {}
        for nf in rows:
            key = nf.upper()
            if key in seen and seen[key] != nf:
                raise GraphQueryError(GraphError(
                    code=INTERNAL_ERROR,
                    message=(f"网元数据完整性错误：{seen[key]!r} 与 {nf!r} 仅大小写"
                             f"不同——请先修数据再搜索")))
            seen[key] = nf
        return rows
    return _memo("nfs", conn, _load)


def versions(conn: sqlite3.Connection) -> list:
    def _load() -> list:
        return [r["version"] for r in conn.execute(
            "SELECT DISTINCT version FROM objects WHERE version IS NOT NULL "
            "AND version != '' ORDER BY version").fetchall()]
    return _memo("versions", conn, _load)


def versions_by_nf(conn: sqlite3.Connection) -> dict:
    def _load() -> dict:
        out: dict = {}
        for r in conn.execute(
            "SELECT DISTINCT nf, version FROM objects "
            "WHERE nf IS NOT NULL AND nf != '' AND version IS NOT NULL "
            "AND version != '' ORDER BY nf, version"
        ).fetchall():
            out.setdefault(r["nf"], []).append(r["version"])
        return out
    return _memo("versions_by_nf", conn, _load)


def domains(conn: sqlite3.Connection) -> list:
    def _load() -> list:
        return [r["domain"] for r in conn.execute(
            "SELECT DISTINCT domain FROM objects WHERE domain IS NOT NULL "
            "AND domain != '' ORDER BY domain").fetchall()]
    return _memo("domains", conn, _load)


def scenarios(conn: sqlite3.Connection) -> list:
    def _load() -> list:
        return [r["scenario"] for r in conn.execute(
            "SELECT DISTINCT scenario FROM objects WHERE scenario IS NOT NULL "
            "AND scenario != '' ORDER BY scenario").fetchall()]
    return _memo("scenarios", conn, _load)


def scenarios_by_domain(conn: sqlite3.Connection) -> dict:
    def _load() -> dict:
        out: dict = {}
        for r in conn.execute(
            "SELECT DISTINCT domain, scenario FROM objects "
            "WHERE domain IS NOT NULL AND domain != '' "
            "AND scenario IS NOT NULL AND scenario != '' ORDER BY domain, scenario"
        ).fetchall():
            out.setdefault(r["domain"], []).append(r["scenario"])
        return out
    return _memo("scenarios_by_domain", conn, _load)
