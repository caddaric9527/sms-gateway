"""Air780EPM / 通用 AT 串口驱动。

设计要点：
  - 串口访问全部串行化（单锁），上层用线程池调用，不阻塞事件循环
  - 每次命令读取到明确的终止符（OK / ERROR / +CMS ERROR / '>'）为止
  - 命令期间出现的 URC（+CMTI 等）单独缓存，不当成响应内容
  - MODEM_MOCK=1 时使用 MockModem，无硬件也能验证整条链路
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

from .config import settings
from .pdu import DecodedSms, build_submit_pdus, decode_pdu, pdu_length

try:
    import serial
    from serial.tools import list_ports
    HAS_SERIAL = True
except ImportError:  # 允许无 pyserial 环境下跑 mock
    serial = None  # type: ignore
    list_ports = None  # type: ignore
    HAS_SERIAL = False


TRACE = False  # 打开后把 AT 交互打到 stdout，排障用


def port_error_hint(exc: BaseException) -> str:
    """串口打不开时补一句人能直接照做的结论。

    `[Errno 1] Operation not permitted` 看着像权限位问题，实际是**内核 device cgroup
    没放行设备**（权限位不够会报 Errno 13 EACCES）。在容器里十次有九次是 compose 没给
    `privileged: true`，改文件权限、加 dialout 组都没用，所以要直接说清楚。
    """
    err_no = getattr(exc, "errno", None)
    text = str(exc).lower()
    if err_no == 1 or "operation not permitted" in text:
        return "（容器未放行串口设备：docker-compose.yml 设 privileged: true 后 docker compose up -d --force-recreate）"
    if err_no == 13 or "permission denied" in text:
        return "（当前用户无串口权限：加入 dialout 组重新登录，或让容器以 privileged/root 运行）"
    if "device busy" in text or "resource busy" in text:
        return "（串口被占用：宿主机的 ModemManager / 其它进程正在用这个口，停掉后重试）"
    return ""

URC_PATTERNS = (
    re.compile(r"^\+CMTI:"),
    re.compile(r"^\+CMT:"),
    re.compile(r"^RING"),
    re.compile(r"^\+CRING"),
    re.compile(r"^\+CUSD:"),
    re.compile(r"^\+CDS:"),
)

FINAL_OK = re.compile(r"^OK$")
FINAL_ERR = re.compile(r"^(ERROR|\+CM[SE] ERROR:.*)$")


@dataclass
class AtResult:
    ok: bool
    lines: list[str] = field(default_factory=list)
    error: str = ""
    raw: str = ""

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class SmsEntry:
    index: int
    status: int
    pdu: str
    decoded: DecodedSms | None = None


@dataclass
class SendResult:
    ok: bool
    error: str = ""
    parts: int = 0
    message_refs: list[int] = field(default_factory=list)


class ModemError(Exception):
    pass


# --------------------------------------------------------------------------- #
# 真实串口设备
# --------------------------------------------------------------------------- #
class Modem:
    def __init__(self, port: str | None = None) -> None:
        self.ser = None
        self.port: str = ""
        # 锁定端口，避免多设备时抢口
        self._fixed_port: str | None = port
        self.lock = threading.RLock()
        self.pending_urc: list[str] = []
        self.new_sms_count = 0
        self._pin_attempts = 0
        self.last_error = ""
        self.connected = False
        self.info: dict[str, str] = {}
        self._buf = ""

    # ------------------------------------------------------------------ 基础
    def _log(self, msg: str) -> None:
        if TRACE:
            print(f"[modem] {msg}", flush=True)

    def _write(self, data: bytes) -> None:
        if not self.ser:
            raise ModemError("串口未打开")
        self._log(f">>> {data!r}")
        self.ser.write(data)
        self.ser.flush()

    def _drain(self) -> None:
        """清掉缓冲区里的残留，避免旧响应污染新命令。"""
        if not self.ser:
            return
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        self._buf = ""

    def _read_block(self, timeout: float, want_prompt: bool = False) -> tuple[list[str], bool, bool]:
        """读到终止符。返回 (完整行列表, 是否成功终止, 是否遇到错误)。"""
        if not self.ser:
            return [], False, False
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        ok = False
        err = False
        while time.monotonic() < deadline:
            try:
                waiting = self.ser.in_waiting
                chunk = self.ser.read(waiting if waiting else 1)
            except Exception as exc:  # 串口掉线
                self.connected = False
                self.last_error = f"串口读取失败: {exc}"
                return lines, False, True
            if chunk:
                self._buf += chunk.decode("utf-8", errors="replace")
                # 统一换行
                while True:
                    idx = self._buf.find("\n")
                    if idx < 0:
                        break
                    line = self._buf[: idx + 1].replace("\r", "").strip()
                    self._buf = self._buf[idx + 1 :]
                    if line:
                        self._log(f"<<< {line}")
                        lines.append(line)
                        if FINAL_OK.match(line):
                            ok = True
                            return lines, ok, err
                        if FINAL_ERR.match(line):
                            err = True
                            return lines, ok, err
                if want_prompt and self._buf.strip().endswith(">"):
                    self._buf = ""
                    return lines, True, False
        return lines, ok, err

    def command(self, cmd: str, timeout: float = 5.0, want_prompt: bool = False,
                accept_error: bool = False) -> AtResult:
        """发送 AT 命令。

        accept_error=True 时，收到 ERROR/+CME ERROR 也算「设备有响应」，
        用于握手探测——部分合宙固件对裸 AT 直接回 ERROR。
        """
        with self.lock:
            self._drain()
            try:
                self._write(cmd.encode() + b"\r")
            except Exception as exc:
                self.connected = False
                return AtResult(False, error=f"写入失败: {exc}")
            lines, ok, err = self._read_block(timeout, want_prompt=want_prompt)
            # URC 分流
            kept = []
            for line in lines:
                if any(p.match(line) for p in URC_PATTERNS):
                    self.pending_urc.append(line)
                    if line.startswith("+CMTI:"):
                        self.new_sms_count += 1
                else:
                    kept.append(line)
            error = ""
            if err:
                error = next((ln for ln in kept if FINAL_ERR.match(ln)), "ERROR")
                self.last_error = f"{cmd} -> {error}"
            elif not ok:
                error = "超时无响应"
                self.last_error = f"{cmd} -> {error}"
            if accept_error and err and not ok:
                return AtResult(True, kept, error, "\n".join(lines))
            return AtResult(ok and not err, kept, error, "\n".join(lines))

    # ------------------------------------------------------------------ 连接
    @staticmethod
    def candidate_ports() -> list[str]:
        if not HAS_SERIAL or list_ports is None:
            return []
        scored: list[tuple[int, str]] = []
        try:
            for p in list_ports.comports():
                score = 0
                vid = getattr(p, "vid", None)
                if vid in (0x19D1, 0x1782):  # 合宙 Luat（19D1 为 EC618/EC718 系列新 VID）
                    score += 100
                if vid in (0x2E8A, 0x1A86, 0x10C4, 0x0403):
                    score += 20
                if "ttyUSB" in p.device:
                    score += 10
                elif "ttyACM" in p.device:
                    score += 8
                elif p.device.startswith("COM"):
                    score += 5
                scored.append((score, p.device))
        except Exception:
            pass

        # 兜底：直接扫设备节点
        import glob as _glob
        import os as _os

        if _os.name != "nt":
            for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/serial/by-id/*"):
                for dev in sorted(_glob.glob(pattern)):
                    if not any(d == dev for _, d in scored):
                        scored.append((1, dev))

        scored.sort(key=lambda x: (-x[0], x[1]))
        # 已经认出模块厂商 VID 时，不要再浪费时间去敲主板自带口/蓝牙串口
        # （Windows 上 COM1/COM3/COM4 这类无 VID 的口每个要空等 2 秒以上）
        if any(s >= 100 for s, _ in scored):
            scored = [(s, d) for s, d in scored if s >= 20]
        return [d for _, d in scored]

    def _open_port(self, port: str) -> bool:
        if not HAS_SERIAL:
            self.last_error = "未安装 pyserial"
            return False
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = settings.modem_baud or 115200
            ser.timeout = 0.05
            ser.write_timeout = 15
            ser.open()
            self.ser = ser
            self.port = port
            time.sleep(0.2)
            # CDC-ACM 口（/dev/ttyACM*）不拉 DTR 可能收不到数据
            try:
                ser.dtr = True
                ser.rts = True
            except Exception:
                pass
            # 握手：部分合宙固件对裸 AT 回 ERROR，只要有响应就说明是 AT 口。
            # 每个口最多两次，避免对日志口/串口空等拖慢启动。
            for cmd in ("AT", "ATE0"):
                res = self.command(cmd, timeout=1.2, accept_error=True)
                if res.ok:
                    self.connected = True
                    if cmd != "ATE0":
                        self.command("ATE0", timeout=1.2, accept_error=True)
                    return True
                if not res.raw:  # 完全没数据，这个口不是 AT 口，直接放弃
                    break
            self.close()
            self.last_error = f"{port} 无 AT 响应"
            return False
        except Exception as exc:
            self.last_error = f"{port} 打开失败: {exc}{port_error_hint(exc)}"
            self.close()
            return False

    def open(self) -> bool:
        with self.lock:
            if self.connected and self.ser:
                return True
            if self._fixed_port:
                ports = [self._fixed_port]
            else:
                ports = [settings.modem_port] if settings.modem_port not in ("", "auto") else []
                if not ports:
                    ports = self.candidate_ports()
            if not ports:
                self.last_error = "未发现可用串口，请检查 USB 连接或 MODEM_PORT 配置"
                return False
            for port in ports:
                if self._open_port(port):
                    if self.init_sequence():
                        return True
                    self.close()
            return False

    def init_sequence(self) -> bool:
        with self.lock:
            self.command("ATE0", timeout=2, accept_error=True)
            self.command("AT+CMEE=1", timeout=2, accept_error=True)
            res = self.command("AT+CMGF=0", timeout=2)     # PDU 模式
            self.info["sms_mode"] = "pdu" if res.ok else "unavailable"
            if not res.ok:
                # 短信命令不可用：多半是固件没注册 GSM 07.05 命令集
                # （例如合宙出厂 airlink demo 只注册了十几个查询命令）。
                # AT+HELP 能列出当前固件真正支持的命令，直接作为证据留存。
                help_res = self.command("AT+HELP", timeout=3, accept_error=True)
                cmds = [ln.strip().split()[0] for ln in help_res.lines if ln.strip().startswith("AT")]
                if cmds:
                    self.info["at_commands"] = cmds
                    self.info["sms_capable"] = any(
                        c.upper().startswith(("AT+CMG", "AT+CMGR", "AT+CMGL")) for c in cmds
                    )
                else:
                    self.info["sms_capable"] = False
            else:
                self.info["sms_capable"] = True
            st = settings.sms_storage or "SM"
            res = self.command(f'AT+CPMS="{st}","{st}","{st}"', timeout=3)
            if not res.ok and st != "ME":
                # 部分固件/SIM 不支持把 SM 作为读写存储，回退到模块存储
                if self.command('AT+CPMS="ME","ME","ME"', timeout=3).ok:
                    self.info["storage"] = "ME"
                else:
                    self.info["storage"] = st
            else:
                self.info["storage"] = st
            self.command("AT+CNMI=2,1,0,0,0", timeout=3, accept_error=True)  # 新短信 URC
            # 采集设备信息（CCID 只作为补充，失败不影响主流程）
            for key, cmd in (
                ("manufacturer", "AT+CGMI"),
                ("model", "AT+CGMM"),
                ("firmware", "AT+CGMR"),
                ("imei", "AT+CGSN"),
            ):
                res = self.command(cmd, timeout=3, accept_error=True)
                val = " ".join(
                    ln for ln in res.lines if not FINAL_OK.match(ln) and not FINAL_ERR.match(ln)
                ).strip()
                if val:
                    m = re.search(r'"([^"]+)"', val)
                    self.info[key] = m.group(1) if m else val
            self.last_error = ""
            return True

    def close(self) -> None:
        with self.lock:
            self.connected = False
            if self.ser:
                try:
                    self.ser.close()
                except Exception:
                    pass
            self.ser = None

    def ensure(self) -> bool:
        if self.connected and self.ser:
            return True
        return self.open()

    # ------------------------------------------------------------------ 状态
    def read_status(self) -> dict:
        st: dict = {
            "connected": False,
            "port": self.port,
            "signal": None,
            "signal_percent": None,
            "registered": False,
            "network_known": False,
            "operator": "",
            "sim_ready": False,
            "sim_status": "unknown",
            "iccid": "",
            "number": "",
            "sms_mode": self.info.get("sms_mode", ""),
            "sms_capable": self.info.get("sms_capable"),
            "info": dict(self.info),
            "error": "",
            "hint": "",
        }
        if not self.ensure():
            st["error"] = self.last_error
            return st
        st["connected"] = True
        # ensure() 可能刚刚完成连接，此处重新取值，避免拿到连接前的空值
        st["port"] = self.port
        st["info"] = dict(self.info)
        st["sms_mode"] = self.info.get("sms_mode", "")
        st["sms_capable"] = self.info.get("sms_capable")

        res = self.command("AT+CSQ", timeout=3)
        m = re.search(r"\+CSQ:\s*(\d+)\s*,\s*(\d+)", res.text)
        if m:
            rssi = int(m.group(1))
            st["signal"] = rssi if rssi != 99 else None
            st["signal_percent"] = min(100, max(0, round(rssi / 31 * 100))) if rssi != 99 else None

        res = self.command("AT+CREG?", timeout=3, accept_error=True)
        m = re.search(r"\+CREG:\s*\d+\s*,\s*(\d+)", res.text)
        if m:
            st["registered"] = m.group(1) in ("1", "5")
            st["network_known"] = True

        res = self.command("AT+COPS?", timeout=5, accept_error=True)
        m = re.search(r'\+COPS:\s*\d+\s*,\s*\d+\s*,"([^"]*)"', res.text)
        if m:
            st["operator"] = m.group(1)
            st["network_known"] = True

        res = self.command("AT+CPIN?", timeout=3, accept_error=True)
        st["sim_ready"] = "READY" in res.text
        # 部分固件没实现 AT+CPIN?（回 ERROR），用 IMSI 反查 SIM 是否就位
        r2 = self.command("AT+CIMI", timeout=3, accept_error=True)
        m = re.search(r"(\d{14,16})", r2.text)
        if m:
            st["sim_ready"] = True
            st["imsi"] = m.group(1)
        if st["sim_ready"]:
            st["sim_status"] = "ok"
        elif "+CME ERROR: 10" in r2.text:
            st["sim_status"] = "not_inserted"
        elif "+CME ERROR: 11" in r2.text:
            st["sim_status"] = "pin_required"
        elif "+CME ERROR: 13" in r2.text:
            st["sim_status"] = "sim_failure"
        else:
            st["sim_status"] = "unknown"

        # SIM 设了 PIN：用 SIM_PIN 解锁。同样限次，错 3 次卡会被锁。
        if st["sim_status"] == "pin_required" and settings.sim_pin and self._pin_attempts < 2:
            self._pin_attempts += 1
            r = self.command(f'AT+CPIN="{settings.sim_pin}"', timeout=8, accept_error=True)
            if r.ok and "ERROR" not in r.text:
                self._pin_attempts = 0
                time.sleep(1.0)
                res = self.command("AT+CPIN?", timeout=3, accept_error=True)
                st["sim_ready"] = "READY" in res.text
                st["sim_status"] = "ok" if st["sim_ready"] else "pin_required"
            else:
                self.last_error = f"SIM PIN 验证失败（第 {self._pin_attempts} 次）"

        if st["sim_ready"]:
            # ICCID 由 CCID/ICCID 两条命令任一条拿到即可
            res = self.command("AT+CCID", timeout=3, accept_error=True)
            m = re.search(r"(\d{18,22})", res.text)
            if m:
                st["iccid"] = m.group(1)
            else:
                res = self.command("AT+ICCID", timeout=3, accept_error=True)
                m = re.search(r"(\d{18,22})", res.text)
                if m:
                    st["iccid"] = m.group(1)
            res = self.command("AT+CNUM", timeout=3, accept_error=True)
            m = re.search(r'\+CNUM:\s*"[^"]*","([^"]*)"', res.text)
            if m:
                st["number"] = m.group(1)

        # 给出可执行的提示，前端直接展示（越致命越优先）
        if st["sms_capable"] is False:
            st["hint"] = "固件没有短信能力，请烧录 firmware/main.lua（见 README 1.6）"
        elif st["sim_status"] == "not_inserted":
            st["hint"] = "SIM 卡未插入，请插卡后重连"
        elif st["sim_status"] == "pin_required":
            if settings.sim_pin:
                st["hint"] = "SIM PIN 未通过，核对 .env 里的 SIM_PIN（错 3 次卡会被锁）"
            else:
                st["hint"] = "SIM 卡设了 PIN，请在 .env 里配置 SIM_PIN"
        elif st.get("network_known") and not st["registered"]:
            st["hint"] = "尚未注册网络，检查 SIM 卡与天线"
        elif st["signal"] is not None and st["signal"] <= 5:
            st["hint"] = "信号很弱，检查天线"
        st["error"] = self.last_error if (not st["registered"] and not st["hint"]) else ""
        return st

    # ------------------------------------------------------------------ 短信读取
    def list_sms(self, stat: int = 4) -> list[SmsEntry]:
        if not self.ensure():
            raise ModemError(self.last_error or "模块不可用")
        res = self.command(f"AT+CMGL={stat}", timeout=45)
        if not res.ok:
            # 存储为空时部分固件回 ERROR，视为空列表
            if "ERROR" in res.error:
                return []
            raise ModemError(f"读取短信失败: {res.error}")

        entries: list[SmsEntry] = []
        idx: int | None = None
        stat_val = 0
        for line in res.lines:
            m = re.match(r"^\+CMGL:\s*(\d+)\s*,\s*(\d+)", line)
            if m:
                idx = int(m.group(1))
                stat_val = int(m.group(2))
                continue
            if idx is None:
                continue
            pdu = line.strip()
            if not re.fullmatch(r"[0-9A-Fa-f]{10,}", pdu):
                continue
            try:
                decoded = decode_pdu(pdu)
            except Exception as exc:
                self.last_error = f"PDU 解析失败({pdu[:32]}...): {exc}"
                decoded = None
            entries.append(SmsEntry(index=idx, status=stat_val, pdu=pdu.upper(), decoded=decoded))
            idx = None
        return entries

    def delete_sms(self, index: int) -> bool:
        if not self.ensure():
            return False
        return self.command(f"AT+CMGD={index}", timeout=10).ok

    def delete_all_sms(self) -> bool:
        if not self.ensure():
            return False
        res = self.command('AT+CMGDA="DEL ALL"', timeout=20)
        return res.ok

    # ------------------------------------------------------------------ 短信发送
    def send_sms(self, peer: str, text: str) -> SendResult:
        if not self.ensure():
            return SendResult(False, self.last_error or "模块不可用")
        if not text.strip():
            return SendResult(False, "内容为空")
        if self.info.get("sms_capable") is False:
            return SendResult(False, "固件没有短信能力，请先烧录 firmware/main.lua")

        pdus = build_submit_pdus(peer, text)
        refs: list[int] = []
        for i, pdu in enumerate(pdus):
            with self.lock:
                self._drain()
                tpdu_len = pdu_length(pdu)
                try:
                    self._write(f"AT+CMGS={tpdu_len}\r".encode())
                except Exception as exc:
                    return SendResult(False, f"写入失败: {exc}", i, refs)
                lines, got_prompt, _ = self._read_block(6.0, want_prompt=True)
                if not got_prompt:
                    return SendResult(False, "模块未返回 > 提示符（可能信号不足或短信中心未配置）", i, refs)
                try:
                    self._write(pdu.encode() + b"\x1a")
                except Exception as exc:
                    return SendResult(False, f"写入 PDU 失败: {exc}", i, refs)
                res_lines, ok, err = self._read_block(60.0)
                if err:
                    msg = next((ln for ln in res_lines if FINAL_ERR.match(ln)), "ERROR")
                    return SendResult(False, f"发送被拒绝: {msg}", i, refs)
                if not ok:
                    return SendResult(False, "发送超时，未收到确认", i, refs)
                for ln in res_lines:
                    m = re.search(r"\+CMGS:\s*(\d+)", ln)
                    if m:
                        refs.append(int(m.group(1)))
            if i < len(pdus) - 1:
                time.sleep(1.2)
        return SendResult(True, "", len(pdus), refs)


# --------------------------------------------------------------------------- #
# Mock 设备：无硬件时验证界面与转发链路
# --------------------------------------------------------------------------- #
class MockModem(Modem):
    """模拟模块。传入序号可模拟多台设备（用于多设备联调）。"""

    def __init__(self, slot: int = 1) -> None:
        super().__init__()
        self.slot = max(1, int(slot))
        self.port = f"mock-{self.slot}"
        self.connected = True
        self.device_id = f"86000000000{self.slot:04d}"      # 造的 IMEI，当作设备主键
        self.mock_name = f"mock-{self.slot}"
        self.info = {
            "manufacturer": "Luat",
            "model": "Air780EPM (mock)",
            "firmware": "mock-1.0",
            "imei": self.device_id,
        }
        self._seeded = False
        self._index = 100 * self.slot

    def open(self) -> bool:
        self.connected = True
        return True

    def ensure(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def init_sequence(self) -> bool:
        return True

    def _seed(self) -> None:
        if self._seeded:
            return
        self._seeded = True
        from . import db  # 延迟导入，避免循环

        seeded = [
            ("+8613800138000", "【中国移动】您本月套餐内流量已使用 80%，剩余 4.2GB。回复 TD 退订。", -1800),
            ("+8613912345678", "晚上一起吃饭吗？", -900),
            ("1069000000", "【某银行】您的验证码为 394827，5 分钟内有效。请勿泄露。", -300),
        ]
        for peer, text, offset in seeded:
            from datetime import datetime, timedelta, timezone

            ts = (datetime.now(timezone.utc).astimezone() + timedelta(seconds=offset)).isoformat(timespec="seconds")
            self._index += 1
            db.insert_message({
                "direction": "in",
                "peer": peer,
                "text": text,
                "ts": ts,
                "encoding": "ucs2" if any("\u4e00" <= c <= "\u9fff" for c in text) else "gsm7",
                "raw_pdu": "MOCK",
                "modem_index": self._index,
                "status": "received",
                "device_id": self.device_id,
                "fingerprint": f"mock-{self.slot}-{peer}-{offset}",
            })

    def read_status(self) -> dict:
        self._seed()
        return {
            "connected": True,
            "port": self.port,
            "signal": 24,
            "signal_percent": 77,
            "registered": True,
            "operator": "CHINA MOBILE",
            "sim_ready": True,
            "sim_status": "ok",
            "iccid": f"8986001234567890{self.slot:04d}",
            "number": f"+8613800138{self.slot:03d}",
            "sms_mode": "pdu",
            "info": dict(self.info),
            "error": "",
            "hint": "",
            "mock": True,
        }

    def list_sms(self, stat: int = 4) -> list[SmsEntry]:
        self._seed()
        if self.new_sms_count:
            self.new_sms_count = 0
            from datetime import datetime, timezone
            from . import db

            self._index += 1
            text = f"模拟新短信 {datetime.now().strftime('%H:%M:%S')}"
            db.insert_message({
                "direction": "in",
                "peer": "1065500000",
                "text": text,
                "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
                "encoding": "ucs2",
                "raw_pdu": "MOCK",
                "modem_index": self._index,
                "status": "received",
                "device_id": self.device_id,
                "fingerprint": f"mock-new-{self.slot}-{self._index}",
            })
        return []

    def inject(self, peer: str, text: str) -> None:
        """测试用：注入一条新短信。"""
        from datetime import datetime, timezone
        from . import db

        self._index += 1
        db.insert_message({
            "direction": "in",
            "peer": peer,
            "text": text,
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "encoding": "ucs2",
            "raw_pdu": "MOCK",
            "modem_index": self._index,
            "status": "received",
            "device_id": self.device_id,
            "fingerprint": f"mock-inject-{self.slot}-{self._index}",
        })

    def delete_sms(self, index: int) -> bool:
        return True

    def delete_all_sms(self) -> bool:
        return True

    def send_sms(self, peer: str, text: str) -> SendResult:
        self._seed()
        time.sleep(0.4)
        if not peer.strip():
            return SendResult(False, "号码为空")
        parts = len(build_submit_pdus(peer, text))
        return SendResult(True, "", parts, [999])


def create_modem(slot: int = 1) -> Modem:
    """按配置直接实例化驱动；auto 模式先给 AT，真正探测放到后台 bootstrap。"""
    if settings.modem_mock or not HAS_SERIAL:
        return MockModem(slot)
    protocol = (settings.modem_protocol or "auto").lower()
    if protocol in ("luatos", "bridge"):
        from .luatos import LuatosModem
        return LuatosModem()
    return Modem()


def detect_modem() -> Modem:
    """探测模块上跑的是哪种固件，返回匹配的驱动。会访问串口，务必放线程里跑。"""
    if settings.modem_mock or not HAS_SERIAL:
        return MockModem()
    protocol = (settings.modem_protocol or "auto").lower()
    if protocol in ("at", "luatos", "bridge"):
        return create_modem()

    from .luatos import LuatosModem, probe_bridge
    candidates = [settings.modem_port] if settings.modem_port not in ("", "auto") else Modem.candidate_ports()
    # 并行探测：USB 复合设备会同时暴露 3 个串口，串行各等 2 秒会拖到启动 10 秒以上
    candidates = candidates[:6]
    if not candidates:
        return Modem()
    from concurrent.futures import ThreadPoolExecutor

    found = False
    with ThreadPoolExecutor(max_workers=min(4, len(candidates))) as pool:
        futures = [pool.submit(probe_bridge, port, 1.5) for port in candidates]
        for fut in futures:
            try:
                if fut.result():
                    found = True
            except Exception:
                pass
    return LuatosModem() if found else Modem()
