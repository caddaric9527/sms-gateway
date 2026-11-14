# 短信网关 · Air780EPM on 飞牛 NAS

把插在 NAS 上的合宙 **Air780EPM**（4G Cat.1）变成一个短信收发 + 自动转发的私有网关。
Web 界面管会话、发短信、配转发规则，短信可推送到 Webhook / 邮件 / 另一张手机卡。

## 功能

| 模块 | 说明 |
|---|---|
| 短信收发 | PDU 模式直连，中文、emoji、长短信（自动分片/合并）全部支持 |
| 会话视图 | 按号码聚合、未读计数、搜索、手动标记已读 |
| 转发规则 | 所有 / 包含 / 正则 / 号码 匹配 → Webhook、邮件、短信转发 |
| 实时推送 | WebSocket 推送新短信和模块状态，无需刷新 |
| 模块面板 | 信号、运营商、ICCID、IMEI、在线状态，支持 AT 指令调试 |
| 认证 | 可选访问密码（Bearer Token，30 天有效） |
| 无硬件模式 | `MODEM_MOCK=1` 用假数据跑通界面和转发，方便先试后装 |

技术栈：FastAPI + SQLite + pyserial；前端零构建（原生 JS），单镜像约 120MB。

---

## 一、部署（飞牛 NAS）

> **先看这条**：Air780EPM **只支持 LuatOS 二次开发，没有 AT 固件**（合宙官方明确说明）。
> 出厂固件跑的是 airlink 演示脚本，`AT+HELP` 只会列出 14 条命令，**没有任何短信指令**。
> 所以必须先给模块烧录 `firmware/main.lua`（见 1.6），短信能力由模块侧 Lua 调用 `sms` 库实现，
> 网关通过 USB 虚拟串口用「文本行 + JSON」协议与它对话。

> **一条命令走完全程**（推荐，细节见 [`docs/部署-飞牛NAS.md`](docs/部署-飞牛NAS.md)）：
>
> ```bash
> tar -xzf sms-gateway-1.1.4.tar.gz && cd sms-gateway
> ./scripts/install.sh                      # 生成 .env（含随机密码）、建 data/
> docker compose build                      # 走清华 pip 源
> docker compose run --rm --entrypoint python sms-gateway scripts/preflight.py   # 自检
> docker compose up -d
> ```

### 1. 确认模块被识别

Air780EPM 在系统里会枚举出 **3 个串口**（VID:PID = `19D1:0001`），分别是 AT/数据口、日志口、调试口。
用脚本自动识别最快。

**Linux / NAS：**

```bash
lsusb | grep -i 19d1         # 应该看到 19D1:0001
ls -l /dev/ttyUSB* /dev/ttyACM*

cd /vol1/1000/docker/sms-gateway      # 换成你放项目的目录
sudo python3 scripts/probe_at.py      # 找 AT 口（看型号/IMEI）
sudo python3 scripts/probe_bridge.py  # 找桥接脚本（刷过脚本后用这个）
```

**Windows（本地先验证）：**

```powershell
.venv\Scripts\python.exe scripts\probe_at_win.py
.venv\Scripts\python.exe scripts\probe_bridge.py
```

输出类似：

```
  ✔ AT 口   /dev/ttyUSB0    Air780EPM | +CPIN: READY
建议在 .env 中设置：MODEM_PORT=/dev/ttyUSB0
```

> 如果提示权限不足：`sudo usermod -aG dialout $USER` 后重新登录。
> 提示"无 AT 应答"的口一般是日志口，忽略即可。
> 服务里 `MODEM_PORT=auto` 也会自动逐个口试，只是启动时多花几秒。

### 1.5 插卡与天线

模块**必须插入有效的 SIM 卡（Nano 卡位）并接好天线**才能收发短信。
没插卡时界面上会显示「SIM 卡未插入，请插卡后重连」。

**关于电信卡**：合宙官方说明——移动/联通卡直接可用；**电信卡必须烧录带 CC(VoLTE) 支持的固件版本，
且卡本身已开通 VoLTE**。实测电信卡（IMSI 46011）在本项目使用的 106 号固件下可正常识别与收发。

### 1.6 给模块烧录桥接脚本（Air780EPM 必做，一次性）

**为什么不能跳过**：Air780EPM 没有 AT 短信指令，短信能力必须由模块侧 Lua 调用 `sms` 库实现。
不烧脚本，网关只能显示「固件没有短信能力」。

**完整图文步骤见 [`docs/烧录指南.md`](docs/烧录指南.md)**，要点：

1. 工具 **Luatools** ≥ 3.0.15：<https://docs.openluat.com/common/Luatools/>
2. 固件 <https://docs.openluat.com/air780epm/luatos/firmware/780epm_version/>
   - **必须选 106 号固件**。合宙明确支持**电信卡**短信的只有 106 / 109 / 110 / 111 号，
     本机是电信卡（IMSI 46011），109~111 是其它机型专用，所以选 **106 号**。
   - 推荐 **V2056 的 106 号**（修了 UTF-8→UCS2 中文乱码）。
3. **进 BOOT 下载模式**：按住 `BOOT` 键的同时按一下 `RESET`（未通电则按住 BOOT 再按 `PWR` 开机）。
   成功标志：设备管理器里该设备**只剩 1 个串口**（平时是 3~4 个）。
4. Luatools → 右上「项目管理测试」→ 新建工程 → 选 `.soc` → 添加 `firmware/main.lua`（放根目录）
   → 勾 **「添加默认 lib」** + **「添加底层」** → 点 **「下载底层和脚本」**。
5. 烧完复位，运行 `.venv\Scripts\python.exe scripts\probe_bridge.py` 验证：

```
  ✔ COM7           桥接脚本在线
      信号 CSQ=22  注册状态=1  SIM=已就绪
建议在 .env 中设置：MODEM_PORT=COM7
```

看到 `桥接脚本在线` 就说明模块侧已就绪，网关 `MODEM_PROTOCOL=auto` 会自动认出它。

> 桥接协议（调试用）：上位机发 `PING` / `STATUS` / `SEND {"id":1,"num":"+86...","txt":"..."}`，
> 模块回 `+PONG` / `+STATUS {...}` / `+SENT {...}`，收到短信主动推 `+SMS {...}`。

### 2. 配置

```bash
cp .env.example .env
vi .env
```

至少改两项：

```ini
MODEM_PORT=/dev/ttyUSB0     # 上一步探测到的口
AUTH_PASSWORD=你的访问密码   # 留空=不登录，强烈建议设置
```

### 3. 启动

```bash
docker compose up -d --build
docker compose logs -f
```

看到 `网关启动，端口=/dev/ttyUSB0 mock=False` 即可。
浏览器打开 `http://NAS的IP:8080`。

### 4. 没插模块先看看界面

```ini
MODEM_MOCK=1
```

重建容器后界面会带几条示例短信，转发规则也能正常试跑。

---

## 二、转发规则

界面 → 转发 → 新建规则。一条规则 = 匹配条件 + 若干目标。

### 匹配方式

| 方式 | 例子 | 说明 |
|---|---|---|
| 所有短信 | — | 全量转发 |
| 内容包含 | `验证码` | 简单关键字，忽略大小写 |
| 正则匹配 | `(验证码\|动态码)[^\d]{0,10}(\d{4,8})` | 提取验证码场景常用 |
| 号码包含 | `1069,95588` | 多个用英文逗号分隔 |

### 目标

**Webhook**（对接 n8n / Node-RED / 自己的服务）

```json
{"type": "webhook", "url": "http://192.168.1.10:5678/webhook/sms", "enabled": true}
```

默认发送的 JSON：

```json
{
  "from": "+8613800138000",
  "text": "【某银行】您的验证码为 394827",
  "timestamp": "2026-11-11T07:09:32+08:00",
  "device": "air780epm",
  "rule": "验证码转发",
  "message_id": 128
}
```

需要自定义格式时填 Body 模板（JSON 字符串，支持占位符 `{from}` `{text}` `{time}` `{device}` `{rule}` `{id}`）：

```json
{"msgtype":"text","text":{"content":"收到 {from} 的短信：{text}"}}
```

上面这个模板可以直接喂给企业微信/钉钉机器人的 Webhook 地址。

**邮件**：先在 设置 → SMTP 填服务器、账号、授权码，规则里选「邮件」填收件人即可。

**短信转发**：把收到的短信用本模块转发到另一个手机号（适合老式设备告警）。

---

## 三、配置项

全部通过环境变量（`.env`）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MODEM_PORT` | `/dev/ttyUSB0` | 串口，用 probe_at.py / probe_bridge.py 探测 |
| `MODEM_PROTOCOL` | `auto` | `auto`=自动探测 / `at`=标准 AT 固件 / `luatos`=模块跑了 `firmware/main.lua` |
| `MODEM_MOCK` | `0` | `1`=模拟模式 |
| `SMS_STORAGE` | `SM` | `SM`=SIM 卡 / `ME`=模块存储。不支持 SM 时自动回退 ME |
| `KEEP_ON_MODEM` | `0` | `0`=入库后从 SIM 删除（推荐，SIM 卡容易满） |
| `POLL_INTERVAL` | `20` | 全量扫描间隔（秒）；有新短信 URC 时会立即同步，实际延迟约 5s |
| `AUTH_PASSWORD` | 空 | 访问密码，空=不登录 |
| `WEB_PORT` | `8080` | 宿主机映射端口 |
| `TZ` | `Asia/Shanghai` | 时区，影响短信时间显示 |

改完 `.env` 后 `docker compose up -d` 生效。

---

## 四、排查

| 现象 | 原因与处理 |
|---|---|
| 界面显示「未连接」 | 端口不对：`docker compose logs` 里搜 `无 AT 响应`，重跑 probe_at.py；或 `.env` 的设备和 compose 的 `devices` 不一致 |
| 显示「SIM 卡未插入」 | 没插卡，或卡座接触不良。`AT+CIMI` 回 `+CME ERROR: 10` 即确认 |
| 信号 `0,99` / 一直未注册 | 天线没接或所处位置无 4G 覆盖。先接天线，等 10~30 秒再刷新状态 |
| 发送报 `+CMS ERROR: 500` | 短信中心未配置或号码格式问题。先用 AT 调试发 `AT+CSCA?` 看有没有号，没有的话 `AT+CSCA="+8613800250500"`（移动）后重试 |
| 发送报 `+CMS ERROR: 515` | 模块还在搜网/未注册。看状态栏是否「已注册」 |
| 界面提示「当前固件不支持短信 AT 命令」 | 模块跑的还不是桥接脚本。按 1.6 烧录 `firmware/main.lua`，然后用 `scripts/probe_bridge.py` 确认出现 `+PONG` |
| `AT+HELP` 只有十几条命令 | 那是合宙 airlink 演示脚本注册的精简 AT 集，**不含任何短信命令**。桥接脚本刷好后这条路就不需要了 |
| 桥接脚本刷了但网关连不上 | 串口被别家工具占用（Luatools/串口助手），关掉后重试；或模块没重启，拔插一次 USB |
| 收不到短信 | 检查 `+CPIN` 是否 READY、信号是否 >0；在 AT 调试里发 `AT+CMGL=4` 看模块本身有没有短信 |
| 中文乱码 | 正常不会。若出现，把「AT 调试」的输出发出来，是 PDU 解析问题 |
| 只能收到 20 条就走不动 | SIM 卡满了。确认 `KEEP_ON_MODEM=0`，或手动 `AT+CMGDA="DEL ALL"` |
| compose 报 `no such file /dev/ttyUSB0` | 设备没插或名字变了。用 `/dev/serial/by-id/xxx` 这种稳定路径填 `MODEM_PORT` |

AT 调试入口在「设置」页底部，可直发 `AT+CSQ`、`AT+COPS?`、`AT+CREG?` 等。

---

## 五、项目结构

```
sms-gateway/
├── app/
│   ├── pdu.py         # PDU 编解码（GSM7/UCS2/长短信 UDH/时区）
│   ├── modem.py       # AT 串口驱动 + 端口自动探测 + 协议探测 + Mock
│   ├── luatos.py      # LuatOS 桥接驱动（文本行 + JSON 协议）
│   ├── service.py     # 轮询同步、分片聚合、发送、事件广播
│   ├── forwarder.py   # Webhook / SMTP / 短信 三种转发通道
│   ├── db.py          # SQLite（短信、规则、设置、日志）
│   ├── api.py         # REST + WebSocket
│   ├── main.py        # FastAPI 入口
│   └── static/        # 前端（index.html / app.js / style.css）
├── firmware/main.lua    # 烧到模块上的 LuatOS 桥接脚本（Air780EPM 必刷）
├── scripts/
│   ├── probe_at.py      # NAS 上探测 AT 口（纯标准库，无需 pip）
│   ├── probe_at_win.py  # Windows 上探测（需要 pyserial）
│   ├── probe_bridge.py  # 探测桥接脚本是否在线（双平台）
│   └── backup-nas.sh    # 完整备份：代码 + .env + 数据库一致快照
├── tests/
│   ├── test_pdu.py      # PDU 自测，23 项
│   └── test_luatos.py   # 桥接驱动自测，20 项（假串口）
├── Dockerfile
└── docker-compose.yml
```

### 备份与还原

代码随时能重新打包，**`.env` 和 `data/sms.db` 丢了就没了**。在项目目录执行：

```bash
./scripts/backup-nas.sh                    # 生成 backups/sms-gateway-backup-<时间戳>.tar.gz
./scripts/backup-nas.sh /vol1/1000/docker/sms-gateway-1.1.4   # 指定源目录
```

它会打包代码 + `.env`（含 `AUTH_PASSWORD`/`SECRET_KEY`）+ 数据库，并附一份
`BACKUP.txt` 记录版本与来源。数据库走 `sqlite3 .backup`（容器在跑就用容器内的
python 做），不是裸 `cp` —— 网关是 WAL 模式，直接拷 `.db` 会丢最后几条未落盘事务。

还原：

```bash
tar -xzf backups/sms-gateway-backup-<时间戳>.tar.gz
cd sms-gateway-backup-<时间戳>
docker compose up -d --build --force-recreate
```

> `--force-recreate` 不能省：只 `restart` 不会重新读取 `privileged` 等容器配置。

本地开发（不用 Docker）：

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
MODEM_MOCK=1 DB_PATH=./data/sms.db .venv/Scripts/python -m uvicorn app.main:app --port 8080
python -m tests.test_pdu        # PDU 自测
python -m tests.test_luatos     # 桥接驱动自测
```

---

## 六、注意

- 短信里的链接和内容来自外部，界面已做转义；转发到 Webhook 时请自行校验。
- 密码、SMTP 授权码存在本地 SQLite（`data/sms.db`），别把整个目录传到公网。
- 想从外网访问，建议直接在飞牛 NAS 上配反向代理 + HTTPS，不要裸奔端口。
