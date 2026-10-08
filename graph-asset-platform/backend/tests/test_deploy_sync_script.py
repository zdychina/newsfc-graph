"""部署脚本的关键安全顺序回归。"""

from pathlib import Path


SYNC_SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "sync.sh"


def _function_body(source: str, name: str, next_marker: str) -> str:
    start = source.index(f"{name}() {{")
    end = source.index(next_marker, start)
    return source[start:end]


def test_apply_compares_manifest_before_overwriting_baseline():
    """旧基线必须先参与比较；先复制新清单会把真实依赖变化伪装成“无变化”。

    运行环境没有可用的 Bash（Windows bash.exe 指向未安装发行版的 WSL），因此
    这里锁定最小但直接的行为前提：cmd_apply 在调用 check_manifests 之前不得写
    MANIFEST_BASELINE_DIR。check_manifests 自身负责“先 diff、再更新基线”。
    """
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    apply_body = _function_body(source, "cmd_apply", "\n# ── 容器生命周期")

    assert "check_manifests" in apply_body
    before_check, _after_check = apply_body.split("check_manifests", 1)
    baseline_copy = 'cp -f "$STAGE_DIR/$m" "$MANIFEST_BASELINE_DIR/$m"'
    assert baseline_copy not in before_check


def test_empty_existing_baseline_is_initialized_from_current_package():
    """基线目录存在但为空时也必须落首份清单，不能永久跳过依赖检测。"""
    source = SYNC_SCRIPT.read_text(encoding="utf-8")
    check_body = _function_body(
        source, "check_manifests", "\n# ── apply：内网执行")
    assert '[ $have_baseline -eq 0 ] && return 0' not in check_body
    assert 'if [ $have_baseline -eq 0 ]; then' in check_body
    assert 'cp -f "$STAGE_DIR/$m" "$MANIFEST_BASELINE_DIR/$m"' in check_body
