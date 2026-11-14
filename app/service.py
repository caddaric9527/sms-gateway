"""业务编排：多设备轮询同步、长短信聚合、发送路由、转发触发、状态聚��与事件广播。

多设备的关键约束：
- 每台模块一个独立的串口驱动实例 + 独立轮询任务，互不阻塞（发一条短信最长会卡 60 秒）；
- 设备身份用 IMEI（`+STATUS` 里带回），端口只用于启动时建立映射；
- 入库时带上 device_id，且去重指纹里必须混入 device_id，
  否则两台模块收到同一条短信时，后到的那条会被当成重复静默丢弃。
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from . import db, forwarder
from .config import settings
from .modem import Modem, SmsEntry, create_modem
from .operator import operator_from_imsi
from .phone import normalize_peer

EventCallback = Callable[[dict], Awaitable[None]]


def _fp(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8", errors="replace")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff\u3000-\u303f\uff00-\uffef]", text))


@dataclass
class DeviceRuntime:
    """一台模块的运行时状态。"""

    key: str                                   # 注册表键（USB 拓扑键 / mock 名），进程内稳定
    modem: Modem
    port: str = ""
    usb_group: str = ""
    device_id: str = ""                        # 入库用的设备主键：IMEI 优先
    name: str = ""
    state: dict[str, Any] = field(default_factory=dict)
    sync_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_sync: str = ""
    fail_streak: int = 0                        # 连续失败次数，用于触发端口重映射

    @property
    def public_id(self) -> str:
        return self.device_id or self.port or self.key


class Gateway:
    def __init__(self) -> None:
        self.devices: dict[str, DeviceRuntime] = {}
        self.primary_key: str = ""
        self.state: dict[str, Any] = {
            "connected": False,
            "port": "",
            "signal": None,
            "signal_percent": None,
            "registered": False,
            "operator": "",
            "sim_ready": False,
            "iccid": "",
            "number": "",
            "info": {},
            "error": "",
            "mock": settings.modem_mock,
            "last_sync": "",
            "last_error": "",
            "poll_interval": settings.poll_interval,
            "devices": [],
        }
        self._listeners: list[EventCallback] = []
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_discover = 0.0

    # ------------------------------------------------------------------ 兼容
    @property
    def modem(self) -> Modem:
        """默认设备（历史接口都只认一个 modem）。"""
        rt = self.devices.get(self.primary_key)
        if rt is None:
            # 还没完成探测：先给一个空驱动，避免调用方拿到 None
            rt = DeviceRuntime(key="pending", modem=create_modem())
            self.devices.setdefault("pending", rt)
            self.primary_key = self.primary_key or "pending"
        return rt.modem

    def runtime(self, ident: str | None = None) -> DeviceRuntime:
        """按 device_id / 端口 / 注册表键找设备；不传则用默认设备。"""
        if not ident:
            rt = self.devices.get(self.primary_key)
            if rt is None:
                self.modem  # 触发兜底创建
                rt = self.devices.get(self.primary_key)
            if rt is None:
                raise ValueError("没有可用设备")
            return rt
        for rt in self.devices.values():
            if ident in (rt.key, rt.port, rt.device_id, rt.public_id):
                return rt
        raise ValueError(f"未找到设备 {ident}")

    # ------------------------------------------------------------------ 事件
    def subscribe(self, cb: EventCallback) -> None:
        self._listeners.append(cb)

    def unsubscribe(self, cb: EventCallback) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    async def emit(self, event: dict) -> None:
        for cb in list(self._listeners):
            try:
                await cb(event)
            except Exception:
                pass

    # ------------------------------------------------------------------ 生命周期
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._loop = asyncio.get_running_loop()
        db.init_db()
        scope = settings.modem_ports or settings.modem_port
        db.add_log("info", "app", f"网关启动，端口={scope} mock={settings.modem_mock}")
        # 设备探测放到后台：串口故障不应阻塞 HTTP 服务启动
        self._tasks.append(asyncio.create_task(self._bootstrap(), name="bootstrap"))

    async def _bootstrap(self) -> None:
        try:
            await self._discover()
            await self._refresh_all()
            for rt in list(self.devices.values()):
                await self.sync(rt.key)
                self._tasks.append(
                    asyncio.create_task(self._device_loop(rt), name=f"dev-{rt.key}")
                )
            await self._refresh_all()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.state["last_error"] = str(exc)
            db.add_log("error", "app", f"设备初始化失败: {exc}")

    async def _discover(self, attempts: int = 3) -> None:
        """建立设备注册表：mock 模式造 N 台，真实模式按 USB 拓扑探测。

        attempts 是探测轮数：启动时给 3 轮（模块可能还在起脚本），
        HTTP 请求里触发的重探给 1 轮，避免把接口请求卡住十几秒。
        """
        from .device import detect_devices

        found: list[tuple[str, str, str, Modem]] = []   # (key, port, group, modem)

        if settings.modem_mock:
            count = max(1, settings.modem_mock_count)
            for idx in range(count):
                key = f"mock-{idx + 1}"
                modem = create_modem(idx + 1)
                found.append((key, f"mock-{idx + 1}", key, modem))
        else:
            probes = await asyncio.to_thread(detect_devices, attempts)
            if not probes:
                db.add_log("warn", "app", "未发现任何模块，等待重插后手动 resync")
            for dev in probes:
                if dev.kind == "luatos":
                    from .luatos import LuatosModem

                    modem: Modem = LuatosModem(port=dev.port)
                else:
                    modem = Modem(port=dev.port)
                found.append((dev.group, dev.port, dev.group, modem))

        self.devices.clear()
        for key, port, group, modem in found:
            self.devices[key] = DeviceRuntime(
                key=key, modem=modem, port=port, usb_group=group,
            )
        self.primary_key = next(iter(self.devices), "")
        if self.devices:
            db.add_log("info", "app", f"发现 {len(self.devices)} 台设备: "
                       + ", ".join(f"{rt.key}@{rt.port}" for rt in self.devices.values()))

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        for rt in list(self.devices.values()):
            await asyncio.to_thread(rt.modem.close)

    # ------------------------------------------------------------------ 状态
    async def refresh_status(self, device_id: str | None = None) -> dict:
        """刷新一台或全部设备；顶层字段保持"默认设备"，多设备明细放 devices。"""
        if not self.devices:
            # 一台都没探到：10 秒允许重探一次，支持插上设备后自动恢复
            now = asyncio.get_running_loop().time()
            if now - self._last_discover > 10:
                self._last_discover = now
                await self._discover(attempts=1)
                if self.devices:
                    for rt in list(self.devices.values()):
                        self._tasks.append(
                            asyncio.create_task(self._device_loop(rt), name=f"dev-{rt.key}")
                        )
        if device_id:
            await self._refresh_one(self.runtime(device_id))
            return self.state
        await self._refresh_all()
        return self.state

    async def _refresh_all(self) -> None:
        for rt in list(self.devices.values()):
            await self._refresh_one(rt)
        self._compose_state()

    async def _refresh_one(self, rt: DeviceRuntime) -> dict:
        prev = rt.state or {}
        try:
            st = await asyncio.to_thread(rt.modem.read_status)
        except Exception as exc:
            st = {"connected": False, "error": str(exc)}
        # AT 路径偶尔取不到 COPS，用 IMSI 兜底
        if not st.get("operator") and st.get("imsi"):
            st["operator"] = operator_from_imsi(str(st["imsi"])) or ""

        # 设备身份：IMEI 优先；拿不到时用 USB 拓扑键，保证入库归属稳定
        imei = str((st.get("info") or {}).get("imei") or st.get("imei") or "")
        rt.device_id = imei or rt.usb_group or rt.key
        rt.port = str(st.get("port") or rt.port)
        st["device_id"] = rt.device_id
        st["key"] = rt.key
        st["name"] = rt.name or rt.public_id
        st["last_sync"] = rt.last_sync
        rt.state = st
        st["mock"] = settings.modem_mock

        if st.get("connected"):
            try:
                await asyncio.to_thread(
                    db.upsert_device,
                    rt.device_id,
                    name=st["name"],
                    port=rt.port,
                    usb_group=rt.usb_group,
                    model=str((st.get("info") or {}).get("model") or ""),
                    firmware=str((st.get("info") or {}).get("firmware") or ""),
                    sim_number=str(st.get("number") or ""),
                    iccid=str(st.get("iccid") or ""),
                    protocol=str(st.get("protocol") or ""),
                )
            except Exception:
                pass

        # 只在状态**变化**时记一条，否则每 45 秒刷一次日志没法看
        was_ok, now_ok = bool(prev.get("connected")), bool(st.get("connected"))
        if now_ok and not was_ok:
            db.add_log("info", "app",
                       f"[{rt.public_id}] 已连接 {rt.port}"
                       f" 信号 {st.get('signal')}（{st.get('signal_percent')}%）"
                       f" {st.get('operator') or ''}".rstrip())
        elif was_ok and not now_ok:
            db.add_log("warn", "app", f"[{rt.public_id}] 掉线: {st.get('error') or '无响应'}")
        elif not now_ok and (st.get("error") or st.get("hint") or "") != (
                prev.get("error") or prev.get("hint") or ""):
            db.add_log("warn", "app",
                       f"[{rt.public_id}] 连接失败: {st.get('error') or st.get('hint') or '无响应'}")
        return st

    def _compose_state(self) -> None:
        """默认设备放顶层（兼容旧前端），全部设备放 devices。"""
        devices = []
        for rt in self.devices.values():
            st = rt.state or {}
            devices.append({
                "device_id": rt.device_id or rt.key,
                "key": rt.key,
                "name": rt.name or rt.device_id or rt.key,
                "port": rt.port,
                "connected": bool(st.get("connected")),
                "signal": st.get("signal"),
                "signal_percent": st.get("signal_percent"),
                "operator": st.get("operator") or "",
                "registered": bool(st.get("registered")),
                "sim_ready": bool(st.get("sim_ready")),
                "number": st.get("number") or "",
                "iccid": st.get("iccid") or "",
                "model": (st.get("info") or {}).get("model") or "",
                "firmware": (st.get("info") or {}).get("firmware") or "",
                "error": st.get("error") or "",
                "last_sync": rt.last_sync,
            })

        # 默认设备：优先挑连上的那台
        primary = self.devices.get(self.primary_key)
        if primary is None or not (primary.state or {}).get("connected"):
            for rt in self.devices.values():
                if (rt.state or {}).get("connected"):
                    primary = rt
                    break
        if primary is None and devices:
            primary = self.devices.get(devices[0]["key"])

        if primary is not None:
            self.primary_key = primary.key
            base = dict(primary.state or {})
            base.setdefault("port", primary.port)
            base["device_id"] = primary.device_id
            base["device_count"] = len(self.devices)
            base["devices"] = devices
            base["mock"] = settings.modem_mock
            base["poll_interval"] = settings.poll_interval
            base["last_sync"] = primary.last_sync
            self.state = base
        else:
            self.state["devices"] = devices

    async def _device_loop(self, rt: DeviceRuntime) -> None:
        """单台设备的轮询 + 状态刷新。互不阻塞，一台挂了不影响其它台。"""
        tick = 5
        elapsed = 0
        status_tick = 0
        while self._running:
            try:
                res: dict | None = None
                if rt.modem.new_sms_count > 0:
                    rt.modem.new_sms_count = 0
                    res = await self.sync(rt.key)
                    elapsed = 0
                elif elapsed >= max(5, settings.poll_interval):
                    res = await self.sync(rt.key)
                    elapsed = 0
                if res is not None:
                    if res.get("ok"):
                        rt.fail_streak = 0
                    else:
                        rt.fail_streak += 1
                        # 连续失败通常是模块重启/重插导致 ttyACM 号跳变，重新对齐端口
                        if rt.fail_streak >= 3 and await self._remap_ports():
                            rt.fail_streak = 0
                            await self._refresh_one(rt)
                            self._compose_state()
                            await self.emit({"type": "status", "data": self.state})
                status_tick += tick
                if status_tick >= max(15, settings.status_interval):
                    status_tick = 0
                    await self._refresh_one(rt)
                    self._compose_state()
                    await self.emit({"type": "status", "data": self.state})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                rt.state["last_error"] = str(exc)
                db.add_log("error", "sync", f"[{rt.key}] 轮询异常: {exc}")
                await asyncio.to_thread(rt.modem.close)
            await asyncio.sleep(tick)
            elapsed += tick

    async def _remap_ports(self) -> bool:
        """模块重启/重插后 USB 号可能跳变，按拓扑键把注册表里的端口重新对齐。

        设备主键用拓扑键（`1-7` 这类）而不是 ttyACM 号，正是为了这一步能自愈：
        组内逐个 PING，谁回 +PONG 就把那台设备的端口换过去。
        """
        from .device import candidate_groups
        from .luatos import probe_bridge

        try:
            groups = await asyncio.to_thread(candidate_groups)
        except Exception:
            return False
        changed = False
        for key, ports in groups:
            rt = self.devices.get(key)
            if rt is None or rt.port in ports:
                continue
            for port in ports:
                try:
                    ok = await asyncio.to_thread(probe_bridge, port, 1.2)
                except Exception:
                    ok = False
                if not ok:
                    continue
                old = rt.port
                rt.port = port
                setattr(rt.modem, "_fixed_port", port)
                setattr(rt.modem, "port", port)
                db.add_log("warn", "app", f"[{rt.key}] 端口漂移 {old}→{port}，已重新映射")
                changed = True
                break
        return changed

    # ------------------------------------------------------------------ 同步
    async def sync(self, device_id: str | None = None) -> dict:
        """从模块读取短信，聚合长短信，入库并触发转发。"""
        rt = self.runtime(device_id)
        if rt.sync_lock.locked():
            return {"ok": True, "skipped": True, "new": 0, "device": rt.public_id}
        async with rt.sync_lock:
            result = {"ok": True, "new": 0, "scanned": 0, "error": "",
                      "device": rt.public_id}
            modem = rt.modem
            # 固件不支持 SMS AT 命令时不要每轮都去撞 ERROR（会刷满日志）
            if getattr(modem, "info", {}).get("sms_capable") is False:
                result.update(ok=False, error="当前固件不支持短信 AT 命令")
                return result
            try:
                entries: list[SmsEntry] = await asyncio.to_thread(modem.list_sms, 4)
            except Exception as exc:
                result.update(ok=False, error=str(exc))
                rt.state["last_error"] = str(exc)
                return result

            result["scanned"] = len(entries)
            rt.last_sync = _now()
            dev = rt.device_id or rt.public_id
            processed_indexes: list[int] = []

            for entry in entries:
                decoded = entry.decoded
                if decoded is None:
                    processed_indexes.append(entry.index)
                    continue
                if decoded.kind != "deliver":
                    # 模块存储里自发送记录，跳过
                    processed_indexes.append(entry.index)
                    continue

                peer = normalize_peer(decoded.sender or "") or "unknown"
                text = decoded.text or ""
                if not text.strip():
                    processed_indexes.append(entry.index)
                    continue

                ts = decoded.timestamp or _now()
                encoding = "ucs2" if (decoded.encoding == "ucs2" or _has_cjk(text)) else decoded.encoding

                mid: int | None = None
                complete = True
                if decoded.concat_total > 1:
                    # 分片键必须带设备，否则两台模块的分片会互相串
                    key = f"{dev}|{peer}|{decoded.concat_ref}|{decoded.concat_total}|{ts[:16]}"
                    fp = _fp("part", dev, key, str(decoded.concat_seq))
                    mid, complete = db.merge_part(
                        peer=peer, concat_key=key, seq=decoded.concat_seq,
                        total=decoded.concat_total, text=text, ref=decoded.concat_ref,
                        ts=ts, fingerprint=fp, device_id=dev,
                    )
                    if mid:
                        db.set_modem_index(mid, entry.index)
                else:
                    fp = (_fp("single", dev, peer, ts, text) if decoded.timestamp
                          else _fp("raw", dev, peer, entry.pdu))
                    mid = db.insert_message({
                        "direction": "in", "peer": peer, "text": text, "ts": ts,
                        "encoding": encoding, "raw_pdu": entry.pdu,
                        "modem_index": entry.index, "device_id": dev,
                        "concat_total": 1, "concat_seq": 1, "complete": 1,
                        "status": "received", "fingerprint": fp,
                    })

                processed_indexes.append(entry.index)

                if mid and complete:
                    row = db.get_message(mid)
                    if row:
                        is_new = row.get("forwarded", 0) == 0 and not row.get("forward_log")
                        await self.emit({"type": "message", "data": row})
                        result["new"] += 1
                        if is_new:
                            await self._forward(row)

            if not settings.keep_on_modem and processed_indexes:
                for idx in processed_indexes:
                    try:
                        await asyncio.to_thread(modem.delete_sms, idx)
                    except Exception:
                        break

            if result["new"]:
                await self.emit({"type": "threads_dirty"})
            return result

    async def _forward(self, msg: dict) -> None:
        if db.get_setting("auto_forward", "1") != "1":
            return
        try:
            retry_limit = int(db.get_setting("retry_limit", "3") or 3)
        except ValueError:
            retry_limit = 3
        try:
            # 必须用收到这条短信的那台模块发回执，不能用默认设备
            modem = self.runtime(str(msg.get("device_id") or "") or None).modem
        except Exception:
            modem = self.modem
        try:
            ok, detail = await forwarder.dispatch(msg, modem, retry_limit)
        except Exception as exc:
            ok, detail = False, f"转发异常: {exc}"
        db.mark_forwarded(int(msg["id"]), ok, detail)
        db.add_log("info" if ok else "warn", "forward", f"#{msg['id']} {detail}")
        await self.emit({"type": "message_update", "data": {"id": msg["id"], "forwarded": int(ok), "forward_log": detail}})

    # ------------------------------------------------------------------ 发送
    def pick_device(self, prefer: str | None = None) -> DeviceRuntime:
        """选一台能发短信的设备：优先指定 → 已注册且有 SIM 的 → 任意连上的。"""
        if prefer:
            return self.runtime(prefer)
        candidates = list(self.devices.values())
        if not candidates:
            raise ValueError("没有可用设备")
        ready = [rt for rt in candidates
                 if (rt.state or {}).get("registered") and (rt.state or {}).get("sim_ready")]
        connected = [rt for rt in candidates if (rt.state or {}).get("connected")]
        return (ready or connected or candidates)[0]

    async def send(self, peer: str, text: str, device: str | None = None) -> dict:
        peer = (peer or "").strip()
        text = (text or "").strip()
        if not peer:
            raise ValueError("接收号码不能为空")
        if not text:
            raise ValueError("短信内容不能为空")
        rt = self.pick_device(device)
        if "mock" not in str(rt.modem.port) and not re.fullmatch(r"[+\d\s\-]{3,25}", peer):
            raise ValueError("号码格式不合法")
        peer = normalize_peer(peer) or peer
        signature = db.get_setting("signature", "")
        body = f"{text}{signature}" if signature else text

        result = await asyncio.to_thread(rt.modem.send_sms, peer, body)
        status = "sent" if result.ok else "failed"
        dev = rt.device_id or rt.public_id
        mid = db.insert_message({
            "direction": "out", "peer": peer, "text": body, "ts": _now(),
            "encoding": "ucs2" if _has_cjk(body) else "gsm7",
            "status": status, "error": result.error, "complete": 1,
            "device_id": dev,
            "fingerprint": _fp("out", dev, uuid.uuid4().hex),
        })
        row = db.get_message(mid) if mid else None
        if row:
            await self.emit({"type": "message", "data": row})
            await self.emit({"type": "threads_dirty"})
        db.add_log("info" if result.ok else "error", "send",
                   f"[{dev}] → {peer} ({result.parts}条) {'成功' if result.ok else result.error}")
        return {"ok": result.ok, "error": result.error, "parts": result.parts,
                "device": dev, "message": row}

    async def resend(self, mid: int) -> dict:
        row = db.get_message(mid)
        if not row:
            raise ValueError("消息不存在")
        return await self.send(row["peer"], row["text"], device=row.get("device_id") or None)


gateway = Gateway()
