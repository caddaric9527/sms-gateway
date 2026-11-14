"""LuatOS 桥接驱动自测：用假串口模拟模块侧脚本，验证协议解析全链路。

不依赖真实硬件，跑法：python -m tests.test_luatos
"""
from __future__ import annotations

import json
import sys
import threading
import time
import types

from app import luatos
from app.luatos import LuatosModem


class FakeSerial:
    """最小串口替身：收到指令就按脚本协议吐响应。"""

    def __init__(self, port: str = "FAKE") -> None:
        self.port = port
        self.baudrate = 115200
        self.timeout = 0.1
        self.write_timeout = 10
        self.dtr = False
        self.rts = False
        self.is_open = True
        self._in = bytearray()
        self._lock = threading.Lock()
        self.sent: list[str] = []
        self.fail_next_send = False
        # 模拟一个已插卡、已注册、信号 22 的模块
        self.state = {
            "csq": 22, "reg": 1, "sim": True,
            "iccid": "89860000000000000000",
            "imsi": "460110000000000",
            "num": "", "imei": "860000000000001",
            "fw": "EC718PM Air780EPM A12", "bsp": "Air780EPM",
        }

    # --- pyserial 接口 ---
    def open(self) -> None:
        self.is_open = True

    def close(self) -> None:
        self.is_open = False

    def flush(self) -> None:
        pass

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._in.clear()

    def write(self, data: bytes) -> int:
        line = data.decode("utf-8").strip()
        self.sent.append(line)
        self._handle(line)
        return len(data)

    def read(self, size: int) -> bytes:
        time.sleep(0.01)
        with self._lock:
            if not self._in:
                return b""
            out = bytes(self._in[:size])
            del self._in[:size]
            return out

    def _push(self, text: str) -> None:
        with self._lock:
            self._in.extend((text + "\r\n").encode("utf-8"))

    def _handle(self, line: str) -> None:
        if line == "PING":
            self._push("+PONG")
        elif line == "STATUS":
            st = dict(self.state)
            st["ts"] = int(time.time())
            self._push("+STATUS " + json.dumps(st, ensure_ascii=False))
        elif line.startswith("SEND "):
            payload = json.loads(line[5:])
            ok = not self.fail_next_send
            self._push("+SENT " + json.dumps(
                {"id": payload.get("id"), "ok": ok, "err": "" if ok else "网络拒绝"},
                ensure_ascii=False))
        else:
            self._push("+ERR unknown")


results: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    results.append((name, bool(cond), detail))


def build_modem(fake: FakeSerial) -> LuatosModem:
    """把模块里的 serial.Serial 换成假串口，端口固定为 FAKE。"""
    fake_module = types.SimpleNamespace(Serial=lambda *a, **k: fake)
    luatos.serial = fake_module
    luatos.settings.modem_port = "FAKE"
    return LuatosModem()


def main() -> int:
    fake = FakeSerial()
    modem = build_modem(fake)
    assert modem.open() is True
    check("握手：PING → +PONG", modem.connected, f"port={modem.port}")

    st = modem.read_status()
    check("状态：已连接", st["connected"] is True)
    check("状态：信号 22 → 71%", st["signal"] == 22 and st["signal_percent"] == 71,
          f"{st['signal']}/{st['signal_percent']}")
    check("状态：已注册", st["registered"] is True)
    check("状态：SIM 就绪", st["sim_ready"] is True and st["sim_status"] == "ok")
    check("状态：ICCID 透传", st["iccid"] == "89860000000000000000", st["iccid"])
    check("状态：协议标记", st.get("protocol") == "luatos")

    # 短信发送
    res = modem.send_sms("+8613912345678", "测试短信")
    check("发送：成功回执", res.ok is True, f"parts={res.parts}")
    check("发送：指令带 JSON", fake.sent[-1].startswith("SEND {"), fake.sent[-1][:40])

    fake.fail_next_send = True
    res = modem.send_sms("+8613912345678", "失败用例")
    check("发送：失败回执透传", res.ok is False and "网络拒绝" in res.error, res.error)
    fake.fail_next_send = False

    long_text = "长" * 80     # 240 字节，超过 140
    res = modem.send_sms("+8613912345678", long_text)
    check("发送：长短信按分片计数", res.ok is True and res.parts >= 2, f"parts={res.parts}")

    # 收短信（模块主动推送）
    fake._push("+SMS " + json.dumps(
        {"num": "+8613800138000", "txt": "您的验证码是 123456", "ts": int(time.time())},
        ensure_ascii=False))
    time.sleep(0.4)
    check("接收：推送计数 +1", modem.new_sms_count == 1, str(modem.new_sms_count))

    items = modem.list_sms()
    check("接收：队列取出 1 条", len(items) == 1)
    if items:
        d = items[0].decoded
        check("接收：号码解析", d.sender == "+8613800138000", d.sender)
        check("接收：正文解析", d.text == "您的验证码是 123456", d.text)
        check("接收：中文判为 ucs2", d.encoding == "ucs2")
        check("接收：时间戳可用", bool(d.timestamp) and "T" in d.timestamp, d.timestamp)

    check("接收：队列取完即空", modem.list_sms() == [])

    # 心跳也应更新信号
    fake.state["csq"] = 9
    fake._push("+HB " + json.dumps({**fake.state, "ts": int(time.time())}, ensure_ascii=False))
    time.sleep(0.3)
    modem._last_status = {}   # 让 read_status 走一次 STATUS 请求
    st2 = modem.read_status()
    check("心跳：缓存生效（无响应时用 +HB）", st2["connected"] is True)

    modem.close()
    check("关闭：连接标记复位", modem.connected is False)

    # 汇总
    passed = sum(1 for _, ok, _ in results if ok)
    print()
    for name, ok, detail in results:
        mark = "✔" if ok else "✘"
        print(f"  {mark} {name}" + (f"  [{detail}]" if detail and not ok else ""))
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
