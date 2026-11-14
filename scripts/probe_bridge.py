#!/usr/bin/env python3
"""探测哪一路串口上跑着本项目的 LuatOS 桥接脚本。

刷完 firmware/main.lua 后用这个验证，不需要启动网关服务。
Windows 依赖 pyserial（项目 venv 里已有）；Linux 上纯标准库即可跑。

用法：
    python3 scripts/probe_bridge.py                # 自动扫全部串口
    python3 scripts/probe_bridge.py /dev/ttyACM0   # 只测指定口
"""
from __future__ import annotations

import json
import os
import sys
import time

BAUD = 115200
PING_TIMEOUT = 2.0


def list_ports() -> list[str]:
    try:
        from serial.tools import list_ports as lp
        return [p.device for p in lp.comports()]
    except ImportError:
        pass
    if os.name != "nt":
        import glob
        ports: list[str] = []
        for pattern in ("/dev/ttyACM*", "/dev/ttyUSB*", "/dev/serial/by-id/*"):
            ports.extend(sorted(glob.glob(pattern)))
        return ports
    return []


class Port:
    """把 pyserial / termios 两种后端包一层，只用到读写和打开关闭。"""

    def __init__(self, dev: str, baud: int = BAUD) -> None:
        self.dev = dev
        self._impl = None
        self._fd: int | None = None
        try:
            import serial  # type: ignore
            ser = serial.Serial()
            ser.port = dev
            ser.baudrate = baud
            ser.timeout = 0.1
            ser.write_timeout = 3
            ser.open()
            try:
                ser.dtr = True
                ser.rts = True
            except Exception:
                pass
            self._impl = ser
        except ImportError:
            import termios
            fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0
            attrs[1] = 0
            attrs[2] = termios.CLOCAL | termios.CREAD | termios.CS8
            attrs[3] = 0
            attrs[4] = attrs[5] = termios.B115200
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            self._fd = fd

    def write(self, data: bytes) -> None:
        if self._impl is not None:
            self._impl.write(data)
            self._impl.flush()
        elif self._fd is not None:
            os.write(self._fd, data)

    def read(self, timeout: float) -> str:
        """读到空闲或超时为止；日志口会持续刷数据，所以必须有绝对截止时间。"""
        buf = b""
        hard_end = time.time() + timeout
        idle_end = hard_end
        if self._impl is not None:
            while time.time() < min(hard_end, idle_end):
                chunk = self._impl.read(4096)
                if chunk:
                    buf += chunk
                    idle_end = time.time() + 0.3
                else:
                    time.sleep(0.02)
            return buf.decode("utf-8", "replace")
        while time.time() < min(hard_end, idle_end):
            try:
                chunk = os.read(self._fd, 4096)  # type: ignore[arg-type]
            except (BlockingIOError, OSError):
                chunk = b""
            if chunk:
                buf += chunk
                idle_end = time.time() + 0.3
            else:
                time.sleep(0.05)
        return buf.decode("utf-8", "replace")

    def close(self) -> None:
        if self._impl is not None:
            try:
                self._impl.close()
            except Exception:
                pass
        if self._fd is not None:
            try:
                os.close(self._fd)
            except Exception:
                pass


def probe(dev: str) -> bool:
    try:
        port = Port(dev)
    except PermissionError:
        print(f"  ✘ {dev:<14} 权限不足（sudo 或加入 dialout / uucp 组）")
        return False
    except Exception as exc:
        print(f"  · {dev:<14} 打开失败: {exc}")
        return False

    try:
        time.sleep(0.15)
        port.read(0.15)                     # 清掉上电日志
        port.write(b"PING\r\n")
        resp = port.read(PING_TIMEOUT)
    except Exception as exc:                 # 蓝牙串口之类的写超时，直接跳过
        print(f"  · {dev:<14} 不可用: {exc}")
        port.close()
        return False

    try:
        if "+PONG" not in resp:
            if resp.strip():
                head = resp.strip().splitlines()[-1][:60]
                print(f"  · {dev:<14} 有数据但不是桥接脚本（{head}）")
            else:
                print(f"  · {dev:<14} 无响应")
            return False

        port.write(b"STATUS\r\n")
        raw = port.read(3.0)
        payload: dict = {}
        for line in raw.splitlines():
            if line.startswith("+STATUS "):
                try:
                    payload = json.loads(line[8:])
                except json.JSONDecodeError:
                    pass
                break

        csq = payload.get("csq", "?")
        reg = payload.get("reg", "?")
        sim = "已就绪" if payload.get("sim") else "未就绪"
        iccid = payload.get("iccid") or "—"
        fw = payload.get("fw") or "—"
        print(f"  ✔ {dev:<14} 桥接脚本在线")
        print(f"      信号 CSQ={csq}  注册状态={reg}  SIM={sim}")
        print(f"      ICCID={iccid}")
        print(f"      固件={fw}")
        print(f"\n建议在 .env 中设置：MODEM_PORT={dev}")
        return True
    finally:
        port.close()


def main() -> int:
    targets = sys.argv[1:] or list_ports()
    if not targets:
        print("没有发现任何串口，先确认模块插好、线能传数据。")
        return 1

    print(f"扫描 {len(targets)} 个串口…\n")
    hits: list[str] = []
    for dev in targets:
        if probe(dev):
            hits.append(dev)
    print()
    if hits:
        print(f"共 {len(hits)} 个口跑着桥接脚本：{', '.join(hits)}")
        return 0
    print("没找到桥接脚本。请确认 firmware/main.lua 已烧录，且模块已重启。")
    return 2


if __name__ == "__main__":
    sys.exit(main())
