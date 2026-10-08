#!/bin/bash
# 图谱平台增量同步部署（参考 AgenticKB pack/apply 模式，适配纯 docker run）。
#
# 用法：
#   bash deploy/sync.sh pack                    # 外网：构建前端 + 打增量包（backend/app + frontend/dist）
#   bash deploy/sync.sh apply <gap-sync-*.tar.gz>  # 内网：校验、原子换代码、重建容器（代码外挂）
#   bash deploy/sync.sh restart                 # 内网：仅重建容器（不动代码/数据）
#   bash deploy/sync.sh logs                    # 内网：看容器日志
#
# 模式：**代码外挂**——backend/ 与 frontend/dist 挂载进容器（镜像只当运行时：
# python3.11 + pip 依赖）。日常升级 = pack 传包 apply，**不再导 tar 镜像**。
# 仅当依赖清单（pyproject.toml / package.json）变化时才需要重新导全量镜像——
# pack/apply 会自动比对并提醒。
#
# 三条铁律：
#   1. 永不触碰 deploy/platform-data/（全部数据）
#   2. 代码替换走 stage 原子换目录（.stage → mv），中断不留半截
#   3. 依赖变更必须提醒走全量镜像（apply 检测 manifest 基线）

set -Eeuo pipefail

# Git Bash（MSYS）路径改写自防护（外网 pack 侧需要；Linux 无此变量，无害）
export MSYS_NO_PATHCONV=1
if [ -n "${MSYSTEM:-}" ] || [ -n "${MSYS:-}" ]; then
    export PATH="/usr/bin:$PATH"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# 布局自适应（2026-09-04 内网实际：sync.sh 直接放在 graph-asset-platform/ 下，
# platform-data/backend/frontend 与之同级，无 deploy/ 子目录；仓库布局则在 deploy/ 里）：
#   上一级有 backend/ → 脚本在 <root>/deploy/（仓库布局）
#   本级有 backend/ 或 platform-data/ → 脚本在 <root>/（内网扁平布局）
if [ -d "$SCRIPT_DIR/../backend" ] || [ -d "$SCRIPT_DIR/../platform-data" ]; then
    ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
elif [ -d "$SCRIPT_DIR/backend" ] || [ -d "$SCRIPT_DIR/platform-data" ]; then
    ROOT="$SCRIPT_DIR"
else
    ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"      # 兜底：按仓库布局
fi
cd "$ROOT"
# 数据目录：优先 GAP_DATA_DIR 显式指定 → deploy/platform-data（仓库布局）→ platform-data（扁平布局）
if [ -n "${GAP_DATA_DIR:-}" ]; then
    DATA_DIR="$GAP_DATA_DIR"
elif [ -d "$ROOT/deploy/platform-data" ]; then
    DATA_DIR="$ROOT/deploy/platform-data"
else
    DATA_DIR="$ROOT/platform-data"
fi

IMAGE_NAME="${IMAGE_NAME:-graph-asset-platform:latest}"
CONTAINER_NAME="${CONTAINER_NAME:-gap}"
HOST_PORT="${GAP_PORT:-80}"                   # 宿主机端口（默认 80；本机测试用 GAP_PORT=18000）
NETWORK_NAME="${GAP_NETWORK:-gap-net}"        # 自定义网桥（默认 bridge 在内网机 -p 不生效，2026-09-04）
MANIFEST_BASELINE_DIR="$ROOT/.gap-sync-last"  # 依赖清单基线（apply 侧维护，gitignore）
STAGE_DIR=""
SWAP_ACTIVE=false

die() { echo "错误：$*" >&2; exit 1; }

cleanup() {
    if [ "$SWAP_ACTIVE" = true ]; then
        echo "=== 正在回滚未完成的代码切换 ===" >&2
        [ -d backend/app.old ] && { rm -rf backend/app; mv backend/app.old backend/app; } || true
        [ -d frontend/dist.old ] && { rm -rf frontend/dist; mv frontend/dist.old frontend/dist; } || true
        SWAP_ACTIVE=false
    fi
    [ -n "$STAGE_DIR" ] && [ -d "$STAGE_DIR" ] && rm -rf "$STAGE_DIR"
}
trap cleanup EXIT

# ── pack：外网执行 ──────────────────────────────────────────
cmd_pack() {
    echo "=== [1/3] 构建前端（npm run build）==="
    command -v npm >/dev/null 2>&1 || die "外网机需有 npm（node 环境）"
    [ -d frontend/src ] || die "缺少 frontend/src"
    (cd frontend && npm run build)
    [ -d frontend/dist ] || die "前端构建失败：dist 不存在"

    echo "=== [2/3] 打增量包（backend/app + frontend/dist + 依赖清单）==="
    STAGE_DIR="$(mktemp -d "$ROOT/.gap-sync-stage.XXXXXX")"
    mkdir -p "$STAGE_DIR/backend" "$STAGE_DIR/frontend"
    cp -a backend/app "$STAGE_DIR/backend/app"
    cp -a frontend/dist "$STAGE_DIR/frontend/dist"
    cp -f backend/pyproject.toml "$STAGE_DIR/pyproject.toml" 2>/dev/null || true
    cp -f frontend/package.json "$STAGE_DIR/package.json" 2>/dev/null || true

    local out ts
    ts="$(date +%Y%m%d-%H%M)"
    out="$ROOT/gap-sync-$ts.tar.gz"
    tar -czf "$out" -C "$STAGE_DIR" .
    # sha256 只记文件名（不记打包机绝对路径——内网 sha256sum -c 才能对上）
    (cd "$ROOT" && sha256sum "${out##*/}" > "${out}.sha256")

    echo "=== [3/3] 完成 ==="
    /usr/bin/ls -lh "$out" "$out.sha256" 2>/dev/null || ls -lh "$out" "$out.sha256"
    echo "把 ${out##*/} 和 .sha256 两个文件一起传内网，然后在 graph-asset-platform/ 下执行："
    echo "  bash deploy/sync.sh apply $out"
}

# ── 依赖清单基线（2026-09-04 改：首次部署豁免，后续仅 warn 不阻断）──
check_manifests() {
    # 包内 pyproject.toml / package.json 与内网基线比对：
    # - 首次部署（无基线）：直接落基线不告警；
    # - 后续部署有基线 + 包内无 manifest（运维侧未挂载源码目录常见）：跳过；
    # - 有基线 + 包内有 manifest + 不一致：⚠ warn（提醒人评估是否重导镜像）但不 exit。
    local m
    if [ ! -d "$MANIFEST_BASELINE_DIR" ]; then
        mkdir -p "$MANIFEST_BASELINE_DIR"
        for m in pyproject.toml package.json; do
            [ -f "$STAGE_DIR/$m" ] && cp -f "$STAGE_DIR/$m" "$MANIFEST_BASELINE_DIR/$m"
        done
        return 0
    fi
    local have_baseline=0
    for m in pyproject.toml package.json; do
        [ -f "$MANIFEST_BASELINE_DIR/$m" ] && have_baseline=1 && break
    done
    if [ $have_baseline -eq 0 ]; then
        for m in pyproject.toml package.json; do
            [ -f "$STAGE_DIR/$m" ] && cp -f "$STAGE_DIR/$m" "$MANIFEST_BASELINE_DIR/$m"
        done
        return 0
    fi

    local changed=()
    local have_pkg=0
    for m in pyproject.toml package.json; do
        [ -f "$STAGE_DIR/$m" ] && have_pkg=1
        if [ -f "$MANIFEST_BASELINE_DIR/$m" ] && [ -f "$STAGE_DIR/$m" ] \
            && ! diff -q "$MANIFEST_BASELINE_DIR/$m" "$STAGE_DIR/$m" >/dev/null 2>&1; then
            changed+=("$m")
        fi
    done
    if [ ${#changed[@]} -gt 0 ]; then
        echo "⚠ 警告：依赖清单与上次不同（${changed[*]}）——pip/npm 依赖可能已变化。" >&2
        echo "  本脚本只同步代码；若新功能依赖新包，请在外网重新导全量镜像（docker save）并 docker load。" >&2
    fi
    [ $have_pkg -eq 0 ] && return 0
    for m in pyproject.toml package.json; do
        [ -f "$STAGE_DIR/$m" ] && cp -f "$STAGE_DIR/$m" "$MANIFEST_BASELINE_DIR/$m"
    done
}

# ── apply：内网执行 ─────────────────────────────────────────
cmd_apply() {
    local pkg="${1:-}"
    [ -n "$pkg" ] || die "用法：bash deploy/sync.sh apply <gap-sync-*.tar.gz>"
    # 相对路径先转绝对（ROOT cd 之后原相对路径会失效——内网"包不存在"的根因）
    [ -f "$pkg" ] && pkg="$(cd "$(dirname "$pkg")" && pwd)/$(basename "$pkg")"
    [ -f "$pkg" ] || die "包不存在：$pkg（注意 cd 到 graph-asset-platform/ 再执行，或传绝对路径）"
    [ -f "$pkg.sha256" ] || die "缺少校验文件：$pkg.sha256（两个文件要一起传）"

    echo "=== [1/4] 校验包完整性 ==="
    (cd "$(dirname "$pkg")" && sha256sum -c "$(basename "$pkg").sha256") \
        || die "校验失败，包可能传输损坏，请重传"

    echo "=== [2/4] 解包并原子替换代码（数据目录不动）==="
    STAGE_DIR="$(mktemp -d "$ROOT/.gap-sync-stage.XXXXXX")"
    tar -xzf "$pkg" -C "$STAGE_DIR"
    [ -d "$STAGE_DIR/backend/app" ] || die "包内缺少 backend/app（非法包）"
    [ -d "$STAGE_DIR/frontend/dist" ] || die "包内缺少 frontend/dist（非法包）"

    mkdir -p backend frontend
    SWAP_ACTIVE=true
    # 首次部署（内网无 backend/ frontend/ 子目录）：mkdir -p 创建的是空目录，
    # 直接 mv 会把源移进"空目录名"里——frontend/dist 移进 frontend/ 变成
    # frontend/frontend/dist/... 嵌套错位。先清空目标子目录（不动同级其它文件），
    # 旧文件改名备份可选。
    rm -rf backend/app.old frontend/dist.old
    [ -d backend/app ] && [ -n "$(ls -A backend/app 2>/dev/null)" ] && mv backend/app backend/app.old
    [ -d frontend/dist ] && [ -n "$(ls -A frontend/dist 2>/dev/null)" ] && mv frontend/dist frontend/dist.old
    rm -rf backend/app frontend/dist 2>/dev/null || true
    mv "$STAGE_DIR/backend/app" backend/app
    mv "$STAGE_DIR/frontend/dist" frontend/dist
    rm -rf backend/app.old frontend/dist.old
    SWAP_ACTIVE=false
    check_manifests

    echo "=== [3/4] 重建容器 ==="
    start_container

    echo "=== [4/4] 完成 ==="
    echo "访问：http://<服务器IP>:$HOST_PORT/  （代码已外挂：以后改码 apply 即生效）"
}

# ── 容器生命周期（代码外挂 + 数据外挂；容器内固定 8000，映射 $HOST_PORT）──
start_container() {
    docker image inspect "$IMAGE_NAME" >/dev/null 2>&1 \
        || die "本地没有镜像 $IMAGE_NAME——首次部署请先 docker load 全量镜像 tar"
    mkdir -p "$DATA_DIR"

    # ⚠ 内网那台机器 docker 默认 bridge 端口转发失效（PortBindings 有值但
    # NetworkSettings.Ports 空、宿主机不监听）——必须走自定义网桥才落地。
    docker network inspect "$NETWORK_NAME" >/dev/null 2>&1 \
        || docker network create "$NETWORK_NAME" >/dev/null
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    docker run -d --name "$CONTAINER_NAME" --restart unless-stopped \
        --network "$NETWORK_NAME" \
        -p "$HOST_PORT:8000" \
        -e GAP_DATA_DIR=/data \
        -v "$DATA_DIR:/data" \
        -v "$ROOT/backend:/gap/backend" \
        -v "$ROOT/frontend/dist:/gap/frontend/dist" \
        "$IMAGE_NAME" >/dev/null

    echo "等待启动…"
    local deadline=$((SECONDS + 60))
    until docker logs "$CONTAINER_NAME" 2>&1 | grep -q "Uvicorn running"; do
        if [ "$SECONDS" -ge "$deadline" ]; then
            docker logs --tail 40 "$CONTAINER_NAME" >&2 || true
            die "60 秒内未就绪（看上方日志定位）"
        fi
        sleep 2
    done

    # 验证端口映射真实生效（此前内网踩过：配置有值但 daemon 未落地）
    if ! docker port "$CONTAINER_NAME" | grep -q "8000/tcp"; then
        docker logs --tail 30 "$CONTAINER_NAME" >&2 || true
        die "端口映射未生效（docker port 为空）——试试 systemctl restart docker 后再 bash deploy/sync.sh restart"
    fi
    docker port "$CONTAINER_NAME"
    docker logs --tail 5 "$CONTAINER_NAME"
}

cmd_restart() {
    echo "=== 重建容器（代码/数据不动）==="
    start_container
}

cmd_logs() {
    docker logs -f --tail 50 "$CONTAINER_NAME"
}

case "${1:-}" in
    pack) cmd_pack ;;
    apply) shift; cmd_apply "$@" ;;
    restart) cmd_restart ;;
    logs) cmd_logs ;;
    *)
        sed -n '2,12p' "$0"
        exit 1
        ;;
esac
