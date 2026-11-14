#!/bin/sh
# Air780EPM 短信网关 · 一键初始化（飞牛 NAS / 任何 Linux + Docker）
# 只做三件事：生成 .env、建好 data 目录、打印下一步。不启动容器。
set -eu

cd "$(dirname "$0")/.."
PROJECT_DIR="$(pwd)"

echo "==> 项目目录: $PROJECT_DIR"

# 1. 环境检查
if ! command -v docker >/dev/null 2>&1; then
    echo "[FAIL] 没找到 docker，请先在飞牛 NAS 的『应用中心』安装 Docker"
    exit 1
fi
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    echo "[FAIL] 没找到 docker compose 插件"
    exit 1
fi
echo "[ OK ] $COMPOSE"

rand_hex() {
    if command -v openssl >/dev/null 2>&1; then
        openssl rand -hex "$1"
    else
        head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'
    fi
}

# 2. 生成 .env（已存在则不覆盖，避免把用户的密码冲掉）
if [ -f .env ]; then
    echo "[ OK ] .env 已存在，保留不覆盖"
else
    PASSWORD="$(rand_hex 6)"
    SECRET="$(rand_hex 24)"
    # 用 sed 从模板生成，避免覆盖式写入丢掉模板里的注释
    sed -e "s|^AUTH_PASSWORD=.*|AUTH_PASSWORD=${PASSWORD}|" \
        -e "s|^SECRET_KEY=.*|SECRET_KEY=${SECRET}|" \
        .env.example > .env
    chmod 600 .env 2>/dev/null || true
    echo "[ OK ] 已生成 .env，登录密码：${PASSWORD}"
    echo "       （改密码就编辑 .env 里的 AUTH_PASSWORD，然后 docker compose up -d 重建）"
fi

# 3. 数据目录
mkdir -p data
echo "[ OK ] 数据目录: $PROJECT_DIR/data"

# 4. 提示看到的串口
if ls /dev/ttyACM* >/dev/null 2>&1; then
    echo "[ OK ] 检测到串口设备:"
    ls /dev/ttyACM* 2>/dev/null | sed 's/^/       /'
else
    echo "[WARN] 当前没有 /dev/ttyACM*，模块插上后再执行一次自检"
fi

cat <<EOF

下一步：
  1) 构建镜像（首次约 2-5 分钟，走清华 pip 源）：
       $COMPOSE build
  2) 自检（确认容器能看到模块、桥接脚本在线）：
       $COMPOSE run --rm --entrypoint python sms-gateway scripts/preflight.py
  3) 启动：
       $COMPOSE up -d
  4) 看日志：
       $COMPOSE logs -f
  5) 浏览器打开 http://<NAS_IP>:8080

详细说明见 docs/部署-飞牛NAS.md
EOF
