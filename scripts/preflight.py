#!/usr/bin/env python3
"""部署前自检：容器能不能看到模块、数据目录能不能写、桥接脚本在不在线。

用法（必须先停掉正在跑的网关，串口是独占的）：

    docker compose stop sms-gateway
    docker compose run --rm --entrypoint python sms-gateway scripts/preflight.py
    docker compose start sms-gateway

退出码：0=至少一台握手成功；1=看得到串口但脚本没响应；2=完全看不到合宙设备；
        3=串口被占用（网关在跑，结果不可信）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OK = "[ OK ]"
NO = "[FAIL]"
WARN = "[WARN]"


def line(title: str) -> None:
    print(f"\n=== {title} ===")


def check_runtime() -> None:
    line("运行环境")
    print(f"python      : {sys.version.split()[0]}")
    print(f"工作目录    : {os.getcwd()}")
    print(f"DB_PATH     : {os.environ.get('DB_PATH', '')}")
    print(f"TZ          : {os.environ.get('TZ', '')}")
    try:
        import serial  # noqa: F401

        print(f"pyserial    : {serial.__version__}")
    except ImportError:
        print(f"pyserial    : {NO} 未安装")


def check_data_dir() -> bool:
    line("数据目录")
    # 容器里 DB_PATH=/data/sms.db；本地直接跑时没有这个变量，回落到项目内的 data/
    db_path = os.environ.get("DB_PATH") or str(Path.cwd() / "data" / "sms.db")
    target = Path(db_path).parent
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".preflight"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        print(f"{target} 可写 {OK}")
        print(f"已存在数据库: {'是' if Path(db_path).exists() else '否（首次启动会自动创建）'}")
        return True
    except Exception as exc:
        print(f"{target} 不可写 {NO} {exc}")
        print("  → 检查 docker-compose.yml 里的 ./data:/data 挂载与宿主机权限")
        return False


def check_ports() -> int:
    line("串口")
    try:
        from serial.tools import list_ports
    except ImportError:
        print(f"{NO} 没有 pyserial，无法枚举串口")
        return 2

    comps = list(list_ports.comports())
    if not comps:
        print(f"{NO} 一个串口都没看到")
        print("  → 模块是否插好？NAS 的 USB 口是否供电？插带电源的 Hub 试试")
        print("  → 宿主机执行 ls -l /dev/ttyACM* 确认设备节点存在")
        return 2

    for c in comps:
        vid = f"{c.vid:04X}" if c.vid else "----"
        pid = f"{c.pid:04X}" if c.pid else "----"
        loc = getattr(c, "location", "") or "-"
        print(f"  {c.device:<16} {vid}:{pid}  loc={loc:<10} {c.description}")

    from app.device import candidate_groups

    groups = candidate_groups()
    if not groups:
        print(f"\n{WARN} 没认出合宙模块（VID 应为 19D1 / 1782）")
        print("  上面列表里如果 VID 是 1A86/10C4，说明用的是 USB 转串口而不是模块直连")
        return 2

    print(f"\n识别到 {len(groups)} 台模块（按 USB 拓扑分组）：")
    import serial

    from app.luatos import probe_bridge

    hits = 0
    busy = 0
    for key, ports in groups:
        print(f"  拓扑 {key:<10} 端口 {', '.join(ports)}")
        data_port = ""
        for port in ports:
            # 先试探能否独占打开：网关在跑时串口被占用，直接 PING 会得到"没响应"，
            # 这跟"固件没刷"表现一样，必须先区分开，否则会误导用户去重刷固件。
            try:
                s = serial.Serial(port, 115200, timeout=0.05)
                s.close()
            except Exception as exc:
                busy += 1
                print(f"    {port:<16} {WARN} 打开失败: {exc}")
                print("         → 端口被独占，先停网关再自检:")
                print("           docker compose stop sms-gateway")
                continue
            try:
                ok = probe_bridge(port, 2.0)
            except Exception as exc:
                print(f"    {port:<16} 探测异常: {exc}")
                continue
            if ok:
                data_port = port
                print(f"    {port:<16} {OK} 回 +PONG，是数据口")
                break
            print(f"    {port:<16} 无响应（日志口，正常）")
        if data_port:
            hits += 1
        elif busy:
            continue
        else:
            print(f"    {NO} 这台没有数据口响应：桥接脚本没跑起来或不在固件里")
            print("         → 按 docs/烧录指南.md 重新刷 firmware/main.lua")

    if hits:
        return 0
    return 3 if busy else 1


def main() -> int:
    print("Air780EPM 短信网关 · 部署自检")
    check_runtime()
    data_ok = check_data_dir()
    code = check_ports()

    line("结论")
    if code == 0 and data_ok:
        print(f"{OK} 环境正常，可以直接 docker compose up -d")
    elif code == 0:
        print(f"{WARN} 模块正常，但数据目录不可写，先修挂载")
    elif code == 3:
        print(f"{WARN} 串口被占用，自检结果不可信 —— 先停网关再跑一次")
    elif code == 1:
        print(f"{NO} 串口在，但桥接脚本没应答 —— 需要重刷固件")
    else:
        print(f"{NO} 没看到模块，先排查 USB 供电与接线")
    return code


if __name__ == "__main__":
    sys.exit(main())
