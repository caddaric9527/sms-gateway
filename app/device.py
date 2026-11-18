"""多设备发现。

难点不在"能不能打开串口"，而在于**认出口属于哪台物理模块**：
一台 Air780EPM 在 USB 上是复合设备，会同时暴露 3 个串口（ap log / soc log / 用户虚拟串口），
插两台就是 6 个。直接按端口号排序会把日志口当成数据口，也会在热插拔后错位。

做法：
1. 用 pyserial 的 LOCATION（USB 拓扑路径，如 `1-7`）把端口分组 —— 同一台模块的 3 个口
   共享同一个拓扑前缀，插在哪台 Hub 的哪个口上一一对应，重启/重插都不变。
2. 组内逐个发 PING，谁回 `+PONG` 谁就是那台模块的 VUART 数据口。
3. 探测时顺手读回 IMEI，**按 IMEI 再做一次收敛**：分组键终究是"猜"出来的，
   而合宙这块板子的 USB iSerial 出厂固定为 `000000000001`，两块同时插上时
   `/dev/serial/by-id` 会因重名互相覆盖（udev 只留后枚举的那台），sysfs 拓扑在容器里
   还可能被裁剪 —— 任一失效都会让同一台模块被拆成多台。IMEI 读得到就以它为准。
4. 设备主键用 IMEI（连接后从 `+STATUS` 拿到），端口只在启动时做一次映射。
"""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

from .config import settings

try:
    from serial.tools import list_ports as _list_ports
except ImportError:      # pragma: no cover - 无 pyserial 时由调用方降级到 mock
    _list_ports = None

# 合宙模块的 VID（19D1 = EC618/EC718 系列，1782 = 早期方案）
LUAT_VIDS = (0x19D1, 0x1782)

_LOCATION_RE = re.compile(r"LOCATION=([0-9A-Za-z][0-9A-Za-z\-]*)", re.IGNORECASE)
# USB 设备级路径：1-1、1-1.2.3（hub 链）。接口层是 1-1:1.0，不匹配。
_USB_DEV_RE = re.compile(r"^\d+-[\d.]+$")


@dataclass
class FoundDevice:
    """一台探测到的模块。"""

    port: str
    group: str          # USB 拓扑分组，作为设备未连上前的临时标识
    kind: str           # luatos | at
    imei: str = ""      # 探测时顺手读到的 IMEI，用于收敛重复分组


def _usb_from_sysfs_path(real: str) -> str:
    """从 sysfs 真实路径里取出 USB **设备级**目录名（`1-1`、`1-1.2`）。

    接口目录是 `1-1:1.0`（含冒号），不匹配，会继续往上找到设备目录。
    """
    # 同时按 / 和 \ 切：Windows 上开发、Linux 上运行时都能正确解析
    for part in reversed(re.split(r"[\\/]", str(real))):
        if _USB_DEV_RE.match(part):
            return part
    return ""


_SERIAL_BY_ID = "/dev/serial/by-id"
# by-id 链接名末尾的接口后缀：`-if02`、`-if02-port0`
_IFACE_SUFFIX_RE = re.compile(r"-if\d+(?:-port\d+)?$")


def _linux_serial_by_id(port: str) -> str:
    """从 `/dev/serial/by-id` 取**设备级**分组键。

    容器里 sysfs 经常被裁剪（`/sys/class/tty/*/device` 解析不出来），此时会退化成用
    `/dev/ttyACM2` 当键，一条 USB 复合设备的 3 个口就成了 3 台"不同设备"。by-id 目录由
    udev 生成、随 `/dev` 一起挂进容器，是最可靠的来源。

    链接名形如 `usb-Luat_LuatOS_Air780EPM_862830072513409-if02`：同一台模块的多个 CDC 口
    共享同一串号，只有 `-ifNN` 后缀不同，剥掉后缀就是稳定键，重启、重插、ttyACM 号漂移都不变。
    """
    port = str(port or "")
    if not port or not os.path.isdir(_SERIAL_BY_ID):
        return ""
    # 按 inode 比对而不是比路径：符号链接、硬链接都能命中，也不受容器里
    # /dev 与挂载点路径不一致的影响
    try:
        want = os.stat(port)
    except OSError:
        return ""
    try:
        entries = os.listdir(_SERIAL_BY_ID)
    except OSError:
        return ""
    for name in sorted(entries):
        try:
            got = os.stat(os.path.join(_SERIAL_BY_ID, name))
        except OSError:
            continue
        if (got.st_dev, got.st_ino) != (want.st_dev, want.st_ino):
            continue
        key = _IFACE_SUFFIX_RE.sub("", name)
        if key:
            return key
    return ""


def _linux_usb_path(port: str) -> str:
    """Linux 下用 sysfs 精确取 USB 设备级路径，把同一台模块的 3 个口归到一组。

    pyserial 在 Linux 给的 location 是**接口级**的（`1-1.1`、`1-1.3`），一台模块会被拆成
    3 组，日志里就会出现 `1-1.3@ttyACM0` 和 `1-1.1@ttyACM2` 这种飘忽的身份。
    """
    name = os.path.basename(str(port or ""))
    if not name:
        return ""
    try:
        real = os.path.realpath(f"/sys/class/tty/{name}/device")
    except OSError:
        return ""
    if not real or not os.path.exists(real):
        return ""
    return _usb_from_sysfs_path(real)


def _group_key(info) -> str:
    """取 USB 设备级标识作为分组键；取不到就退化成端口名（各自独立成组）。"""
    port = str(getattr(info, "device", ""))
    # 1) by-id：容器里唯一可靠的来源（sysfs 可能不可见）
    by_id = _linux_serial_by_id(port)
    if by_id:
        return by_id
    # 2) sysfs 拓扑，宿主直跑最准
    usb = _linux_usb_path(port)
    if usb:
        return usb
    for attr in ("location", "hwid"):
        raw = getattr(info, attr, "") or ""
        if not raw:
            continue
        m = _LOCATION_RE.search(raw) if attr == "hwid" else None
        key = m.group(1) if m else raw
        # 统一剥掉接口后缀：`1-1:1.0` / `1-1.1` 都归到 `1-1`
        key = key.split(":")[0].strip()
        if key:
            return key
    return str(getattr(info, "device", ""))


def candidate_groups() -> list[tuple[str, list[str]]]:
    """按物理模块分组返回 [(组键, [端口, ...]), ...]。只保留合宙模块。"""
    if _list_ports is None:
        return []
    groups: dict[str, list[str]] = {}
    try:
        infos = list(_list_ports.comports())
    except Exception:
        return []
    for info in infos:
        vid = getattr(info, "vid", None)
        if vid not in LUAT_VIDS:
            continue
        groups.setdefault(_group_key(info), []).append(str(info.device))
    # 组内端口顺序稳定（COM5/COM6/COM7 这类按名字排）
    return [(key, sorted(ports)) for key, ports in sorted(groups.items())]


def explicit_ports() -> list[str]:
    """MODEM_PORTS 显式指定的端口列表。"""
    raw = (settings.modem_ports or "").strip()
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[,;\s]+", raw) if p.strip()]


def _collapse(tasks: list[tuple[str, str]], infos: list[dict]) -> list[FoundDevice]:
    """把逐口探测结果收敛成「一台模块一条」。

    主依据是 IMEI：同一台模块的多个口即使被分组键拆散，IMEI 仍然相同，收敛回一台；
    反过来，两块 iSerial 撞车的板子即使被塞进同一组，IMEI 不同就必须拆成两台。
    读不到 IMEI 的口（老固件没有该字段）退回按拓扑键去重，保持旧行为。
    """
    by_imei: dict[str, FoundDevice] = {}
    by_group: dict[str, FoundDevice] = {}
    for (group, port), info in zip(tasks, infos):
        if not info.get("ok"):
            continue
        imei = str(info.get("imei") or "")
        if imei:
            prev = by_imei.get(imei)
            # 同一 IMEI 命中多个口时固定取端口名最小的，保证多次探测结果一致
            if prev is None or port < prev.port:
                by_imei[imei] = FoundDevice(port=port, group=group, kind="luatos", imei=imei)
        elif group not in by_group:
            by_group[group] = FoundDevice(port=port, group=group, kind="luatos")

    out = sorted(by_imei.values(), key=lambda d: d.port)
    taken = {d.port for d in out}
    seen = {d.group for d in out}
    for dev in sorted(by_group.values(), key=lambda d: d.port):
        if dev.port in taken or dev.group in seen:
            continue
        out.append(dev)
    return out


def detect_devices(attempts: int = 3, gap: float = 2.0) -> list[FoundDevice]:
    """探测全部模块。会打开串口，务必放在线程里跑。

    模块刚上电时 Lua 脚本要几秒才起得来，此时 PING 不会应答。探测不到就重试，
    **重试必须发生在退回 AT 模式之前**——否则会先选中 AT 驱动去连一个跑着桥接脚本的口，
    表现就是界面报「无 AT 响应」。
    """
    from concurrent.futures import ThreadPoolExecutor

    from .luatos import probe_bridge_info

    explicit = explicit_ports()
    if explicit:
        # 用户指定了端口：只认这些口，探不到也照样返回，让连接阶段报出明确错误
        with ThreadPoolExecutor(max_workers=min(4, len(explicit))) as pool:
            infos = list(pool.map(lambda p: probe_bridge_info(p, 2.0), explicit))
        return [
            FoundDevice(port=p, group=p, kind="luatos" if info.get("ok") else "at",
                        imei=str(info.get("imei") or ""))
            for p, info in zip(explicit, infos)
        ]

    groups = candidate_groups()
    if not groups:
        return []

    # (group, port) 全量展开，组间、组内并行
    tasks: list[tuple[str, str]] = []
    for group, ports in groups:
        for port in ports:
            tasks.append((group, port))

    for attempt in range(max(1, attempts)):
        with ThreadPoolExecutor(max_workers=min(8, len(tasks))) as pool:
            infos = list(pool.map(lambda item: probe_bridge_info(item[1], 2.0), tasks))

        found = _collapse(tasks, infos)
        if found:
            return found
        if attempt + 1 < max(1, attempts):
            time.sleep(gap)

    # 重试完仍没有任何口回 +PONG：模块大概率跑的是 AT/其它固件（正常启动的桥接脚本不会这样）
    from .modem import Modem

    fallback = Modem.candidate_ports()
    if fallback:
        return [FoundDevice(port=fallback[0], group=groups[0][0], kind="at")]
    return []
