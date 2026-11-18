# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格，版本语义为 `主.次.修订`。
应用版本与固件版本独立编号，互不绑定。

---

## [1.1.8] - 2026-11-18

升级后浏览器继续用旧脚本导致界面卡死的修复。固件仍为 **1.1.1**，数据库结构未变。

### 修复

- **升级后页面一直显示「等待模块…」**。静态资源原先只带 `Last-Modified`/`ETag`，
  浏览器会做启发式强缓存：`index.html` 已经是新版，`app.js` 却还是老缓存。
  新旧混用后老脚本找不到已删除的 DOM 节点（`Cannot set properties of null`），
  异常又被状态刷新的 `try/catch` 吞掉，于是设备栏永远停在静态占位符，且没有任何提示。
  现在静态资源统一带 `Cache-Control: no-cache`，每次回源校验：
  内容没变走 304（几十字节），变了立刻生效，升级后无需手动硬刷新。

### 改进

- **状态拉取失败不再静默**：`/api/status` 出错时设备栏显示「状态获取失败 + 原因」，
  30 秒后自动重试（原先只在控制台报错，用户侧看着像模块没插好）。

---

## [1.1.7] - 2026-11-18

双模块真机上线后暴露的分组缺陷修复。固件仍为 **1.1.1**，无需重刷，数据库结构未变。

### 修复

- **两块同型号模块会被认成一台（或一台被拆成多台）**。Air780EPM 的 USB iSerial
  出厂固定为 `000000000001`，两块板子完全一样，`/dev/serial/by-id` 会因重名互相覆盖
  （udev 只保留后枚举的那台，实测先插的那块三个口全丢），sysfs 拓扑在容器里又可能被裁剪。
  任一分组键失效都会让「一台模块的 3 个串口」变成 3 台设备，或「两台模块」挤进同一组只认出一台。
- **改为探测时顺带读回 IMEI，按 IMEI 收敛**：同一 IMEI 的多个口合并成一台，
  不同 IMEI 即使分组键撞车也拆成两台。读不到 IMEI 的老固件仍按拓扑键去重（行为不变）。
- **注册表键改用 IMEI**：重启、换 USB 口之后设备身份保持稳定，不再随拓扑键漂移。
- **端口漂移自愈改按 IMEI 重对齐**：`_remap_ports` 重新探测后按 IMEI 匹配，
  已被占用的口会因 EBUSY 探测失败而被跳过，不会误改没漂移的设备。

### 变更

- `luatos.probe_bridge_info()` 新增（探测 + 读 IMEI），`probe_bridge()` 抽出公共逻辑保持原速。
- `FoundDevice` 增加 `imei` 字段。
- 设备发现日志里的标识由拓扑键改为 IMEI，一眼能看出哪台在哪个口。

### 说明

- 新增 3 项回归：同 IMEI 多口收敛、同组不同 IMEI 拆分、同 IMEI 重复应答只留一个口。
- 本次不涉及前端，界面与 1.1.6 完全一致。

---

## [1.1.6] - 2026-11-16

多设备第二步：界面能看全所有模块，发信可以指定用哪台。固件仍为 **1.1.1**，无需重刷。

### 新增

- **多设备状态栏**：左侧栏由单卡改为设备列表，每台一行显示号码 / 运营商 / 端口 / 在线状态 + 信号格。
  点任意一台即切换「发信设备」，高亮并写入 `localStorage`，刷新页面后保持；设备掉线自动回退主设备。
- **发信可选设备**：会话输入框下方「从 xx 发送」下拉（多设备时才出现）；`新短信` 弹窗同样可选。
  请求体带 `device`，复用 `POST /api/messages` 已有的设备路由。
- **设置页设备卡**：每台一块，IMEI / 号码 / ICCID / 型号与固件分列，
  重连与同步按设备执行（`/api/modem/reconnect?device=`、`/api/modem/resync?device=`）。
- **消息气泡标来源设备**：多设备时才显示，单机不制造噪音。
- 顶部副标题改为 `在线数/总数 · 主设备运营商 信号`。

### 说明

- 纯前端改动（`app/static/app.js`、`index.html`、`style.css`），后端接口未变，旧前端仍可单独使用。
- 单设备时观感与 1.1.5 一致：下拉自动隐藏，状态栏仍是一台。
- 会话列表仍按号码跨设备聚合（同一人给两张卡发消息会合并），留待下一步。

---

## [1.1.5] - 2026-11-15

当前稳定版，已在飞牛 NAS 实装验证。固件仍为 **1.1.1**，无需重刷。

### 修复

- **转发模板占位符全部失效**：`_render` 用 `str.format`，JSON 模板自身的花括号被当成占位符语法，
  抛 `KeyError` 后整条模板原样返回，推出去是字面的 `{text}`。影响企业微信 / 钉钉 / Telegram / Bark
  等所有 JSON 模板方案。改为只替换已知占位符。
- **群机器人出错被判为发送成功**：企业微信 / 钉钉失败时 HTTP 仍是 200，错误只在 body 的 `errcode` 里。
  原来只判状态码，key 填错、频率超限、内容超长在界面上都显示成功。现在解析 `errcode`
  并给中文提示（93000 / 45009 / 40058）。
- **短信正文含引号或换行时 JSON 被打散**：`你的"验证码"是\n123456` 这类常见正文会让模板变成非法 JSON，
  退回 `text/plain` 后企业微信报 40058 —— 转发静默失败。现在渲染失败会先对值做 JSON 转义再重试，
  同时保证 `{"id":{id}}` 这类数字占位符不被误加引号。
- JSON **数组**模板原来被当成纯文本发（`isinstance(body, dict)` 漏了 list）。
- 用户显式指定的 `Content-Type` 不再被覆盖。
- SMTP 主题模板含 `{text}` 时换行压成一行，否则 `smtplib` 抛 `Header values may not contain linefeed`。

### 新增

- `tests/test_forwarder.py`：28 项，覆盖模板渲染、转义、errcode、Content-Type 优先级、SMTP 主题。
- `scripts/upgrade.sh`：换版本升级一键完成。自动探测当前运行目录、先备份、复用 `.env`、
  `data` 做软链接共享、健康检查失败自动回滚；支持原地升级与 `--dry-run`。
- `docs/转发规则.md`、`docs/配置项参考.md`：从 README 拆出的专题文档。
- GitHub Actions CI：每次 push 自动跑 8 个测试套件。

---

## [1.1.4] - 2026-11-13

### 修复

- **`[Errno 1] Operation not permitted` 打不开串口**：这不是文件权限问题（权限位不足是 `Errno 13`），
  而是内核 device cgroup 没放行。原 compose 靠 `device_cgroup_rules: ["c 166:* rwm"]`，
  但部分引擎 / Compose 版本会**静默忽略**该字段 —— 不报错也不生效，节点看得见却 `open()` 失败。
  改为默认 `privileged: true`（`device_cgroup_rules` 注释保留供收窄权限）。
  **升级必须 `up -d --force-recreate`**，`restart` 不会重读容器配置。
- **容器内设备身份漂移**：容器内 sysfs 被裁剪，`/sys/class/tty/X/device` 解析不出 USB 拓扑，
  分组键退化成端口路径，一台模块的 3 个 CDC 口被当成 3 台设备，ttyACM 号一跳变身份就跟着变。
  改为优先读 `/dev/serial/by-id`，剥掉 `-ifNN` 后缀得到设备级键；按 inode 比对端口，
  符号链接与硬链接都能命中。三级回退：by-id → sysfs → LOCATION。
- `app/modem.py::port_error_hint()`：按 errno 给直接指引（`1`→容器 privileged、
  `13`→dialout 组、`16`→被 ModemManager 占用），避免朝错误方向排查权限。

### 新增

- `scripts/backup-nas.sh`：完整备份代码 + `.env` + 数据库。数据库走 `sqlite3.backup()` 一致快照
  （WAL 模式下裸 `cp` 会丢最后几条未落盘事务），容器在跑时借容器内 python 执行。
- `MANIFEST.txt` 随发布包提供，记录版本、内容清单与还原步骤。

---

## [1.1.3] - 2026-11-13

### 修复

- Linux 分组键取到接口级路径 `1-1.1` / `1-1.3`，导致一台模块被拆成多台。
  新增 `app/device.py::_linux_usb_path()` 取 USB **设备级**路径 `1-1`
  （realpath `/sys/class/tty/X/device`，路径切分必须同时按 `[\\/]` 处理 Windows 分隔符）。
- 探测一轮不应答就过早退回 AT：改为重试 3 轮（间隔 2 秒），应答判定放宽到
  `+PONG` / `+READY` / `+STATUS` / `+BOOT` / `+HB`。
- 热插拔后 ttyACM 号漂移不自愈：`_remap_ports()` 连续 3 次同步失败后按拓扑键重扫。
- `preflight.py` 在网关运行时误报「固件没刷」：区分端口占用，退出码 3。
- Dockerfile 的 pip 换源：`fastapi==0.115.6 (from versions: none)` 时按
  阿里云 → 腾讯云 → 官方自动重试。

---

## [1.1.2] / [1.1.1]

- **1.1.2**：Dockerfile 增加 `ARG PIP_INDEX` + `PIP_FALLBACK_INDEXES` 多源回退。
- **1.1.1**：SIM 卡 PIN 支持（`SETPIN` 桥接指令、`SIM_PIN` 环境变量、状态里带 pin 字段），
  固件同步升至 1.1.1。

---

## [1.1.0] 及更早

端到端真机联调与多设备后端核心，期间修复的关键问题：

| 问题 | 修复 |
|---|---|
| 模块反复 `Lua VM exit!! reboot in 15000ms` | `main.lua` 末尾补 `sys.run()` —— 缺它 VM 会直接退出 |
| 打开 CDC 后立刻写 PING，首帧丢失 | 延时 + 清空输入缓冲 + PING 重试 3 次；并行探测把启动从 20s 压到 10s |
| 同一号码拆成两条会话（`13300000000` vs `+8613300000000`） | `app/phone.py::normalize_peer()`，带 `+` 的号码不做猜测 |
| 运营商显示 `—` | `app/operator.py::operator_from_imsi()` 从 IMSI 反推 |
| 多设备迁移建索引报 `no such column: device_id` | 索引创建移到 `_ensure_column` 之后 |
| 转发时用默认设备发错模块 | 改为 `runtime(msg["device_id"]).modem` |
| 去重指纹与 `concat_key` 未混入 device_id | 补上设备维度 |

完整真机验证过程见 [实测记录](docs/实测记录.md)。
