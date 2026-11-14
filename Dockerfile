# syntax=docker/dockerfile:1
# Air780EPM 短信网关
# 国内网络：pip 默认走阿里云，且源不通会自动退到腾讯云/官方源。
# 手动指定：docker compose build --build-arg PIP_INDEX=https://pypi.org/simple
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    DB_PATH=/data/sms.db

# python:slim 默认不带 zoneinfo，缺了它 TZ=Asia/Shanghai 会被当作 UTC，
# 短信时间戳会整体差 8 小时。
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# pip 源：默认阿里云。构建时按 PIP_INDEX -> PIP_FALLBACK_INDEXES 顺序逐个重试，
# 某一家不通（表现为 "from versions: none"）会自动换下一家，不用手工改文件。
#   docker compose build --build-arg PIP_INDEX=https://pypi.org/simple
ARG PIP_INDEX=https://mirrors.aliyun.com/pypi/simple
ARG PIP_FALLBACK_INDEXES="https://mirrors.cloud.tencent.com/pypi/simple https://pypi.org/simple"

COPY requirements.txt .
RUN set -eu; \
    ok=0; \
    for idx in "${PIP_INDEX}" ${PIP_FALLBACK_INDEXES}; do \
        echo "==> 尝试 pip 源: ${idx}"; \
        if pip install --no-cache-dir --timeout 30 --retries 3 --index-url "${idx}" -r requirements.txt; then \
            ok=1; break; \
        fi; \
        echo "!! 该源不可用，换下一个: ${idx}"; \
    done; \
    if [ "${ok}" != "1" ]; then \
        echo "================================================================"; \
        echo "所有 pip 源都取不到包，排查方向："; \
        echo "  1) 先确认构建容器能不能出网（能出网才谈得上换源）："; \
        echo "     docker run --rm python:3.12-slim python -m pip download --no-deps -d /tmp x"; \
        echo "  2) 指定一个确定能通的源重试："; \
        echo "     docker compose build --build-arg PIP_INDEX=https://pypi.org/simple"; \
        echo "  3) 需要认证代理时，在 docker-compose.yml 的 build.args 里加 HTTPS_PROXY"; \
        echo "================================================================"; \
        exit 1; \
    fi

COPY app ./app
# 排障脚本一起打进去，容器内可直接跑 python scripts/preflight.py
COPY scripts ./scripts

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=6s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
