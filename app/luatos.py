"""LuatOS 桥接驱动：与模块上的 firmware/main.lua 对话。

为什么需要它：Air780EPM 只支持 LuatOS 二次开发，没有标准 GSM 07.05 短信指令，
所以短信收发在模块侧完成，上位机通过 USB 虚拟串口用「文本行 + JSON」协议交互。
对上层（service / forwarder）而言，接口与 AT 驱动完全一致。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime

from .config import settings
from .modem import Modem, SendResult, SmsEntry, port_error_hint
from .operator import operator_from_imsi
from .pdu import DecodedSms

try:
    import serial
    from serial.tools import list_ports  # noqa: F401
    HAS_SERIAL = True
except ImportError:
    serial = None  # type: ignore
    HAS_SERIAL = False


# SIM 状态要连续几轮观测一致才承认「恢复就绪」：模块正忙（例如发长短信）时
# 单次 STATUS 偶发读到空 ICCID 很常见，不能据此判定卡已插好并发放新的 PIN 预算。
SIM_SETTLE_POLLS = 2


def _now_iso(epoch: float | int | None = None) -> str:
    if epoch:
        try:
            return datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")
        except (ValueError, OSError, OverflowError):
            pass
    return datetime.now().astimezone().isoformat(timespec="seconds")


class LuatosModem:
    """模块主动推送短信；上位机下发指令。"""

    def __init__(self, port: str | None = None) -> None:
        self.ser = None
        self.port: str = ""
        # 多设备场景必须锁定端口：否则第二台会去抢第一台已经占用的口，
        # 表现为「两台只连上一台」。None 表示沿用旧的自动选口行为。
        self._fixed_port: str | None = port
        self.lock = threading.RLock()
        self.new_sms_count = 0
        self.last_error = ""
        self.connected = False
        self.info: dict = {"mode": "luatos", "sms_capable": True}

        self._queue: list[SmsEntry] = []
        self._queue_lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._events: dict[str, threading.Event] = {}
        self._reader: threading.Thread | None = None
        self._stop = threading.Event()
        self._buf = ""
        self._seq = 0
        self._last_status: dict = {}
        self._pin_attempts = 0
        # SIM 观察窗口：把 PIN 重试预算绑定到「一张卡的一次出现」，而不是整个进程生命周期。
        # 否则热插拔一次（或模块重启一次）就把 2 次预算烧光，之后再也解不开卡。
        self._pin_blocked = False      # 卡曾就绪又离位 → 暂停自动验证，等卡回来或模块重启
        self._sim_ready_streak = 0     # 连续「就绪」轮数
        self._sim_lost_streak = 0      # 连续「未就绪」轮数
        self._sim_epoch_iccid = ""     # 本窗口观测到的 ICCID，用于识别换卡
        self._ever_ready = False       # 本窗口内是否见过 SIM 就绪
        self._sim_events: list[str] = []   # 待上行的 SIM 状态变化，由 read_status 取走

    # ------------------------------------------------------------------ 串口
    def _write_line(self, line: str) -> None:
        if not self.ser:
            raise RuntimeError("串口未打开")
        self.ser.write(line.encode("utf-8") + b"\r\n")
        self.ser.flush()

    def _event(self, tag: str) -> threading.Event:
        ev = self._events.get(tag)
        if ev is None:
            ev = threading.Event()
            self._events[tag] = ev
        return ev

    def _request(self, line: str, tag: str, timeout: float) -> dict | None:
        with self.lock:
            ev = self._event(tag)
            ev.clear()
            self._cache.pop(tag, None)
            try:
                self._write_line(line)
            except Exception as exc:
                self.connected = False
                self.last_error = f"写入失败: {exc}"
                return None
            return self._cache.get(tag) if ev.wait(timeout) else None

    def open(self) -> bool:
        with self.lock:
            if self.connected and self.ser:
                return True
            if self._fixed_port:
                ports = [self._fixed_port]
            else:
                ports = [settings.modem_port] if settings.modem_port not in ("", "auto") else []
                if not ports:
                    ports = Modem.candidate_ports()
            if not ports:
                self.last_error = "未发现可用串口，请检查 USB 连接或 MODEM_PORT 配置"
                return False
            for port in ports:
                if self._open_port(port):
                    return True
            return False

    def _open_port(self, port: str) -> bool:
        if not HAS_SERIAL:
            self.last_error = "未安装 pyserial"
            return False
        try:
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = settings.modem_baud or 115200
            ser.timeout = 0.1
            ser.write_timeout = 10
            ser.open()
        except Exception as exc:
            self.last_error = f"{port} 打开失败: {exc}{port_error_hint(exc)}"
            return False

        self.ser = ser
        self.port = port
        try:
            ser.dtr = True
            ser.rts = True
        except Exception:
            pass
        self._stop.clear()
        self._buf = ""
        self._reader = threading.Thread(target=self._reader_loop, name="luatos-rx", daemon=True)
        self._reader.start()

        # 握手：脚本只认 PING；AT 命令在这里会得到 +ERR 或没反应
        for _ in range(3):
            payload = self._request("PING", "+PONG", 2.0)
            if payload is not None:
                self.connected = True
                self.last_error = ""
                st = self._request("STATUS", "+STATUS", 5.0)
                if st:
                    self._last_status = st
                self.info["bridge"] = "online"
                return True
            time.sleep(0.3)

        self.close()
        self.last_error = f"{port} 未响应 PING（模块脚本未运行或固件不含 sms 库）"
        return False

    def close(self) -> None:
        self._stop.set()
        reader = self._reader
        self._reader = None
        if reader and reader.is_alive():
            reader.join(timeout=1.5)
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

    # ------------------------------------------------------------------ 收数据
    def _reader_loop(self) -> None:
        while not self._stop.is_set():
            ser = self.ser
            if not ser:
                return
            try:
                chunk = ser.read(4096)
            except Exception as exc:
                self.connected = False
                self.last_error = f"串口读取失败: {exc}"
                return
            if not chunk:
                continue
            self._buf += chunk.decode("utf-8", "replace")
            if len(self._buf) > 65536:      # 防脏数据把内存吃光
                self._buf = ""
                continue
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip("\r\n\t ")
                if line:
                    self._on_line(line)

    def _on_line(self, line: str) -> None:
        if not line.startswith("+"):
            return
        head, _, body = line.partition(" ")
        head = head.upper()
        payload: dict = {}
        if body.strip():
            try:
                parsed = json.loads(body)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {}

        if head == "+SMS":
            self._enqueue_sms(payload)
            return

        if head == "+BOOT":
            # 固件脚本重跑 = 模块重启过：这是全新的一次 SIM 机会，预算归零
            self._reset_sim_epoch("检测到模块重启")

        if head in ("+STATUS", "+HB", "+READY") and payload:
            self._last_status = payload
            if head == "+READY":
                self.info["firmware"] = payload.get("fw") or self.info.get("firmware", "")
                self.info["model"] = payload.get("bsp") or self.info.get("model", "")

        self._cache[head] = payload if payload else {"_raw": body}
        self._event(head).set()

    def _enqueue_sms(self, payload: dict) -> None:
        text = str(payload.get("txt") or "")
        sender = str(payload.get("num") or "").strip() or "unknown"
        ts = payload.get("ts")
        decoded = DecodedSms(
            kind="deliver",
            sender=sender,
            text=text,
            timestamp=_now_iso(ts) if ts else _now_iso(),
            encoding="ucs2" if any("\u4e00" <= ch <= "\u9fff" for ch in text) else "gsm7",
            raw=json.dumps(payload, ensure_ascii=False),
        )
        with self._queue_lock:
            self._queue.append(SmsEntry(index=0, status=0, pdu="", decoded=decoded))
            self.new_sms_count += 1

    # ------------------------------------------------------------------ SIM PIN
    def _note_sim(self, msg: str) -> None:
        self._sim_events.append(msg)
        del self._sim_events[:-4]      # 上限 4 条，没人取也不会无限增长

    def _reset_sim_epoch(self, reason: str = "") -> None:
        """开启新的 SIM 观察窗口，把 PIN 重试预算归零。

        PIN 连续错 3 次卡会被锁死（只能用 PUK 解），所以预算必须小；但预算也不能
        跟着进程一辈子 —— 模块重启、换卡、卡重新插好，都该算一次新的机会。
        """
        used = self._pin_attempts
        was_blocked = self._pin_blocked
        self._pin_attempts = 0
        self._pin_blocked = False
        self._sim_ready_streak = 0
        self._sim_lost_streak = 0
        self._sim_epoch_iccid = ""
        self._ever_ready = False
        if reason and (used or was_blocked):
            self._note_sim(f"{reason}，SIM PIN 重试预算已重置")

    def _observe_sim(self, sim_ready: bool, iccid: str) -> None:
        """按连续 STATUS 观测识别「卡离位 / 卡回来 / 换卡」，据此管理 PIN 预算。

        只有拔卡和「PIN 未验证」两种情况的 sim 都是 false，光看一轮无法区分，
        所以要结合历史：曾经就绪、现在读不到卡 ⇒ 认定为离位，立刻停手，
        否则下一轮就会对着空卡白烧一次 SETPIN 预算。
        """
        if sim_ready:
            self._sim_lost_streak = 0
            self._sim_ready_streak += 1

            # 换卡：ICCID 变了就是另一张卡，预算重算
            if iccid and self._sim_epoch_iccid and iccid != self._sim_epoch_iccid:
                old, new = self._sim_epoch_iccid, iccid
                self._reset_sim_epoch(f"检测到换卡（…{old[-6:]} → …{new[-6:]}）")
                self._sim_ready_streak = SIM_SETTLE_POLLS
            if iccid:
                self._sim_epoch_iccid = iccid

            # 卡离位后重新就绪：连续 N 轮才算数，防单次抖动误判
            if self._pin_blocked and self._sim_ready_streak >= SIM_SETTLE_POLLS:
                self._reset_sim_epoch("SIM 卡恢复就绪")
                self._sim_ready_streak = SIM_SETTLE_POLLS
                self._sim_epoch_iccid = iccid
            self._ever_ready = True
            return

        self._sim_ready_streak = 0
        self._sim_lost_streak += 1
        if self._ever_ready and not self._pin_blocked:
            used = self._pin_attempts
            self._pin_blocked = True
            self._pin_attempts = 0
            self._sim_epoch_iccid = ""
            tail = f"，离位前已用 {used} 次" if used else ""
            self._note_sim(f"SIM 卡离位，已暂停自动 PIN 验证{tail}")

    def _try_unlock_pin(self) -> None:
        """SIM 明确未就绪时下发一次 SETPIN（需要固件 1.1.1+）。

        三道保险：
        1. 只在 `+STATUS` 明确回报 sim=false 时才动手，状态未知绝不猜；
        2. 卡曾就绪又离位（_pin_blocked）时不发，避免对着空卡烧掉预算；
        3. 每个 SIM 观察窗口最多 2 次，因为 PIN 连续错 3 次卡会被锁，只能用 PUK 解。
        """
        pin = settings.sim_pin
        if not pin or self._pin_attempts >= 2 or self._pin_blocked:
            return
        if (self._last_status or {}).get("sim") is not False:
            return

        self._pin_attempts += 1
        body = json.dumps({"pin": pin}, ensure_ascii=False)
        payload = self._request(f"SETPIN {body}", "+PIN", 12.0)
        if payload is None:
            self.last_error = "模块未响应 SETPIN（固件需 1.1.1 及以上）"
            return
        if payload.get("ok"):
            self.last_error = ""
            # 卡解锁后 SIM 初始化需要一点时间，等一会再让上层重新取 STATUS
            time.sleep(2.0)
            self._last_status = {}
        else:
            self.last_error = f"SIM PIN 验证失败（第 {self._pin_attempts} 次）"

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
            "imsi": "",
            "number": "",
            "sms_mode": "pdu",
            "sms_capable": True,
            "info": dict(self.info),
            "error": "",
            "hint": "",
            "protocol": "luatos",
        }
        if not self.ensure():
            st["error"] = self.last_error
            st["info"] = dict(self.info)
            return st

        # SIM 卡设了 PIN 时，卡未解锁就拿不到 ICCID、也注册不上网络。
        # 必须在取 STATUS 之前解锁，这样下面的解析能直接拿到解锁后的状态。
        self._try_unlock_pin()

        payload = self._request("STATUS", "+STATUS", 6.0) or self._last_status
        st["port"] = self.port
        if not payload:
            st["connected"] = True
            st["error"] = "模块无响应（脚本可能已停止）"
            st["info"] = dict(self.info)
            return st

        st["connected"] = True
        csq = payload.get("csq")
        if isinstance(csq, (int, float)):
            st["signal"] = int(csq) if int(csq) > 0 else None
            st["signal_percent"] = min(100, max(0, round(int(csq) / 31 * 100))) if int(csq) > 0 else None

        reg = payload.get("reg")
        if isinstance(reg, (int, float)):
            st["network_known"] = True
            # 1 已注册 / 5 漫游 / 6 仅SMS / 7 仅SMS漫游，都算可用
            st["registered"] = int(reg) in (1, 5, 6, 7)

        st["sim_ready"] = bool(payload.get("sim"))
        st["sim_status"] = "ok" if st["sim_ready"] else "not_inserted"
        if not st["sim_ready"] and self._pin_attempts:
            st["sim_status"] = "pin_required"
        st["iccid"] = str(payload.get("iccid") or "")
        st["imsi"] = str(payload.get("imsi") or "")
        # 先更新 SIM 观察窗口，hint 才能反映「卡离位 / 刚恢复」这类判断
        self._observe_sim(st["sim_ready"], st["iccid"])
        if self._sim_events:
            st["sim_event"] = "；".join(self._sim_events)
            self._sim_events.clear()
        # LuatOS 不提供运营商名称接口，用 IMSI 的 MCC+MNC 反推
        st["operator"] = operator_from_imsi(st["imsi"])
        st["number"] = str(payload.get("num") or "")
        if payload.get("imei"):
            self.info["imei"] = str(payload["imei"])
        if payload.get("fw"):
            self.info["firmware"] = str(payload["fw"])
        if payload.get("bsp"):
            self.info["model"] = str(payload["bsp"])
        st["info"] = dict(self.info)

        if not st["sim_ready"]:
            if self._pin_blocked:
                st["hint"] = ("SIM 卡已离位（或模块重启过），已暂停自动 PIN 验证；"
                              "卡插好后会自动恢复，解不开就重新插拔模块 USB")
            elif settings.sim_pin and self._pin_attempts:
                st["hint"] = "SIM PIN 未通过，核对 .env 里的 SIM_PIN（错 3 次卡会被锁）"
            elif settings.sim_pin:
                st["hint"] = "SIM 卡未就绪，检查插卡"
            else:
                st["hint"] = "SIM 卡未就绪；若这张卡设了 PIN，请在 .env 配置 SIM_PIN"
        elif not st["registered"]:
            st["hint"] = "尚未注册网络，检查 SIM 卡与天线"
        elif not st["signal"]:
            st["hint"] = "无信号，检查天线"
        elif st["signal"] <= 5:
            st["hint"] = "信号很弱，检查天线"
        return st

    # ------------------------------------------------------------------ 短信
    def list_sms(self, stat: int = 4) -> list[SmsEntry]:
        with self._queue_lock:
            items = self._queue[:]
            self._queue.clear()
        return items

    def delete_sms(self, index: int) -> bool:
        return True

    def send_sms(self, peer: str, text: str) -> SendResult:
        if not self.ensure():
            return SendResult(False, self.last_error or "模块不可用")
        if not text.strip():
            return SendResult(False, "内容为空")

        self._seq += 1
        seq = self._seq
        body = json.dumps({"id": seq, "num": peer, "txt": text}, ensure_ascii=False)
        # 长短信在模块侧分片，等待时间放宽
        timeout = 120.0 if len(text.encode("utf-8")) > 140 else 45.0
        payload = self._request(f"SEND {body}", "+SENT", timeout)
        if payload is None:
            return SendResult(False, "模块未回执（超时）")

        if payload.get("id") not in (seq, None):
            # 回执对不上说明是上一条的迟到响应，按失败处理让用户可重发
            return SendResult(False, "回执序号不匹配")

        parts = 1
        raw_len = len(text.encode("utf-8"))
        if raw_len > 140:
            parts = (raw_len + 133) // 134
        if payload.get("ok"):
            return SendResult(True, "", parts, [seq])
        return SendResult(False, str(payload.get("err") or "发送失败"))


# 桥接脚本只要活着，任何一条以 + 开头的协议行都能证明它在跑
# （+HB 心跳、+BOOT 启动、+READY 网络就绪都可能先于 +PONG 到达）
_ALIVE_TOKENS = ("+PONG", "+READY", "+STATUS", "+BOOT", "+HB")


def _ping_alive(ser, timeout: float) -> bool:
    """在已打开的串口上确认桥接脚本是否在跑。串口由调用方负责关闭。

    USB CDC 刚打开时主机侧还没就绪，紧跟着写的第一帧 PING 会被丢掉，
    所以先等一会儿再清空输入缓冲，并且重试 3 次（实测单次成功率约 1/3）。
    """
    try:
        ser.dtr = True
        ser.rts = True
    except Exception:
        pass
    try:
        time.sleep(0.3)                 # 等 CDC 通道就绪
        ser.reset_input_buffer()
        deadline = time.time() + max(timeout, 1.0)
        buf = ""
        for _ in range(3):
            if time.time() > deadline:
                break
            try:
                ser.write(b"PING\r\n")
                ser.flush()
            except Exception:
                return False
            step_end = time.time() + 0.6
            while time.time() < min(step_end, deadline):
                chunk = ser.read(512)
                if chunk:
                    buf += chunk.decode("utf-8", "replace")
                    if any(tok in buf for tok in _ALIVE_TOKENS):
                        return True
                else:
                    time.sleep(0.03)
        return False
    except Exception:
        return False


def _read_imei(ser, timeout: float = 2.0) -> str:
    """在已经确认在线的串口上要一次 STATUS，取 IMEI。

    IMEI 是唯一能区分物理模块的标识：两块 Air780EPM 的 USB iSerial 出厂都是
    `000000000001`，插在同一台机器上时 `/dev/serial/by-id` 会因重名互相覆盖，
    sysfs 拓扑在容器里还可能被裁剪，只有 IMEI 能稳定对上号。
    """
    try:
        ser.write(b"STATUS\r\n")
        ser.flush()
    except Exception:
        return ""
    buf = ""
    deadline = time.time() + max(timeout, 1.0)
    while time.time() < deadline:
        try:
            chunk = ser.read(512)
        except Exception:
            return ""
        if not chunk:
            time.sleep(0.03)
            continue
        buf += chunk.decode("utf-8", "replace")
        if len(buf) > 65536:            # 防脏数据把内存吃光
            buf = ""
            continue
        for line in buf.splitlines():
            line = line.strip()
            if not line.upper().startswith("+STATUS"):
                continue
            _, _, body = line.partition(" ")
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("imei"):
                return str(payload["imei"])
    return ""


def _open_serial(port: str):
    if not HAS_SERIAL:
        return None
    try:
        return serial.Serial(port, settings.modem_baud or 115200, timeout=0.1, write_timeout=3)
    except Exception:
        return None


def probe_bridge(port: str, timeout: float = 2.0) -> bool:
    """轻量探测：发 PING 看是否回 +PONG。用于自动选择协议。"""
    ser = _open_serial(port)
    if ser is None:
        return False
    try:
        return _ping_alive(ser, timeout)
    finally:
        try:
            ser.close()
        except Exception:
            pass


def probe_bridge_info(port: str, timeout: float = 2.0) -> dict:
    """探测桥接脚本并尽量读回 IMEI，返回 `{"ok": bool, "imei": str}`。

    比 `probe_bridge` 多花一次 STATUS 往返（几百毫秒），只在设备发现时用；
    读到的 IMEI 用于把「同一台模块的多个串口」收敛成一台设备 ——
    分组键撞车时（两块同 iSerial 的板子）这是唯一的纠错依据。
    """
    out = {"ok": False, "imei": ""}
    ser = _open_serial(port)
    if ser is None:
        return out
    try:
        if not _ping_alive(ser, timeout):
            return out
        out["ok"] = True
        out["imei"] = _read_imei(ser)
        return out
    finally:
        try:
            ser.close()
        except Exception:
            pass
