# 参与开发

## 本地环境

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Windows
# .venv/bin/pip install -r requirements.txt      # Linux / macOS

cp .env.example .env
```

不接硬件也能开发，设 `MODEM_MOCK=1` 即可：

```bash
# Windows
MODEM_MOCK=1 .venv/Scripts/python.exe -m uvicorn app.main:app --port 8080 --reload
# Linux / macOS
MODEM_MOCK=1 .venv/bin/python -m uvicorn app.main:app --port 8080 --reload
```

浏览器打开 `http://127.0.0.1:8080`。`MODEM_MOCK_COUNT=2` 可模拟两台设备，用来调多设备界面。

## 测试

项目没用 pytest，测试是 8 个自包含的 Python 模块，用标准库断言 + `check()` 辅助函数：

```bash
for t in pdu luatos phone operator multi pin device forwarder; do
    python -m tests.test_$t
done
```

测试不接触真实串口，用假串口对象；数据库用 `tempfile` 建的临时库，跑完自动清理。
CI 在每次 push / PR 上跑全套，失败即拦住合并。

**改动原则**：

- 修 bug 时先补一条能复现该 bug 的断言，再改实现，避免回归。
- PDU / 协议解析的改动必须跑 `test_pdu` 和 `test_luatos`。
- 转发模板的改动必须跑 `test_forwarder`（含 JSON 转义、errcode、Content-Type 优先级）。
- 多设备分组的改动必须跑 `test_device` 和 `test_multi`。

## 版本与固件

**应用版本**（`app/__init__.py`、`app/main.py`、`docker-compose.yml` 的 `TAG`）与
**固件版本**（`firmware/main.lua` 顶部 `VERSION`）是两条独立的线，不要一起改。

| 改了什么 | 需要升版本吗 |
|---|---|
| Python 后端 / 前端 / compose | 升应用版本，更新 `CHANGELOG.md` |
| `firmware/main.lua` 桥接协议 | 升固件版本，且用户必须重刷模块 |
| 只改文档 / 测试 | 不用升版本 |

改固件协议时要同步检查 `app/luatos.py` 的解析逻辑，两端的指令清单见
[烧录指南 - 协议速查](docs/烧录指南.md)。

## 提交规范

commit message 用中文一句话概括，正文写「为什么」而不是「改了哪行」。
一次提交只做一件事，不要混进无关的格式调整。

提交前确认：

- [ ] 8 个测试套件全绿
- [ ] 没有把 `.env`、`data/*.db`、`dist/`、`.venv/` 加进提交
- [ ] 文档里出现的真实手机号 / ICCID / IMSI 已替换成示例值
- [ ] 配置文件里没有硬编码的密钥或密码

## 打包

发布包只含可再生的源码与文档，**刻意不含 `.env` 和 `data/`**：

```bash
VER=1.1.8
PKG="dist/sms-gateway-$VER"
rm -rf "$PKG" && mkdir -p "$PKG/scripts"

cp -r app firmware docs tests "$PKG/"
cp scripts/*.sh scripts/*.py "$PKG/scripts/"
cp Dockerfile docker-compose.yml requirements.txt README.md LICENSE \
   CHANGELOG.md CONTRIBUTING.md .env.example .dockerignore .gitignore "$PKG/"

rm -rf "$PKG"/{app,tests,scripts}/__pycache__
chmod +x "$PKG"/scripts/*
tar -czf "dist/sms-gateway-$VER-stable-$(date +%Y%m%d).tar.gz" -C dist "sms-gateway-$VER"
sha256sum "dist/sms-gateway-$VER-stable-$(date +%Y%m%d).tar.gz"
```

> 打包脚本目前是手工命令，如果你经常发版，欢迎提 PR 把它固化成 `scripts/package.sh`。
