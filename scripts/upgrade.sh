#!/usr/bin/env bash
# 把正在运行的网关升级到「当前目录里的这个版本」，带备份 + 健康检查 + 自动回滚。
#
#   cd sms-gateway-1.1.7
#   ./scripts/upgrade.sh              # 升级并保留旧目录（可回滚）
#   ./scripts/upgrade.sh --no-backup  # 跳过备份（不建议，除非刚备份过）
#   ./scripts/upgrade.sh --dry-run    # 只打印要做什么，不改任何东西
#
# 关键点：docker-compose.yml 里数据卷是相对路径 `./data:/data`，换目录等于换空库。
# 所以新目录的 data 会做成指向旧 data 的软链接 —— 两个版本共用同一份数据，
# 升级失败回滚时，升级期间新收到的短信也不会丢。
set -euo pipefail

SELF_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="sms-gateway"
BACKUP=1
DRY=0

for arg in "$@"; do
    case "$arg" in
        --no-backup) BACKUP=0 ;;
        --dry-run)   DRY=1 ;;
        -h|--help)   sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "未知参数: $arg（-h 看用法）" >&2; exit 2 ;;
    esac
done

info() { printf '\033[36m›\033[0m %s\n' "$*"; }
ok()   { printf '\033[32m✔\033[0m %s\n' "$*"; }
warn() { printf '\033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✘\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY" = 1 ]; then printf '   [dry-run] %s\n' "$*"; else eval "$@"; fi; }

# ---------------------------------------------------------------- 0. 环境检查
[ -f "$SELF_DIR/app/__init__.py" ] || die "$SELF_DIR 不像项目目录（找不到 app/__init__.py）"
[ -f "$SELF_DIR/docker-compose.yml" ] || die "$SELF_DIR 缺少 docker-compose.yml"
command -v docker >/dev/null 2>&1 || die "找不到 docker 命令"
docker compose version >/dev/null 2>&1 || die "需要 docker compose v2（docker-compose 老版本不支持本配置）"

NEW_VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SELF_DIR/app/__init__.py")"
NEW_VER="${NEW_VER:-unknown}"

# ---------------------------------------------------------------- 1. 找当前在跑什么
running_dir() {
    docker inspect "$CONTAINER" >/dev/null 2>&1 || return 1
    local dir
    dir="$(docker inspect "$CONTAINER" \
        --format '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null || true)"
    case "$dir" in
        ""|"<no value>") return 1 ;;
        *) printf '%s\n' "$dir" ;;
    esac
}

OLD_DIR="$(running_dir || true)"
OLD_VER=""
if [ -n "$OLD_DIR" ] && [ -f "$OLD_DIR/app/__init__.py" ]; then
    OLD_VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$OLD_DIR/app/__init__.py")"
fi

info "当前目录（新版本）: $SELF_DIR  版本 $NEW_VER"
if [ -n "$OLD_DIR" ]; then
    info "正在运行的项目目录: $OLD_DIR  版本 ${OLD_VER:-未知}"
else
    warn "没有探测到正在运行的 $CONTAINER 容器（可能已停），按首次部署处理"
fi

if [ -n "$OLD_DIR" ] && [ "$OLD_DIR" = "$SELF_DIR" ]; then
    MODE="inplace"
    info "升级方式: 原地升级（新旧目录相同）"
elif [ -n "$OLD_DIR" ]; then
    MODE="switch"
    info "升级方式: 换目录升级（旧目录保留，可随时回滚）"
else
    MODE="fresh"
    info "升级方式: 首次部署"
fi

if [ "$MODE" = "switch" ] && [ "$NEW_VER" != "unknown" ] && [ "$NEW_VER" = "${OLD_VER:-}" ]; then
    warn "新旧版本号相同（都是 $NEW_VER），确认你解压的是新包吗？"
fi

# ---------------------------------------------------------------- 2. 端口
read_port() {
    local dir="$1" port=""
    if [ -f "$dir/.env" ]; then
        port="$(sed -n 's/^WEB_PORT=//p' "$dir/.env" | tail -1 | tr -d '"' | tr -d '[:space:]')"
    fi
    printf '%s\n' "${port:-8080}"
}
PORT="$(read_port "${OLD_DIR:-$SELF_DIR}")"
info "健康检查端口: $PORT"

# ---------------------------------------------------------------- 3. 备份
if [ "$BACKUP" = 1 ] && [ -n "$OLD_DIR" ] && [ -f "$OLD_DIR/app/__init__.py" ]; then
    info "备份当前运行版本（代码 + .env + 数据库一致快照）…"
    if [ "$DRY" = 1 ]; then
        printf '   [dry-run] %s/scripts/backup-nas.sh %s\n' "$OLD_DIR" "$OLD_DIR"
    elif [ -x "$OLD_DIR/scripts/backup-nas.sh" ]; then
        ( cd "$OLD_DIR" && ./scripts/backup-nas.sh "$OLD_DIR" "$OLD_DIR/backups" ) \
            || die "备份失败，已中止升级（先解决备份问题再升）"
    else
        warn "旧目录没有 backup-nas.sh（1.1.4 才有），跳过自动备份 —— 建议手动 cp 一份 .env 和 data/"
    fi
fi

# ---------------------------------------------------------------- 4. 准备配置与数据
if [ "$MODE" = "switch" ]; then
    # 4a. .env
    if [ -f "$OLD_DIR/.env" ] && [ ! -f "$SELF_DIR/.env" ]; then
        info "复用旧 .env（保住 AUTH_PASSWORD / SECRET_KEY / SIM_PIN）"
        run cp -p "$OLD_DIR/.env" "$SELF_DIR/.env"
    elif [ -f "$SELF_DIR/.env" ]; then
        ok "新目录已有 .env，保留不动"
    else
        warn "旧目录也没有 .env —— 首次启动会自动生成（登录密码见容器日志）"
    fi

    # 4b. data —— 软链接共享，避免两份数据分叉
    if [ -e "$SELF_DIR/data" ]; then
        if [ -L "$SELF_DIR/data" ]; then
            ok "data 已是软链接 → $(readlink "$SELF_DIR/data")"
        else
            warn "新目录已存在真实 data/，不覆盖；如需共享旧库请手动处理"
        fi
    elif [ -d "$OLD_DIR/data" ]; then
        info "data 软链接到旧目录（两版本共用同一份库，回滚不丢数据）"
        run ln -s "$OLD_DIR/data" "$SELF_DIR/data"
    else
        warn "旧目录没有 data/，将新建空库"
        run mkdir -p "$SELF_DIR/data"
    fi
fi

# ---------------------------------------------------------------- 5. 起新版本
info "构建并启动 $NEW_VER …"
run "cd '$SELF_DIR' && docker compose up -d --build --force-recreate"

# ---------------------------------------------------------------- 6. 健康检查
health() {
    local url="http://127.0.0.1:${PORT}/healthz" i
    local retries="${HEALTH_RETRIES:-30}"
    for i in $(seq 1 "$retries"); do
        if command -v curl >/dev/null 2>&1; then
            curl -fsS --max-time 2 "$url" >/dev/null 2>&1 && return 0
        elif command -v wget >/dev/null 2>&1; then
            wget -q -O /dev/null --timeout=2 "$url" 2>/dev/null && return 0
        else
            docker exec "$CONTAINER" python -c \
                "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=2)" \
                >/dev/null 2>&1 && return 0
        fi
        sleep 2
    done
    return 1
}

if [ "$DRY" = 1 ]; then
    echo
    ok "[dry-run] 检查通过，实际未做任何改动。去掉 --dry-run 即可执行。"
    exit 0
fi

info "等待服务就绪（最多 $(( ${HEALTH_RETRIES:-30} * 2 )) 秒）…"
if health; then
    ok "服务已就绪: http://<NAS_IP>:$PORT"
else
    warn "健康检查未通过，最近日志："
    docker logs --tail=40 "$CONTAINER" 2>&1 || true
    echo

    if [ "$MODE" = "switch" ]; then
        warn "回滚到 $OLD_DIR（${OLD_VER:-旧版本}）…"
        if ( cd "$OLD_DIR" && docker compose up -d --force-recreate --no-build ); then
            if health; then
                ok "已回滚，服务恢复运行。新版本的问题请把上面的日志发出来。"
            else
                die "回滚后仍不健康 —— 请手动执行: cd $OLD_DIR && docker compose logs --tail=80"
            fi
            exit 1
        fi
        die "回滚失败，请手动执行: cd $OLD_DIR && docker compose up -d --force-recreate --no-build"
    fi
    die "启动失败，且当前是原地升级无法自动回滚。可从 $OLD_DIR/backups 里的备份还原。"
fi

# ---------------------------------------------------------------- 7. 结果
echo
ok "升级完成: ${OLD_VER:-?} → $NEW_VER"
docker ps --filter "name=$CONTAINER" --format '   容器 {{.Names}}  {{.Status}}  镜像 {{.Image}}'
echo
info "确认设备已连上："
docker logs --tail=30 "$CONTAINER" 2>&1 | grep -E "已连接|掉线|未发现|连接失败" || warn "日志里暂时没有设备状态行，稍等再看"

if [ "$MODE" = "switch" ]; then
    echo
    warn "data 目前是软链接，指向 $OLD_DIR/data"
    echo "   稳定运行几天、确定不回滚之后，把数据实体化（可选）："
    echo "     cd $SELF_DIR && rm data && mv '$OLD_DIR/data' ./data"
    echo "   确认不再需要回滚时，旧目录可以整个删掉，但请先把 data 实体化。"
fi
