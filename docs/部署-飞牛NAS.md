# 飞牛 NAS 部署指南

目标：把网关跑在飞牛 NAS 的 Docker 里，USB 口插 Air780EPM，网页收发短信。

整套流程约 15 分钟，其中镜像构建 2~5 分钟。

---

## 0. 前置条件

| 项 | 要求 |
|---|---|
| NAS | 飞牛 fnOS，已安装 Docker（应用中心里有） |
| 硬件 | Air780EPM + 已开通短信的 SIM 卡 |
| 供电 | **必须用带外置电源的 USB Hub**（见下面第 1 节） |
| 网络 | NAS 能和手机正常上网即可，不需要公网 |

固件必须是本项目提供的 `firmware/main.lua`。**Air780EPM 没有 AT 固件**，出厂自带的是 airlink 演示脚本，不刷这个脚本网关认不到短信能力。刷法见 [`烧录指南.md`](烧录指南.md)。

---

## 1. 先把硬件接对（这一步最容易翻车）

Air780EPM 发射瞬间电流能到安培级。**NAS 前面板/背板的 USB 口通常只有 0.5A**，直接插会出现：模块反复重启 → IMEI 跳动 → 网关不停重连。现场表现是"网页上设备一会儿在线一会儿离线"。

正确做法：

```
NAS USB 口 ──▶ 带电源的 USB Hub（自带 DC 5V 适配器）──▶ Air780EPM
```

没有 Hub 时先单台测试也可能正常，但发长短信或信号差时会掉。多台务必上 Hub。

插好后在 NAS 的 SSH 里确认设备节点：

```bash
ls -l /dev/ttyACM*
# 一台模块会出现 3 个：ttyACM0/1/2（ap log / soc log / 用户虚拟串口）
```

看到 3 个 `ttyACM` 说明 USB 认到了。一个都没有 → 换线、换口、查 Hub 供电。

---

## 2. 上传并解压

把 `sms-gateway-1.1.12.tar.gz` 传到 NAS，比如 `/vol1/1000/docker/`：

```bash
cd /vol1/1000/docker
tar -xzf sms-gateway-1.1.12.tar.gz
cd sms-gateway
```

> 飞牛的文件管理器也能直接上传 + 解压，结果一样。

---

## 3. 初始化配置

```bash
chmod +x scripts/install.sh
./scripts/install.sh
```

它会做三件事：检查 docker、从模板生成 `.env`（自动填随机登录密码和会话密钥）、建好 `data/` 目录。**把打印出来的密码记下来。**

想改端口/密码就编辑 `.env`：

```bash
vi .env
```

常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `WEB_PORT` | 8080 | 网页端口，被占用就换 |
| `AUTH_PASSWORD` | 随机 | 登录密码，留空=不登录（仅限可信内网） |
| `MODEM_PORT` | auto | `auto` 自动发现全部模块 |
| `MODEM_PORTS` | 空 | 手动指定，如 `/dev/ttyACM0,/dev/ttyACM3` |
| `KEEP_ON_MODEM` | 0 | 0=入库后从 SIM 删除（避免卡满） |
| `POLL_INTERVAL` | 20 | 轮询间隔秒数 |

### 3.1 SIM 卡带 PIN 码怎么办

国内 SIM 卡出厂默认**不开** PIN。如果这张卡设了 PIN，不处理就是连不上——卡锁着就读不到 ICCID，也注册不上网络，界面只会显示「SIM 未就绪」。两条路：

**A. 把卡里的 PIN 关掉（最省事，一次性）**

插到手机上：设置 → 安全 → SIM 卡锁 → 关闭「锁定 SIM 卡」，输一次 PIN 确认。之后插回模块，网关不需要任何配置。

**B. 让网关替你解锁**

1. 固件必须是 **1.1.1 及以上**（1.0.x 没有 `SETPIN` 指令，会回 `+ERR unknown:SETPIN`）
2. `.env` 里加一行：

   ```ini
   SIM_PIN=1234
   ```

3. `docker compose up -d` 让容器重新加载配置

安全性上做了三层处理：PIN **不烧进脚本**（脚本在模块里能被读出来），由网关运行时下发；只在 `+STATUS` 明确回报 `sim=false` 时才尝试，状态未知绝不动手；每个 SIM 观察窗口最多试 2 次。

> ⚠️ **填之前务必核对清楚。** PIN 连续错 3 次卡会被锁，只能用 PUK 解，PUK 再错 10 次卡就作废。不确定就别填，先用方式 A。

AT 固件的模块走 `AT+CPIN="1234"`，逻辑与限次完全一样。

### 3.2 SIM 卡能热插拔吗

**不建议拔卡，建议拔 USB。** 带电插拔 SIM 座时接触弹跳会让模块看到反复的「卡插入/拔出」，部分固件会误判为卡故障并锁住卡接口，这类问题软件兜不住。

换卡请按这个顺序：

```bash
docker compose stop        # 1. 停容器
# 2. 拔掉模块 USB 线（不是拔卡）
# 3. 换卡
# 4. 插回 USB
docker compose start       # 5. 启动
```

只拔 USB、不换卡的话，插回来即自动恢复：USB 重新枚举后驱动会重建，端口按 IMEI 重新对齐，PIN 解锁预算也会重置。

**1.1.12 起** 网关会主动识别 SIM 状态变化，三种情况自动重置 PIN 重试预算：模块重启（`+BOOT`）、换卡（ICCID 变化）、卡恢复就绪（连续 2 轮确认，防单次抖动误判）。同时，卡曾经就绪后又突然读不到时，会**立刻暂停**自动 PIN 验证，避免对着空卡把 2 次预算白烧掉——界面提示会明确写「SIM 卡已离位」。

> 带 PIN 的卡热插拔后仍可能停在暂停态：此时模块读不到卡，上位机无法区分「空卡」与「PIN 未验证」。重新插拔一次模块 USB 即可恢复。

---

## 4. 构建镜像

```bash
docker compose build
```

已默认使用阿里云 pip 源，且**源不通会自动依次重试**（阿里云 → 腾讯云 → 官方 PyPI），
不需要手工一家家试。想换源就在 `.env` 里放开这两行，改完直接重新 `build`：

```ini
BASE_IMAGE=docker.m.daocloud.io/library/python:3.12-slim
PIP_INDEX=https://mirrors.cloud.tencent.com/pypi/simple
PIP_FALLBACK_INDEXES=https://pypi.org/simple
```

如果报 `Could not find a version that satisfies the requirement ... (from versions: none)`，
这是**索引根本没被取到**（不是版本写错），三种原因按概率排序：

| 原因 | 判断方法 | 处理 |
|---|---|---|
| 该源在你的网络里不通 | 换个源，构建日志会看到 `!! 该源不可用，换下一个` | 已在 `.env` 配了回退列表，通常不用管 |
| 构建容器没有 DNS | 构建容器里 `pip download --no-deps -d /tmp x` 也失败 | `docker compose build` 前设 `export DOCKER_BUILDKIT=1`，或在 `build` 段加工 `network: host` |
| 需要认证代理 | 宿主机能上网、容器不行 | 在 `docker-compose.yml` 的 `build.args` 里加 `HTTPS_PROXY` |

单次换源验证（不改文件）：

```bash
docker compose build --build-arg PIP_INDEX=https://pypi.org/simple
```

---

## 5. 自检（强烈建议）

```bash
docker compose run --rm --entrypoint python sms-gateway scripts/preflight.py
```

正常输出长这样：

```
=== 串口 ===
  /dev/ttyACM0     19D1:0001  loc=1-1.2:1.0  ...
  /dev/ttyACM1     19D1:0001  loc=1-1.2:1.2  ...
  /dev/ttyACM2     19D1:0001  loc=1-1.2:1.4  ...

识别到 1 台模块（按 USB 拓扑分组）：
  拓扑 1-1.2   端口 /dev/ttyACM0, /dev/ttyACM1, /dev/ttyACM2
    /dev/ttyACM0     无响应（日志口，正常）
    /dev/ttyACM1     [ OK ] 回 +PONG，是数据口

=== 结论 ===
[ OK ] 环境正常，可以直接 docker compose up -d
```

三种失败情况及对应处理：

| 现象 | 原因 | 处理 |
|---|---|---|
| 一个串口都没有 | USB 没认到 / 供电不足 | 换带电源 Hub，`lsusb` 确认有 `19d1:` 开头的设备 |
| 有串口但 VID 不是 19D1 | 用的是 USB 转串口芯片 | 直插模块的 USB 口，不要经转接板 |
| 有串口但没有 `+PONG` | 模块在跑别的脚本 | 按 `docs/烧录指南.md` 重刷 `firmware/main.lua` |
| 提示"打开失败 / 端口被独占" | 网关还在跑，串口被占了 | `docker compose stop sms-gateway` 后重跑自检 |

---

## 6. 启动

```bash
docker compose up -d
docker compose logs -f      # Ctrl+C 退出查看，不影响容器
```

日志里应该出现：

```
网关启动，端口=auto mock=False
发现 1 台设备: 1-1.2@/dev/ttyACM1
```

浏览器打开 `http://<NAS_IP>:8080`，输入第 3 步的密码。

界面顶部应显示：运营商（如"中国电信"）、信号百分比、号码。**设备卡片显示"已连接"就算成功。**

---

## 7. 验证收发

**发**：点右上「新建」，收件人填自己的手机号，发一条。

**收**：用手机给模块里的 SIM 卡号发一条短信，网页会通过 WebSocket 实时弹出。

如果发送成功但收不到回信，看 `docker compose logs -f` 里有没有 `sync` 相关报错，以及 SIM 卡是否欠费。

---

## 8. 重启 / 断电后的行为

- 容器设置了 `restart: unless-stopped`，NAS 重启后会自动拉起。
- 模块重启导致的 `ttyACM` 编号漂移会被自动纠正：程序按 **USB 拓扑分组**认设备，连续 3 次同步失败会重新扫描端口并重新映射（日志里出现 `端口漂移 ttyACM2→ttyACM5，已重新映射`）。
- 设备主键是 **IMEI**，所以历史消息永远挂在同一台模块下，不会因为换口而错乱。

---

## 9. 多台模块

同一份代码、同一个容器，**直接把第二台插上**即可，不需要改配置：

```bash
docker compose restart sms-gateway
curl -s http://127.0.0.1:8080/api/devices | head -c 800
```

会看到两个 `device_id`（IMEI）。发信时可指定设备：

```bash
curl -X POST http://127.0.0.1:8080/api/messages \
  -H "Content-Type: application/json" \
  -d '{"peer":"13800138000","text":"从2号机发","device":"860000000000001"}'
```

不指定 `device` 时按「已注册且有 SIM → 任意在线」自动挑一台。

详细设计见 [`多设备.md`](多设备.md)。

---

## 10. 备份与升级

**备份**（只有 `data/` 重要，里面是 SQLite）：

```bash
docker compose stop
tar -czf sms-backup-$(date +%F).tar.gz data/
docker compose start
```

**升级（推荐）**：用仓库里的 `scripts/upgrade.sh`。它会自动备份、复用旧 `.env`、健康检查失败自动回滚：

```bash
./scripts/upgrade.sh /vol1/1000/docker/sms-gateway /vol1/1000/docker/sms-gateway-1.1.12
```

**手工升级**要注意：发布包只有 `.env.example`，**不含真实 `.env`**（避免密码进 git）。所以必须从旧目录把 `.env` 复制过来：

```bash
cd /vol1/1000/docker
tar -xzf sms-gateway-1.1.12.tar.gz
ln -s /vol1/1000/docker/sms-gateway/data sms-gateway-1.1.12/data
cp -p sms-gateway/.env sms-gateway-1.1.12/.env          # ← 漏掉这步会退回默认配置
cd sms-gateway-1.1.12 && docker compose up -d --build
```

> **漏 `cp .env` 的后果**：`AUTH_PASSWORD` / `SECRET_KEY` 变空 —— 登录状态全部作废，
> 而且容器里跑的是裸露的默认配置。验证方法：`docker inspect sms-gateway | grep -E 'AUTH_PASSWORD|SECRET_KEY'`。
>
> 数据库会自动做增量迁移（`messages` 表新增列不会丢数据）。

---

## 11. 常见问题

| 现象 | 排查 |
|---|---|
| 网页打不开 | `docker compose ps` 看是否 `Up`；`netstat -tlnp \| grep 8080` 看端口占用；飞牛防火墙放行 |
| 显示"设备未连接 / 无 AT 响应" | 跑第 5 步自检；确认固件已刷、`MODEM_PROTOCOL=auto` |
| 自检说"没认出合宙模块"，但 `ls /dev/ttyACM*` 有设备 | 容器读不到 USB 描述符（少见）。在 `.env` 里手动指定数据口：`MODEM_PORTS=/dev/ttyACM1`，然后 `docker compose up -d` |
| 日志里设备标识在 `1-1.1` / `1-1.3` 之间跳变 | v1.1.3 以前按**接口级**路径分组，一台模块被拆成 3 台。升级到 1.1.3 即按 USB 设备级路径（`1-1`）分组，标识稳定 |
| 日志出现"发现 1 台设备"后再无下文 | v1.1.3 起会明确打出 `[IMEI] 已连接 …` 或 `[拓扑键] 连接失败: 原因`，直接照原因处理；旧版看不到结果 |
| 启动时报"未发现任何模块" | 模块刚上电脚本还没起。v1.1.3 会重试 3 轮（约 10 秒），仍在报就等 15 秒后点界面「重连」 |
| 信号有但注册失败 | SIM 卡是否欠费、是否被停机；重新插拔等 30 秒 |
| SIM 未就绪 / 卡设了 PIN | 见 [3.1](#31-sim-卡带-pin-码怎么办)：关掉 PIN，或配 `SIM_PIN`（固件需 1.1.1+） |
| 运营商显示 `—` | 从 IMSI 反推，SIM 未就绪时为空属正常 |
| 时间差 8 小时 | 镜像已装 tzdata；确认 `.env` 里 `TZ=Asia/Shanghai` |
| 容器不断重启 | `docker compose logs --tail=80 sms-gateway` 看退出原因；`docker compose ps` 看 RESTARTS 列 |
| 报 `[Errno 1] Operation not permitted` 打开串口 | **不是文件权限问题**（那会报 `Errno 13`），是内核 device cgroup 没放行，`chmod` / 加 `dialout` 组都没用。v1.1.4 的 compose 已默认 `privileged: true`，`docker compose up -d --force-recreate` 即可。旧包手动把 `device_cgroup_rules` 注释掉、加 `privileged: true` |
| 报 `[Errno 13] Permission denied` | 真正的权限问题：宿主 `sudo usermod -aG dialout $USER` 后重登，或直接用 `privileged: true` |
| 日志里设备标识显示成 `/dev/ttyACM2` 而不是 `1-1` | 容器内 sysfs 被裁剪，取不到 USB 拓扑。v1.1.4 起优先读 `/dev/serial/by-id`（一台模块的 3 个口归成一组），不再飘 |

查看设备是否被宿主认到的底层命令：

```bash
lsusb | grep -i 19d1      # 合宙 VID
dmesg | tail -20          # 看 USB 枚举与 ttyACM 创建
```

---

## 12. 反向代理（可选）

飞牛自带反代功能，把这个服务挂成 `sms.你的域名` 时注意两点：

1. 必须转发 **WebSocket**（`Upgrade` / `Connection` 头），否则新短信不会实时弹出。
2. 大 body 不需要放开，接口都是小 JSON。

Nginx 片段：

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```
