#!/usr/bin/env python3
"""串口状态看板：确认模块是否已进入 BOOT 下载模式。

用法：
    python scripts/port_watch.py            # 打一次快照并给结论
    python scripts/port_watch.py --watch    # 持续监听，端口集合变化时打印（Ctrl+C 退出）

判断依据（合宙官方）：进 BOOT 下载模式后，模块在系统里只枚举出 1 个串口；
正常运行时是 3~4 个。
"""

from __future__ import annotations

import argparse
import sys
import time

try:
    from serial.tools import list_ports
except ImportError:
    print("需要 pyserial：pip install pyserial", file=sys.stderr)
    sys.exit(2)

LUAT_VIDS = {0x19D1, 0x1782}


def snapshot() -> list[tuple[str, object]]:
    return [(p.device, p) for p in list_ports.comports()]


def describe(ports: list[tuple[str, object]]) -> tuple[str, str]:
    """返回 (结论标记, 说明)。"""
    luat = [(d, p) for d, p in ports if getattr(p, "vid", None) in LUAT_VIDS]
    pool = luat or ports
    if not pool:
        return "none", "没发现任何串口：检查 USB 线（要能传数据）和驱动"
    if len(pool) == 1:
        dev = pool[0][0]
        return "boot", f"模块只有 1 个串口（{dev}）→ 已进入 BOOT 下载模式，可以去 Luatools 点下载了"
    return "normal", (
        f"模块有 {len(pool)} 个串口（{'/'.join(d for d, _ in pool)}）"
        "→ 仍是正常运行模式，需要按住 BOOT 再复位才能烧录"
    )


def show(ports: list[tuple[str, object]]) -> str:
    lines = []
    for dev, p in ports:
        vid = getattr(p, "vid", None)
        tag = " ← 合宙模块" if vid in LUAT_VIDS else ""
        lines.append(f"  {dev:<8} vid={vid} pid={getattr(p, 'pid', None)} | {p.description}{tag}")
    state, msg = describe(ports)
    icon = {"boot": "✔", "normal": "●", "none": "✘"}[state]
    lines.append("")
    lines.append(f"{icon} {msg}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="持续监听端口变化")
    ap.add_argument("--interval", type=float, default=1.0, help="轮询间隔，默认 1 秒")
    args = ap.parse_args()

    ports = snapshot()
    print(show(ports))
    if not args.watch:
        return 0

    print("\n监听中，端口变化会打印出来…（Ctrl+C 退出）")
    last = {d for d, _ in ports}
    while True:
        try:
            time.sleep(args.interval)
            ports = snapshot()
            cur = {d for d, _ in ports}
            if cur != last:
                print(f"\n[{time.strftime('%H:%M:%S')}] 端口变化：{'/'.join(sorted(last)) or '无'} → {'/'.join(sorted(cur)) or '无'}")
                print(show(ports))
                last = cur
        except KeyboardInterrupt:
            print("\n已退出。")
            return 0


if __name__ == "__main__":
    sys.exit(main())
