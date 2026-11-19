"""REST API 与 WebSocket。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import db, forwarder
from .config import settings
from .service import gateway

router = APIRouter()

TOKEN_TTL = 30 * 86400


def _secret() -> str:
    if settings.secret_key:
        return settings.secret_key
    val = db.get_setting("_secret", "")
    if not val:
        val = secrets.token_hex(32)
        db.set_settings({"_secret": val})
    return val


def make_token() -> str:
    exp = int(time.time()) + TOKEN_TTL
    payload = str(exp)
    sig = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{payload}.{sig}"


def verify_token(token: str) -> bool:
    if not settings.auth_password:
        return True
    try:
        payload, sig = token.split(".", 1)
        exp = int(payload)
    except (ValueError, AttributeError):
        return False
    if exp < time.time():
        return False
    expect = hmac.new(_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expect, sig)


async def auth(request: Request) -> None:
    if not settings.auth_password:
        return
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    if not verify_token(token):
        raise HTTPException(status_code=401, detail="未登录或登录已过期")


# --------------------------------------------------------------------------- #
# 认证
# --------------------------------------------------------------------------- #
@router.get("/ping")
async def ping() -> dict:
    return {"ok": True, "auth_required": bool(settings.auth_password),
            "title": settings.site_title, "mock": settings.modem_mock}


@router.post("/auth/login")
async def login(payload: dict = Body(...)) -> dict:
    if not settings.auth_password:
        return {"ok": True, "token": "", "auth_required": False}
    if not hmac.compare_digest(str(payload.get("password", "")), settings.auth_password):
        await asyncio.sleep(0.6)
        raise HTTPException(status_code=401, detail="密码错误")
    return {"ok": True, "token": make_token(), "auth_required": True}


@router.get("/auth/check", dependencies=[Depends(auth)])
async def check() -> dict:
    return {"ok": True}


# --------------------------------------------------------------------------- #
# 设备状态
# --------------------------------------------------------------------------- #
@router.get("/status", dependencies=[Depends(auth)])
async def status() -> dict:
    return gateway.state


@router.get("/devices", dependencies=[Depends(auth)])
async def devices() -> dict:
    """在线设备 + 历史登记。多设备时前端按 device_id 过滤会话。"""
    online = {d["device_id"]: d for d in gateway.state.get("devices", [])}
    items = []
    for row in db.list_devices():
        dev_id = row["device_id"]
        live = online.get(dev_id, {})
        items.append({
            **row,
            "online": bool(live.get("connected")),
            "signal": live.get("signal"),
            "signal_percent": live.get("signal_percent"),
            "operator": live.get("operator") or "",
            "number": live.get("number") or row.get("sim_number") or "",
        })
    # 在线但还没登记进库的（刚插上、还没刷新状态）
    known = {i["device_id"] for i in items}
    for dev_id, live in online.items():
        if dev_id not in known:
            items.append({"device_id": dev_id, **live, "online": True})
    return {"items": items, "count": len(items), "primary": gateway.primary_key}


@router.post("/status/refresh", dependencies=[Depends(auth)])
async def refresh(device: str | None = None) -> dict:
    return await gateway.refresh_status(device)


@router.post("/devices/{device_id}/alias", dependencies=[Depends(auth)])
async def device_alias(device_id: str, payload: dict = Body(...)) -> dict:
    """给设备起名字。境外卡不写本机号码时，这是区分设备的主要手段。"""
    return await gateway.set_alias(device_id, str(payload.get("alias", "")))


@router.post("/modem/resync", dependencies=[Depends(auth)])
async def resync(device: str | None = None) -> dict:
    return await gateway.sync(device)


@router.post("/modem/reconnect", dependencies=[Depends(auth)])
async def reconnect(device: str | None = None) -> dict:
    rt = gateway.runtime(device)
    await asyncio.to_thread(rt.modem.close)
    ok = await asyncio.to_thread(rt.modem.open)
    await gateway.refresh_status(rt.key)
    if ok:
        await gateway.sync(rt.key)
    return {"ok": ok, "device": rt.public_id, "port": rt.modem.port,
            "error": "" if ok else rt.modem.last_error}


@router.post("/modem/at", dependencies=[Depends(auth)])
async def at_command(payload: dict = Body(...), device: str | None = None) -> dict:
    cmd = str(payload.get("cmd", "")).strip()
    if not cmd.upper().startswith("AT"):
        raise HTTPException(status_code=400, detail="只允许 AT 命令")
    if len(cmd) > 200:
        raise HTTPException(status_code=400, detail="命令过长")
    rt = gateway.runtime(device)
    res = await asyncio.to_thread(rt.modem.command, cmd, 15.0)
    return {"ok": res.ok, "lines": res.lines, "error": res.error, "raw": res.raw}


# --------------------------------------------------------------------------- #
# 短信
# --------------------------------------------------------------------------- #
@router.get("/threads", dependencies=[Depends(auth)])
async def threads(limit: int = Query(200, ge=1, le=1000)) -> list[dict]:
    return db.list_threads(limit)


@router.post("/threads/{peer}/read", dependencies=[Depends(auth)])
async def read_thread(peer: str) -> dict:
    db.mark_read(peer)
    return {"ok": True}


@router.get("/messages", dependencies=[Depends(auth)])
async def messages(peer: str | None = None, q: str | None = None,
                   device: str | None = None,
                   limit: int = Query(60, ge=1, le=300),
                   offset: int = Query(0, ge=0)) -> dict:
    items = db.list_messages(peer=peer, query=q, limit=limit, offset=offset, device=device)
    if peer and db.get_setting("auto_mark_read", "1") == "1":
        db.mark_read(peer)
    return {"items": items, "limit": limit, "offset": offset}


@router.post("/messages", dependencies=[Depends(auth)])
async def send_message(payload: dict = Body(...)) -> dict:
    peer = str(payload.get("peer", ""))
    text = str(payload.get("text", ""))
    device = str(payload.get("device", "") or "") or None
    try:
        result = await gateway.send(peer, text, device=device)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result.get("ok"):
        return JSONResponse(status_code=502, content=result)
    return result


@router.delete("/messages/{mid}", dependencies=[Depends(auth)])
async def remove_message(mid: int) -> dict:
    return {"ok": db.delete_message(mid)}


@router.delete("/messages", dependencies=[Depends(auth)])
async def clear_thread(peer: str) -> dict:
    return {"ok": True, "deleted": db.clear_messages(peer)}


@router.post("/messages/{mid}/resend", dependencies=[Depends(auth)])
async def resend(mid: int) -> dict:
    try:
        return await gateway.resend(mid)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/messages/{mid}/forward", dependencies=[Depends(auth)])
async def manual_forward(mid: int) -> dict:
    row = db.get_message(mid)
    if not row:
        raise HTTPException(status_code=404, detail="消息不存在")
    await gateway._forward(row)
    return {"ok": True, "message": db.get_message(mid)}


# --------------------------------------------------------------------------- #
# 转发规则
# --------------------------------------------------------------------------- #
@router.get("/rules", dependencies=[Depends(auth)])
async def get_rules() -> list[dict]:
    return db.list_rules()


@router.post("/rules", dependencies=[Depends(auth)])
async def create_rule(payload: dict = Body(...)) -> dict:
    if not str(payload.get("name", "")).strip():
        raise HTTPException(status_code=400, detail="规则名称不能为空")
    rid = db.upsert_rule(payload)
    return {"ok": True, "id": rid}


@router.put("/rules/{rid}", dependencies=[Depends(auth)])
async def update_rule(rid: int, payload: dict = Body(...)) -> dict:
    payload["id"] = rid
    db.upsert_rule(payload)
    return {"ok": True, "id": rid}


@router.delete("/rules/{rid}", dependencies=[Depends(auth)])
async def remove_rule(rid: int) -> dict:
    return {"ok": db.delete_rule(rid)}


@router.post("/rules/test", dependencies=[Depends(auth)])
async def test_rule(payload: dict = Body(...)) -> dict:
    """用假数据试跑一条规则，方便调 Webhook。"""
    target = payload.get("target") or {}
    fake_msg = {
        "id": 0,
        "peer": payload.get("peer") or "+8613800138000",
        "text": payload.get("text") or "这是一条测试短信 Test 123456",
        "ts": db.now_iso(),
    }
    fake_rule = {"name": payload.get("rule_name") or "测试规则"}
    ttype = target.get("type", "")
    try:
        if ttype == "webhook":
            await forwarder._send_webhook(target, fake_msg, fake_rule, 15)
        elif ttype == "smtp":
            await forwarder._send_smtp(target, fake_msg, fake_rule)
        elif ttype == "sms":
            await forwarder._send_sms(target, fake_msg, fake_rule, gateway.modem)
        else:
            raise ValueError(f"未知类型 {ttype}")
        return {"ok": True, "detail": "发送成功"}
    except Exception as exc:
        return {"ok": False, "detail": str(exc)}


# --------------------------------------------------------------------------- #
# 设置 / 日志
# --------------------------------------------------------------------------- #
HIDDEN_SETTINGS = {"_secret"}


@router.get("/settings", dependencies=[Depends(auth)])
async def get_settings_api() -> dict:
    data = db.get_settings()
    if data.get("smtp_pass"):
        data["smtp_pass_set"] = "1"
        data["smtp_pass"] = ""
    for k in HIDDEN_SETTINGS:
        data.pop(k, None)
    return data


@router.put("/settings", dependencies=[Depends(auth)])
async def put_settings(payload: dict = Body(...)) -> dict:
    clean: dict[str, Any] = {}
    for k, v in payload.items():
        if k in HIDDEN_SETTINGS or k.startswith("_"):
            continue
        if k == "smtp_pass" and v == "":
            continue  # 空值表示不修改
        clean[k] = v
    db.set_settings(clean)
    return {"ok": True}


@router.get("/logs", dependencies=[Depends(auth)])
async def logs(limit: int = Query(100, ge=1, le=500), level: str | None = None) -> list[dict]:
    return db.list_logs(limit, level)


# --------------------------------------------------------------------------- #
# WebSocket
# --------------------------------------------------------------------------- #
class WSManager:
    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()

    async def broadcast(self, event: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(json.dumps(event, ensure_ascii=False, default=str))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


ws_manager = WSManager()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    token = ws.query_params.get("token", "")
    if settings.auth_password and not verify_token(token):
        await ws.close(code=4401)
        return
    await ws.accept()
    ws_manager.clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "status", "data": gateway.state}, ensure_ascii=False, default=str))
        while True:
            raw = await ws.receive_text()
            if raw == "ping":
                await ws.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        ws_manager.clients.discard(ws)


gateway.subscribe(ws_manager.broadcast)
