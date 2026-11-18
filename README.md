# 短信网关 · Air780EPM on 飞牛 NAS

![version](https://img.shields.io/badge/app-1.1.9-2ea44f) ![firmware](https://img.shields.io/badge/firmware-1.1.1-blue) ![python](https://img.shields.io/badge/python-3.12-3776ab) ![license](https://img.shields.io/badge/license-MIT-green) ![tests](https://img.shields.io/badge/tests-8%20suites-blue)

把插在 NAS 上的合宙 **Air780EPM**（4G Cat.1）变成一个私有短信网关：Web 界面收发短信、按规则自动转发到 Webhook / 群机器人 / 邮件 / 另一张卡，数据全部留在自己的机器上。

> ⚠️ **Air780EPM 没有 AT 短信固件**，只有 LuatOS 二次开发。必须先给模块烧录
> [`firmware/main.lua`](firmware/main.lua) 桥接脚本，短信能力由模块侧 Lua 调用 `sms` 库实现。
> 这一步是硬前提，跳过则网关只能显示「固件没有短信能力」。

## 架构

```mermaid
flowchart LR
    M["📱 Air780EPM<br/>LuatOS 桥接脚本"] <-->|"USB CDC<br/>文本行 + JSON"| G["⚙️ FastAPI 网关<br/>Docker"]
    G <-->|读写| D[("SQLite WAL")]
    G <-->|"WebSocket"| W["🖥️ Web 界面"]
    G -->|转发规则| F["Webhook / 机器人<br/>SMTP / 短信"]
    M <-->|4G| N["运营商短信网"]
```

后端 FastAPI + SQLite(WAL) + pyserial，前端零构建（原生 JS + hash 路由），单容器部署。
模块侧与上位机之间是自研的轻量桥接协议：`PING`/`STATUS`/`SEND`/`SETPIN` ↔ `+PONG`/`+STATUS`/`+SMS`/`+SENT`/`+BOOT`。

## 功能

| 模块 | 说明 |
|---|---|
| 短信收发 | PDU 编解码，中文、emoji、长短信自动分片与合并 |
| 会话视图 | 按号码聚合、未读计数、搜索、手动标记已读 |
| 转发规则 | 所有 / 包含 / 正则 / 号码匹配 → Webhook、邮件、短信转发 |
| 实时推送 | WebSocket 推新短信与模块状态，无需刷新 |
| 多设备 | 按 USB 设备级身份分组，插几台认几台，热插拔自愈 |
| 模块面板 | 信号、运营商、ICCID、IMEI、在线状态、AT 调试 |
| 认证 | 可选访问密码（Bearer Token，30 天有效） |
| 无硬件模式 | `MODEM_MOCK=1` 用假数据跑通界面和转发，先试后装 |

## 快速开始

```bash
# 1. 初始化（生成 .env 含随机密码、建 data/）
./scripts/install.sh

# 2. 给模块烧录桥接脚本 —— 一次性，见 docs/烧录指南.md

# 3. 启动
docker compose up -d --build
docker compose logs -f
```

看到 `[IMEI] 已连接 /dev/ttyACM0 信号 xx（xx%）中国联通` 就成功了，
浏览器打开 `http://NAS的IP:8080`。详细步骤（含自检、PIN 卡、反向代理）见
[部署指南](docs/部署-飞牛NAS.md)。

**没插模块想先看界面**：`.env` 里设 `MODEM_MOCK=1`，重建容器即可，会带几条示例短信。

## 文档

| 文档 | 内容 |
|---|---|
| [部署 · 飞牛 NAS](docs/部署-飞牛NAS.md) | 硬件接线、安装、自检、启动验证、常见问题、反向代理 |
| [烧录指南](docs/烧录指南.md) | Luatools 建工程、BOOT 模式、固件版本选择、排错、协议速查 |
| [转发规则](docs/转发规则.md) | 匹配方式、模板占位符、各服务配置、errcode 排障 |
| [配置项参考](docs/配置项参考.md) | 全部环境变量、构建参数、pip 源 |
| [多设备](docs/多设备.md) | 分组策略、接口变化、数据库迁移、已知限制 |
| [实测记录](docs/实测记录.md) | 端到端真机验证过程与结论 |
| [CHANGELOG](CHANGELOG.md) | 版本变更 |
| [CONTRIBUTING](CONTRIBUTING.md) | 本地开发与测试 |

## 项目结构

```
sms-gateway/
├── app/                 # FastAPI 后端
│   ├── pdu.py           #   PDU 编解码（GSM7/UCS2/UDH/时区）
│   ├── modem.py         #   AT 驱动 + 端口探测
│   ├── luatos.py        #   LuatOS 桥接驱动
│   ├── device.py        #   多设备发现与身份分组
│   ├── service.py       #   同步、分片聚合、事件广播
│   ├── forwarder.py     #   Webhook / SMTP / 短信转发
│   ├── db.py / api.py / main.py
│   └── static/          #   零构建前端
├── firmware/main.lua    # 烧进模块的 LuatOS 桥接脚本（必刷）
├── scripts/             # install / preflight / probe_* / backup-nas / upgrade
├── docs/                # 专题文档（见上表）
├── tests/               # 8 个自测套件，纯 Python，不依赖硬件
├── Dockerfile
└── docker-compose.yml
```

## 测试与开发

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
MODEM_MOCK=1 .venv/Scripts/python.exe -m uvicorn app.main:app --port 8080

for t in pdu luatos phone operator multi pin device forwarder; do
    .venv/Scripts/python.exe -m tests.test_$t
done
```

8 个套件覆盖 PDU 编解码、桥接协议、号码归一化、运营商识别、多设备、SIM PIN、
设备发现、转发通道模板与错误判定。CI 在每次 push 自动跑全套。

## 升级与备份

换版本目录有个坑：compose 数据卷是相对路径 `./data:/data`，新目录直接 `up` 会得到空库。别手动拷，用脚本：

```bash
./scripts/upgrade.sh --dry-run    # 先看计划
./scripts/upgrade.sh              # 执行：备份 → 复用 .env → data 软链 → 健康检查 → 失败自动回滚
```

日常备份用 `./scripts/backup-nas.sh`，会把代码 + `.env` + 数据库一致快照打包
（SQLite 是 WAL 模式，脚本走 `sqlite3.backup()` 而不是裸 `cp`）。

## 注意

- 短信内容与链接来自外部，界面已做转义；转发到自建服务时请自行校验。
- 密码、SMTP 授权码存在本地 `data/sms.db`，别把整个目录传到公网。
- 想从外网访问，建议飞牛 NAS 配反向代理 + HTTPS，不要裸奔端口。
- `.env` 与 `data/` 不在版本控制内，也**不在发布包内**，升级时务必保留。

## 许可证

[MIT](LICENSE) © 2026 caddaric9527
