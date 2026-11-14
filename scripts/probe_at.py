#!/usr/bin/env python3
"""在飞牛 NAS 上探测 Air780EPM 的 AT 串口。

纯标准库实现（termios），无需 pip 安装任何东西：
    sudo python3 probe_at.py

输出每个候选口的 AT 应答，并给出应该填进 .env 的 MODEM_PORT。
"""
from __future__ import annotations

import glob
import os
import select
import sys
import time

BAUD = 115200
BAUD_CONST = {9600: "B9600", 19200: "B19200", 38400: "B38400",
              57600: "B57600", 115200: "B115200", 230400: "B230400"}


def candidate_ports() -> list[str]:
    ports: list[str] = []
    # 优先 by-id（重启后名字稳定）
    for p in sorted(glob.glob("/dev/serial/by-id/*")):
        ports.append(p)
    for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*"):
        for p in sorted(glob.glob(pattern), key=lambda x: (len(x), x)):
            real = os.path.realpath(p)
            if real not in [os.path.realpath(x) for x in ports]:
                ports.append(p)
    return ports


def open_serial(dev: str, baud: int = BAUD) -> int:
    import termios

    fd = os.open(dev, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    iflag, oflag, cflag, lflag, _, _, cc = attrs
    iflag = 0
    oflag = 0
    lflag = 0
    cflag = termios.CS8 | termios.CREAD | termios.CLOCAL
    speed = getattr(termios, BAUD_CONST.get(baud, "B115200"))
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])
    termios.tcflush(fd, termios.TCIOFLUSH)
    return fd


def read_for(fd: int, seconds: float) -> str:
    buf = b""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        r, _, _ = select.select([fd], [], [], 0.15)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                continue
            except OSError:
                break
            if chunk:
                buf += chunk
    return buf.decode("utf-8", errors="replace")


def probe(dev: str) -> tuple[bool, str]:
    try:
        fd = open_serial(dev)
    except PermissionError:
        return False, "权限不足（把当前用户加入 dialout 组，或用 sudo 运行）"
    except Exception as exc:
        return False, f"打开失败: {exc}"
    try:
        # 部分合宙固件对裸 AT 回 ERROR，有应答即说明是 AT 口
        for attempt in range(3):
            os.write(fd, b"AT\r")
            resp = read_for(fd, 1.2)
            if "OK" in resp or "ERROR" in resp:
                os.write(fd, b"AT+CGMM\r")
                model = read_for(fd, 1.2).replace("\r", "").replace("\n", " ").strip()
                os.write(fd, b"AT+CPIN?\r")
                cpin = read_for(fd, 1.2).replace("\r", "").replace("\n", " ").strip()
                return True, f"{model} | {cpin}"
            time.sleep(0.3)
        return False, "无 AT 应答（可能是日志口/DIAG 口）"
    finally:
        try:
            os.close(fd)
        except Exception:
            pass


def main() -> int:
    if os.name == "nt":
        print("此脚本面向 Linux(NAS)。Windows 上请用 pyserial 手动测试 COM 口。")
        return 1
    if not os.path.exists("/dev") or not glob.glob("/dev/tty*"):
        print("没有找到任何串口设备节点。")
        return 1

    explicit = sys.argv[1] if len(sys.argv) > 1 else ""
    ports = [explicit] if explicit else candidate_ports()
    if not ports:
        print("没有找到 /dev/ttyUSB* 或 /dev/ttyACM*。请检查：")
        print("  1. USB 线是否插好（Air780EPM 上电后红灯/蓝灯是否亮）")
        print("  2. 执行 lsusb 是否能看到 1782:xxxx（Luat）")
        print("  3. dmesg | tail -30 看是否有 ttyUSB 枚举日志")
        return 1

    print(f"发现 {len(ports)} 个候选串口，开始探测 AT 口…\n")
    found: list[str] = []
    for dev in ports:
        ok, info = probe(dev)
        mark = "✔ AT 口" if ok else "✘"
        print(f"  {mark:<8} {dev:<45} {info}")
        if ok:
            found.append(dev)

    print()
    if found:
        best = found[0]
        print("=" * 68)
        print(f"建议在 .env 中设置：\n\n    MODEM_PORT={best}\n")
        if len(found) > 1:
            print("（多个口都有 AT 应答是正常的，任选其一即可）")
        print("=" * 68)
        return 0

    print("均未应答。排查建议：")
    print("  · 用 sudo 再跑一次（权限问题最常见）")
    print("  · Air780EPM 若烧了 LuatOS 固件，AT 口可能被关闭，需先刷回 AT 固件")
    print("  · 尝试手动指定：sudo python3 probe_at.py /dev/ttyUSB1")
    return 1


if __name__ == "__main__":
    sys.exit(main())
