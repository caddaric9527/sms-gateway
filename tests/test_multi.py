"""多设备后端自测：python -m tests.test_multi

不碰真实串口，用假驱动验证多设备场景最容易出错的四件事：
1. 两台模块收到同一条短信，不能被指纹去重吞掉；
2. 入库的 device_id 要能区分是谁收的；
3. 发信要能按设备路由，不能永远发给默认那台；
4. 老库（没有 device_id 列）要能自动迁移。
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.config import settings  # noqa: E402
from app.modem import SendResult, SmsEntry  # noqa: E402
from app.pdu import DecodedSms  # noqa: E402
from app.service import DeviceRuntime, Gateway  # noqa: E402

FAILED: list[str] = []


def check(name: str, actual, expected) -> None:
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(f"{name}: 期望 {expected!r}, 实际 {actual!r}")


def ok(name: str, cond: bool, detail: str = "") -> None:
    print(f"{'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        FAILED.append(f"{name} {detail}")


class FakeModem:
    """满足 Gateway 需要的最小驱动接口。"""

    def __init__(self, imei: str, port: str, number: str, ready: bool = True) -> None:
        self.port = port
        self.connected = True
        self.new_sms_count = 0
        self.info = {"model": "fake", "firmware": "f", "imei": imei, "sms_capable": True}
        self._imei = imei
        self._number = number
        self._ready = ready
        self._entries: list[SmsEntry] = []
        self.sent: list[tuple[str, str]] = []

    def read_status(self) -> dict:
        return {
            "connected": True, "port": self.port, "signal": 20, "signal_percent": 65,
            "registered": self._ready, "sim_ready": self._ready,
            "iccid": "8986" + self._imei[-6:], "number": self._number,
            "imsi": "460000000000000", "operator": "",
            "info": dict(self.info), "error": "", "hint": "",
        }

    def push(self, sender: str, text: str, ts: str, concat: tuple[int, int, int] | None = None) -> None:
        ref, total, seq = concat or (None, 1, 1)
        self._entries.append(SmsEntry(
            index=len(self._entries) + 1, status=0, pdu=f"PDU-{text}-{seq}",
            decoded=DecodedSms(kind="deliver", sender=sender, text=text, timestamp=ts,
                               encoding="ucs2", concat_ref=ref, concat_total=total,
                               concat_seq=seq, raw=""),
        ))

    def list_sms(self, stat: int = 4) -> list[SmsEntry]:
        out, self._entries = self._entries, []
        return out

    def delete_sms(self, index: int) -> bool:
        return True

    def send_sms(self, peer: str, text: str) -> SendResult:
        self.sent.append((peer, text))
        return SendResult(True, "", 1, [1])

    def close(self) -> None:
        pass


def build_gateway() -> tuple[Gateway, FakeModem, FakeModem]:
    gw = Gateway()
    a = FakeModem("860000000000001", "fake-A", "+8613800000001")
    b = FakeModem("860000000000002", "fake-B", "+8613800000002")
    gw.devices = {
        "A": DeviceRuntime(key="A", modem=a, port="fake-A", usb_group="1-7"),
        "B": DeviceRuntime(key="B", modem=b, port="fake-B", usb_group="1-8"),
    }
    gw.primary_key = "A"
    return gw, a, b


async def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sms-multi-")
    settings.db_path = os.path.join(tmp, "test.db")
    settings.modem_mock = False
    db.init_db()

    gw, a, b = build_gateway()
    await gw._refresh_all()
    check("设备 A 主键取自 IMEI", gw.devices["A"].device_id, "860000000000001")
    check("状态聚合列出 2 台", len(gw.state["devices"]), 2)

    # --- 0. 设备别名：境外卡不写本机号码时，只能靠别名区分设备 ---
    dev_a = gw.devices["A"].device_id
    check("别名默认为空", gw.state["devices"][0].get("alias"), "")
    check("写别名返回成功", db.set_device_alias(dev_a, "香港卡"), True)
    gw.aliases = db.device_aliases()
    gw._aliases_loaded = True
    gw._compose_state()
    check("别名写进状态",
          [d.get("alias") for d in gw.state["devices"] if d["device_id"] == dev_a],
          ["香港卡"])
    check("别名读库一致", db.device_aliases().get(dev_a), "香港卡")
    check("给不存在的设备改名返回 False", db.set_device_alias("999999999999999", "x"), False)

    # --- 1. 同一号码、同一时间、同一内容，两台各收到一条 ---
    ts = "2026-11-13T22:00:00+08:00"
    a.push("13300000000", "同一条通知", ts)
    b.push("13300000000", "同一条通知", ts)
    ra = await gw.sync("A")
    rb = await gw.sync("B")
    check("A 收到 1 条", ra["new"], 1)
    check("B 收到 1 条（未被去重吞掉）", rb["new"], 1)
    rows = db.list_messages(limit=10)
    check("库里共 2 条", len(rows), 2)
    check("两条归属不同设备", sorted(r["device_id"] for r in rows),
          ["860000000000001", "860000000000002"])
    check("号码已归一化", {r["peer"] for r in rows}, {"+8613300000000"})

    # --- 2. 同一台设备重复同步同一条，只入库一次 ---
    a.push("13300000000", "同一条通知", ts)
    ra2 = await gw.sync("A")
    check("A 重复同步不再新增", ra2["new"], 0)
    check("库里仍是 2 条", len(db.list_messages(limit=10)), 2)

    # --- 3. 长短信分片按设备隔离，不互相串 ---
    a.push("13900000000", "分片一", ts, (7, 2, 1))
    b.push("13900000000", "分片二", ts, (7, 2, 1))
    a.push("13900000000", "分片二", ts, (7, 2, 2))
    b.push("13900000000", "分片一", ts, (7, 2, 2))
    await gw.sync("A")
    await gw.sync("B")
    merged = [r for r in db.list_messages(peer="+8613900000000", limit=10)]
    check("长短信合成为 2 条（每台一条）", len(merged), 2)
    # A 先收到「分片一」，B 先收到「分片二」——拼出来的内容各自独立，说明没有串片
    check("每台各自内容正确", sorted(r["text"] for r in merged), ["分片一分片二", "分片二分片一"])
    check("分片记录都已完整", {r["complete"] for r in merged}, {1})

    # --- 4. 发信按设备路由 ---
    r1 = await gw.send("13300000000", "发给A", device="860000000000001")
    check("路由到 A", (r1["ok"], len(a.sent), len(b.sent)), (True, 1, 0))
    r2 = await gw.send("13300000000", "发给B", device="fake-B")
    check("按端口也能指定 B", (r2["ok"], len(b.sent)), (True, 1))
    check("出站记录带设备", db.get_message(r2["message"]["id"])["device_id"], "860000000000002")

    # --- 5. 默认设备选择：优先已注册且 SIM 就绪的 ---
    b._ready = False
    await gw._refresh_all()
    check("SIM 未就绪的设备被跳过", gw.pick_device().key, "A")

    # --- 6. 按设备过滤消息 ---
    only_a = db.list_messages(device="860000000000001", limit=20)
    ok("设备过滤只返回本设备消息", all(r["device_id"] == "860000000000001" for r in only_a),
       f"共 {len(only_a)} 条")

    # --- 7. 老库迁移：没有 device_id 列的库要能自动补列 ---
    old = os.path.join(tmp, "old.db")
    # 模拟"升级前的库"：结构就是当前结构，只是少了 device_id 列
    with sqlite3.connect(old) as c:
        c.execute("""CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, direction TEXT NOT NULL, peer TEXT NOT NULL,
            text TEXT DEFAULT '', ts TEXT NOT NULL, encoding TEXT DEFAULT 'gsm7',
            raw_pdu TEXT DEFAULT '', modem_index INTEGER, concat_ref INTEGER,
            concat_total INTEGER DEFAULT 1, concat_seq INTEGER DEFAULT 1, concat_key TEXT,
            parts TEXT DEFAULT '{}', complete INTEGER DEFAULT 1, status TEXT DEFAULT 'received',
            error TEXT DEFAULT '', is_read INTEGER DEFAULT 0, forwarded INTEGER DEFAULT 0,
            forward_log TEXT DEFAULT '', fingerprint TEXT UNIQUE, created_at TEXT NOT NULL)""")
        c.execute("CREATE INDEX idx_msg_peer_ts ON messages(peer, ts DESC)")
        c.execute("CREATE INDEX idx_msg_concat ON messages(concat_key)")
        c.execute("INSERT INTO messages(direction,peer,text,ts,fingerprint,created_at)"
                  " VALUES('in','+8613800000000','老数据','2026-01-01T00:00:00+08:00','old-1','2026-01-01')")
    settings.db_path = old
    db.init_db()
    with db.conn() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(messages)").fetchall()}
    ok("老库自动补上 device_id 列", "device_id" in cols)
    check("老数据仍可读", len(db.list_messages(limit=5)), 1)

    print()
    if FAILED:
        print(f"{len(FAILED)} 项失败:")
        for f in FAILED:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
