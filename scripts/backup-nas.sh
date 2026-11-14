#!/usr/bin/env bash
# 归档「当前正在运行的网关」：代码 + .env + 短信库一致快照
#
#   ./scripts/backup-nas.sh                 # 备份到 <项目>/backups/
#   ./scripts/backup-nas.sh /vol1/1000/docker/sms-gateway-1.1.4
#   ./scripts/backup-nas.sh <项目目录> <输出目录>
#
# 代码随时能重打包，但 .env（AUTH_PASSWORD / SECRET_KEY）和 data/sms.db 丢了就没了，
# 所以这个脚本的重点是后两样。
set -euo pipefail

SRC="${1:-$(cd "$(dirname "$0")/.." && pwd)}"
OUT_DIR="${2:-$SRC/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
NAME="sms-gateway-backup-$STAMP"

if [ ! -d "$SRC/app" ]; then
    echo "✘ $SRC 不像项目目录（找不到 app/）" >&2
    exit 1
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
DEST="$WORK/$NAME"
mkdir -p "$DEST" "$OUT_DIR"

VER="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$SRC/app/__init__.py" 2>/dev/null || echo unknown)"
FW_VER="$(sed -n 's/^VERSION = "\(.*\)"/\1/p' "$SRC/firmware/main.lua" 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- 1. 代码
# 排除虚拟环境 / 缓存 / 旧产物，避免备份包越滚越大
tar -C "$SRC" -cf - \
    --exclude='./.venv' --exclude='./venv' \
    --exclude='__pycache__' --exclude='*.pyc' \
    --exclude='./dist' --exclude='./backups' \
    --exclude='./data/*.db' --exclude='./data/*.db-wal' --exclude='./data/*.db-shm' \
    . | tar -C "$DEST" -xf -

# ---------------------------------------------------------------- 2. 配置
if [ -f "$SRC/.env" ]; then
    cp "$SRC/.env" "$DEST/.env"
    echo "✔ 已归档 .env（含密码与 SECRET_KEY，注意保管）"
else
    echo "! 没有 .env，跳过（若靠 docker-compose.yml 默认值运行则属正常）"
fi

# ---------------------------------------------------------------- 3. 数据库
# SQLite 跑在 WAL 模式，直接 cp sms.db 会丢未 checkpoint 的数据，必须用 .backup 取一致快照。
DB="$SRC/data/sms.db"
if [ -f "$DB" ]; then
    mkdir -p "$DEST/data"
    if docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'sms-gateway'; then
        # 容器里一定有 python + sqlite3，借它做备份，不依赖宿主编译的 sqlite3
        docker exec sms-gateway python -c "
import sqlite3
s = sqlite3.connect('/data/sms.db')
d = sqlite3.connect('/data/_backup_tmp.db')
s.backup(d); d.close(); s.close()
"
        docker cp sms-gateway:/data/_backup_tmp.db "$DEST/data/sms.db"
        docker exec sms-gateway rm -f /data/_backup_tmp.db
        echo "✔ 短信库已快照（容器内 python backup）"
    elif command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$DB" ".backup '$DEST/data/sms.db'"
        echo "✔ 短信库已快照（sqlite3 .backup）"
    else
        # 最后兜底：三件套一起拷，仍有极小概率丢最后几条事务
        cp "$DB" "$DEST/data/sms.db"
        [ -f "$DB-wal" ] && cp "$DB-wal" "$DEST/data/sms.db-wal"
        [ -f "$DB-shm" ] && cp "$DB-shm" "$DEST/data/sms.db-shm"
        echo "⚠ 未找到 sqlite3 且容器未运行，已直接拷贝 sms.db（可能丢最后几条未落盘事务）"
    fi
else
    echo "! 没有 data/sms.db，跳过"
fi

# ---------------------------------------------------------------- 4. 清单
cat > "$DEST/BACKUP.txt" <<EOF
打包时间   : $(date '+%Y-%m-%d %H:%M:%S %Z')
来源目录   : $SRC
应用版本   : $VER
固件版本   : $FW_VER
数据库     : $([ -f "$DEST/data/sms.db" ] && echo "已包含 data/sms.db" || echo "未包含")
配置文件   : $([ -f "$DEST/.env" ] && echo "已包含 .env" || echo "未包含")
EOF

tar -czf "$OUT_DIR/$NAME.tar.gz" -C "$WORK" "$NAME"

echo
echo "备份完成: $OUT_DIR/$NAME.tar.gz ($(du -h "$OUT_DIR/$NAME.tar.gz" | cut -f1))"
echo "还原步骤:"
echo "  tar -xzf $NAME.tar.gz && cd $NAME"
echo "  docker compose up -d --build --force-recreate"
