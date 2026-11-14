"""转发引擎：可插拔 Provider。

target 结构（存在 rules.targets JSON 数组里）：
  {"type": "webhook", "url": "http://...", "headers": {...}, "template": "", "enabled": true}
  {"type": "smtp",    "to": "a@b.com, c@d.com", "subject": "", "enabled": true}
  {"type": "sms",     "to": "+8613800138000", "prefix": "", "enabled": true}

占位符：{from} {text} {time} {device} {rule} {id}
"""
from __future__ import annotations

import asyncio
import json
import smtplib
import ssl
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate
from typing import Any

from . import db
from .config import settings

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False


class ForwardError(Exception):
    pass


def _render(tpl: str, ctx: dict[str, Any]) -> str:
    try:
        return tpl.format(**ctx)
    except (KeyError, IndexError, ValueError):
        return tpl


def _ctx(msg: dict, rule: dict) -> dict[str, Any]:
    return {
        "from": msg.get("peer", ""),
        "text": msg.get("text", ""),
        "time": msg.get("ts", ""),
        "device": settings.device_name,
        "device_id": msg.get("device_id", ""),
        "rule": rule.get("name", ""),
        "id": msg.get("id", ""),
    }


def _load_smtp_conf() -> dict[str, str]:
    conf = db.get_settings()
    conf.setdefault("smtp_port", "465")
    conf.setdefault("smtp_ssl", "1")
    return conf


# --------------------------------------------------------------------------- #
# Providers
# --------------------------------------------------------------------------- #
async def _send_webhook(target: dict, msg: dict, rule: dict, timeout: float) -> None:
    if not HAS_HTTPX:
        raise ForwardError("缺少 httpx 依赖")
    ctx = _ctx(msg, rule)
    url = _render(str(target.get("url", "")), ctx)
    if not url.startswith(("http://", "https://")):
        raise ForwardError(f"非法 URL: {url!r}")

    headers = {"Content-Type": "application/json; charset=utf-8"}
    headers.update(target.get("headers") or {})

    tpl = (target.get("template") or "").strip()
    if tpl:
        body_text = _render(tpl, ctx)
        try:
            body = json.loads(body_text)
        except json.JSONDecodeError:
            headers["Content-Type"] = "text/plain; charset=utf-8"
            body = body_text  # type: ignore[assignment]
    else:
        body = {
            "from": msg.get("peer", ""),
            "text": msg.get("text", ""),
            "timestamp": msg.get("ts", ""),
            "device": settings.device_name,
            "device_id": msg.get("device_id", ""),
            "rule": rule.get("name", ""),
            "message_id": msg.get("id"),
            "id": msg.get("id"),
        }

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.post(url, json=body if isinstance(body, dict) else None,
                                 content=None if isinstance(body, dict) else body,
                                 headers=headers)
    if resp.status_code >= 300:
        raise ForwardError(f"HTTP {resp.status_code}: {resp.text[:200]}")


def _send_smtp_sync(target: dict, msg: dict, rule: dict) -> None:
    conf = _load_smtp_conf()
    host = conf.get("smtp_host", "").strip()
    if not host:
        raise ForwardError("未配置 SMTP 服务器")
    try:
        port = int(conf.get("smtp_port", "465"))
    except ValueError:
        port = 465
    use_ssl = conf.get("smtp_ssl", "1") == "1"
    user = conf.get("smtp_user", "").strip()
    password = conf.get("smtp_pass", "")
    sender = conf.get("smtp_from", "").strip() or user
    recipients = [x.strip() for x in (target.get("to") or conf.get("smtp_to", "")).split(",") if x.strip()]
    if not recipients:
        raise ForwardError("收件人为空")
    if not sender:
        raise ForwardError("未配置发件人")

    ctx = _ctx(msg, rule)
    subject_tpl = target.get("subject") or "[{device}] 来自 {from} 的短信"
    subject = _render(str(subject_tpl), ctx)
    body_tpl = target.get("body") or (
        "发件号码: {from}\n接收时间: {time}\n匹配规则: {rule}\n\n{text}\n\n"
        "—— 由 {device} 短信网关自动转发"
    )
    body = _render(str(body_tpl), ctx)

    mail = MIMEText(body, "plain", "utf-8")
    mail["Subject"] = Header(subject, "utf-8")
    mail["From"] = formataddr(("SMS Gateway", sender))
    mail["To"] = ", ".join(recipients)
    mail["Date"] = formatdate(localtime=True)

    if use_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=25,
                                                context=ssl.create_default_context())
    else:
        server = smtplib.SMTP(host, port, timeout=25)
        try:
            server.starttls(context=ssl.create_default_context())
        except smtplib.SMTPException:
            pass  # 服务器不支持 STARTTLS 时继续明文
    try:
        if user:
            server.login(user, password)
        server.sendmail(sender, recipients, mail.as_string())
    finally:
        try:
            server.quit()
        except Exception:
            pass


async def _send_smtp(target: dict, msg: dict, rule: dict) -> None:
    await asyncio.to_thread(_send_smtp_sync, target, msg, rule)


async def _send_sms(target: dict, msg: dict, rule: dict, modem) -> None:
    to = (target.get("to") or "").strip()
    if not to:
        raise ForwardError("未配置转发号码")
    prefix = target.get("prefix") or ""
    text = f"{prefix}[{msg.get('peer','')}] {msg.get('text','')}"
    result = await asyncio.to_thread(modem.send_sms, to, text)
    if not result.ok:
        raise ForwardError(result.error or "短信转发失败")


# --------------------------------------------------------------------------- #
# 调度
# --------------------------------------------------------------------------- #
def match_rule(rule: dict, msg: dict) -> bool:
    if not rule.get("enabled"):
        return False
    peer = msg.get("peer", "")
    text = msg.get("text", "")
    kind = rule.get("match_type", "all")
    pattern = (rule.get("pattern") or "").strip()
    if kind == "all":
        return True
    if kind == "contains":
        return bool(pattern) and pattern.lower() in text.lower()
    if kind == "regex":
        if not pattern:
            return False
        import re
        try:
            return re.search(pattern, text, re.IGNORECASE) is not None
        except re.error:
            return False
    if kind == "sender":
        if not pattern:
            return False
        for pat in pattern.split(","):
            pat = pat.strip()
            if pat and pat in peer:
                return True
        return False
    return False


async def dispatch(msg: dict, modem, retry_limit: int = 3) -> tuple[bool, str]:
    """按规则逐条推送，返回 (是否全部成功, 明细)。"""
    rules = db.list_rules(enabled_only=True)
    matched = [r for r in rules if match_rule(r, msg)]
    if not matched:
        return True, "无匹配规则"

    timeout = float(db.get_setting("webhook_timeout", "10") or 10)
    results: list[str] = []
    all_ok = True

    for rule in matched:
        for target in rule.get("targets") or []:
            if not isinstance(target, dict) or target.get("enabled") is False:
                continue
            ttype = target.get("type", "")
            label = f"{rule.get('name','规则')}/{ttype}"
            last_err = ""
            ok = False
            for attempt in range(1, max(1, retry_limit) + 1):
                try:
                    if ttype == "webhook":
                        await _send_webhook(target, msg, rule, timeout)
                    elif ttype == "smtp":
                        await _send_smtp(target, msg, rule)
                    elif ttype == "sms":
                        await _send_sms(target, msg, rule, modem)
                    else:
                        raise ForwardError(f"未知类型: {ttype}")
                    ok = True
                    break
                except Exception as exc:
                    last_err = str(exc)[:300]
                    db.add_log("warn", "forward", f"{label} 第{attempt}次失败: {last_err}")
                    if attempt < retry_limit:
                        await asyncio.sleep(min(2 ** attempt, 8))
            if ok:
                results.append(f"{label}: OK")
            else:
                all_ok = False
                results.append(f"{label}: 失败({last_err})")

    detail = "; ".join(results) if results else "无启用的转发目标"
    return all_ok, detail
