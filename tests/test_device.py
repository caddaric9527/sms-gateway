"""设备发现自测：python -m tests.test_device

锁住两件事：
1. 同一台模块的 3 个串口必须归到**同一个** USB 拓扑键（接口号 1-1.1 / 1-1:1.0 都要被剥掉）；
2. 探测不到时必须先重试，**不能**急着重试就退回 AT 模式。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.device as device_mod  # noqa: E402
import app.luatos as luatos_mod  # noqa: E402
import app.modem as modem_mod  # noqa: E402

FAILED = []


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(f"{name}: 期望 {expected!r}, 实际 {actual!r}")


class FakePort:
    """模拟 pyserial 的 ListPortInfo。"""

    def __init__(self, dev, location="", hwid=""):
        self.device = dev
        self.location = location
        self.hwid = hwid
        self.vid = 0x19D1


# ---------------------------------------------------------------- sysfs 路径解析
SI = "/sys/devices/pci0000:00/0000:00:14.0/usb1/1-1"
check("接口目录 1-1:1.0 → 设备 1-1",
      device_mod._usb_from_sysfs_path(f"{SI}/1-1:1.0/tty/ttyACM0"), "1-1")
check("接口目录 1-1:1.3 → 设备 1-1",
      device_mod._usb_from_sysfs_path(f"{SI}/1-1:1.3"), "1-1")
check("hub 链路 1-1.2:1.0 → 设备 1-1.2",
      device_mod._usb_from_sysfs_path(f"{SI}/1-1.2/1-1.2:1.0/tty/ttyACM2"), "1-1.2")
check("没有 USB 段时返回空",
      device_mod._usb_from_sysfs_path("/sys/devices/platform/serial8250/tty/ttyS0"), "")

# ---------------------------------------------------------------- 分组键
check("location 带接口 → 1-1",
      device_mod._group_key(FakePort("/dev/ttyACM0", location="1-1:1.0")), "1-1")
check("hwid 里的 LOCATION 带接口 → 1-1",
      device_mod._group_key(FakePort("/dev/ttyACM0", hwid="USB VID:PID=19D1:0001 LOCATION=1-1:1.3")), "1-1")
check("location 缺失时退化为端口名",
      device_mod._group_key(FakePort("/dev/ttyUSB9")), "/dev/ttyUSB9")

# ---------------------------------------------------------------- by-id 分组
# 容器里 /sys/class/tty/*/device 常常解析不出来（sysfs 被裁剪），此时只能靠 by-id。
# 一台模块的 3 个 CDC 口共享串号、只差 -ifNN 后缀，剥掉后缀必须归成同一组。
import os
import tempfile

with tempfile.TemporaryDirectory() as tmp:
    by_id_dir = Path(tmp) / "serial" / "by-id"
    by_id_dir.mkdir(parents=True)
    acm2 = Path(tmp) / "ttyACM2"
    acm2.write_text("", encoding="utf-8")
    acm0 = Path(tmp) / "ttyACM0"
    acm0.write_text("", encoding="utf-8")
    # 用硬链接模拟 udev 生成的 by-id 条目（Windows 上创建符号链接需要额外权限）
    os.link(acm2, by_id_dir / "usb-Luat_LuatOS_Air780EPM_862830072513409-if02")
    os.link(acm0, by_id_dir / "usb-Luat_LuatOS_Air780EPM_862830072513409-if00")

    real_by_id = device_mod._SERIAL_BY_ID
    device_mod._SERIAL_BY_ID = str(by_id_dir)
    try:
        check("by-id 剥掉 -ifNN 得到设备级键",
              device_mod._linux_serial_by_id(str(acm2)),
              "usb-Luat_LuatOS_Air780EPM_862830072513409")
        check("同一模块两个口分组键相同",
              device_mod._group_key(FakePort(str(acm2))),
              device_mod._group_key(FakePort(str(acm0))))
        check("by-id 里查不到的口返回空",
              device_mod._linux_serial_by_id(str(Path(tmp) / "ttyACM9")), "")
    finally:
        device_mod._SERIAL_BY_ID = real_by_id

# ---------------------------------------------------------------- 打开失败提示
# EPERM(1) 是 cgroup 没放行，不是文件权限位，报错必须直接指到 privileged。
check("EPERM 指向容器 privileged",
      "privileged" in modem_mod.port_error_hint(PermissionError(1, "Operation not permitted")), True)
check("EACCES 指向 dialout 组",
      "dialout" in modem_mod.port_error_hint(PermissionError(13, "Permission denied")), True)
check("设备被占用时提示停掉占用进程",
      "占用" in modem_mod.port_error_hint(OSError(16, "Device busy")), True)
check("普通错误不硬塞提示",
      modem_mod.port_error_hint(ValueError("boom")), "")

# ---------------------------------------------------------------- 探测重试顺序
real_groups = device_mod.candidate_groups
real_probe = luatos_mod.probe_bridge
real_cands = modem_mod.Modem.candidate_ports
calls = {"n": 0}


def patch(groups, probe):
    device_mod.candidate_groups = lambda: groups
    luatos_mod.probe_bridge = probe
    modem_mod.Modem.candidate_ports = staticmethod(lambda: ["/dev/ttyACM0"])


try:
    # A) 一台模块 3 个口，只有 ACM1 是数据口
    calls["n"] = 0
    patch([("1-1", ["/dev/ttyACM0", "/dev/ttyACM1", "/dev/ttyACM2"])],
          lambda port, timeout=2.0: port == "/dev/ttyACM1")
    found = device_mod.detect_devices(attempts=1, gap=0)
    check("3 个口里认出数据口",
          [(d.port, d.group, d.kind) for d in found],
          [("/dev/ttyACM1", "1-1", "luatos")])

    # B) 模块还在启动：第 2 轮才应答，必须继续重试而不是退回 AT
    def probe_second_round(port, timeout=2.0):
        calls["n"] += 1
        return calls["n"] >= 2

    calls["n"] = 0
    patch([("1-1", ["/dev/ttyACM1"])], probe_second_round)
    found = device_mod.detect_devices(attempts=3, gap=0)
    check("第 2 轮才应答仍认成桥接脚本",
          [(d.port, d.kind) for d in found], [("/dev/ttyACM1", "luatos")])
    check("只重试到命中就停", calls["n"], 2)

    # C) 一直不应答：才退回 AT 模式
    calls["n"] = 0
    patch([("1-1", ["/dev/ttyACM1"])], lambda port, timeout=2.0: False)
    found = device_mod.detect_devices(attempts=2, gap=0)
    check("始终无应答才退回 AT", [(d.port, d.kind) for d in found],
          [("/dev/ttyACM0", "at")])
finally:
    device_mod.candidate_groups = real_groups
    luatos_mod.probe_bridge = real_probe
    modem_mod.Modem.candidate_ports = staticmethod(real_cands)

print()
if FAILED:
    print(f"{len(FAILED)} 项失败:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
