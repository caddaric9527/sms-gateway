"""SQLite 持久层：短信、分片聚合、转发规则、设置、事件日志。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Iterator

from .config import ensure_data_dir, settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    direction     TEXT NOT NULL,              -- in | out
    peer          TEXT NOT NULL,
    text          TEXT NOT NULL DEFAULT '',
    ts            TEXT NOT NULL,              -- ISO8601
    encoding      TEXT DEFAULT 'gsm7',
    raw_pdu       TEXT DEFAULT '',
    modem_index   INTEGER,
    concat_ref    INTEGER,
    concat_total  INTEGER DEFAULT 1,
    concat_seq    INTEGER DEFAULT 1,
    concat_key    TEXT,
    parts         TEXT DEFAULT '{}',          -- 长短信分片 {"seq": "text"}
    complete      INTEGER DEFAULT 1,
    status        TEXT DEFAULT 'received',    -- received | sent | failed | sending
    error         TEXT DEFAULT '',
    is_read       INTEGER DEFAULT 0,
    forwarded     INTEGER DEFAULT 0,
    forward_log   TEXT DEFAULT '',
    device_id     TEXT DEFAULT '',            -- 收/发所属模块（IMEI 优先，未取到时用端口）
    fingerprint   TEXT UNIQUE,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_peer_ts ON messages(peer, ts DESC);
CREATE INDEX IF NOT EXISTS idx_msg_concat ON messages(concat_key);
CREATE INDEX IF NOT EXISTS idx_msg_created ON messages(created_at DESC);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    enabled     INTEGER DEFAULT 1,
    match_type  TEXT DEFAULT 'all',           -- all | contains | regex | sender
    pattern     TEXT DEFAULT '',
    targets     TEXT NOT NULL DEFAULT '[]',   -- [{"type":"webhook",...}]
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,             -- IMEI；取不到时用 USB 拓扑键
    name        TEXT DEFAULT '',
    port        TEXT DEFAULT '',
    usb_group   TEXT DEFAULT '',
    model       TEXT DEFAULT '',
    firmware    TEXT DEFAULT '',
    sim_number  TEXT DEFAULT '',
    iccid       TEXT DEFAULT '',
    protocol    TEXT DEFAULT '',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL DEFAULT 'info',     -- info | warn | error
    source  TEXT NOT NULL DEFAULT 'app',
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts DESC);
"""

DEFAULT_RULE_SEED = [
    {
        "name": "全部转发示例（默认关闭）",
        "enabled": 0,
        "match_type": "all",
        "pattern": "",
        "targets": [
            {"type": "webhook", "url": "http://192.168.1.10:5678/webhook/sms", "enabled": True}
        ],
    },
    {
        "name": "验证码类短信",
        "enabled": 0,
        "match_type": "regex",
        "pattern": r"(验证码|校验码|动态码|code)[^\d]{0,10}(\d{4,8})",
        "targets": [
            {"type": "webhook", "url": "http://192.168.1.10:5678/webhook/sms-code", "enabled": True}
        ],
    },
]

DEFAULT_SETTINGS = {
    "auto_forward": "1",
    "auto_mark_read": "1",
    "signature": "",
    "webhook_timeout": "10",
    "smtp_host": "",
    "smtp_port": "465",
    "smtp_ssl": "1",
    "smtp_user": "",
    "smtp_pass": "",
    "smtp_from": "",
    "smtp_to": "",
    "retry_limit": "3",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    ensure_data_dir(settings.db_path)
    c = sqlite3.connect(settings.db_path, timeout=20)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=20000")
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()


def _ensure_column(c: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """给老库补列。SQLite 的 ALTER TABLE ADD COLUMN 是安全的增量操作。"""
    cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)
        # 必须先补列再建索引：老库没有 device_id 时，建索引会直接报错
        _ensure_column(c, "messages", "device_id", "TEXT DEFAULT ''")
        # 老数据的 device_id 留空表示"未知设备"，不参与多设备去重
        c.execute("CREATE INDEX IF NOT EXISTS idx_msg_device_ts ON messages(device_id, ts DESC)")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(k, v) VALUES(?, ?)", (k, v))
        count = c.execute("SELECT COUNT(*) FROM rules").fetchone()[0]
        if count == 0:
            for r in DEFAULT_RULE_SEED:
                c.execute(
                    "INSERT INTO rules(name, enabled, match_type, pattern, targets, created_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (r["name"], r["enabled"], r["match_type"], r["pattern"],
                     json.dumps(r["targets"], ensure_ascii=False), now_iso()),
                )


# --------------------------------------------------------------------------- #
# 设备
# --------------------------------------------------------------------------- #
def upsert_device(device_id: str, **info: Any) -> None:
    """登记/更新一台设备。device_id 用 IMEI，取不到时用 USB 拓扑键。"""
    if not device_id:
        return
    ts = now_iso()
    fields = {
        "name": info.get("name", ""),
        "port": info.get("port", ""),
        "usb_group": info.get("usb_group", ""),
        "model": info.get("model", ""),
        "firmware": info.get("firmware", ""),
        "sim_number": info.get("sim_number", ""),
        "iccid": info.get("iccid", ""),
        "protocol": info.get("protocol", ""),
    }
    with conn() as c:
        c.execute(
            "INSERT INTO devices(device_id, name, port, usb_group, model, firmware,"
            " sim_number, iccid, protocol, first_seen, last_seen)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(device_id) DO UPDATE SET"
            "  name=excluded.name, port=excluded.port, usb_group=excluded.usb_group,"
            "  model=excluded.model, firmware=excluded.firmware,"
            "  sim_number=excluded.sim_number, iccid=excluded.iccid,"
            "  protocol=excluded.protocol, last_seen=excluded.last_seen",
            (device_id, fields["name"], fields["port"], fields["usb_group"],
             fields["model"], fields["firmware"], fields["sim_number"],
             fields["iccid"], fields["protocol"], ts, ts),
        )


def list_devices() -> list[dict]:
    with conn() as c:
        rows = c.execute("SELECT * FROM devices ORDER BY first_seen").fetchall()
    return [dict(r) for r in rows]


def get_device(device_id: str) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- #
# 设置
# --------------------------------------------------------------------------- #
def get_settings() -> dict[str, str]:
    with conn() as c:
        rows = c.execute("SELECT k, v FROM settings").fetchall()
    return {r["k"]: r["v"] for r in rows}


def get_setting(key: str, default: str = "") -> str:
    with conn() as c:
        row = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def set_settings(items: dict[str, Any]) -> None:
    with conn() as c:
        for k, v in items.items():
            c.execute(
                "INSERT INTO settings(k, v) VALUES(?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, str(v)),
            )


# --------------------------------------------------------------------------- #
# 短信
# --------------------------------------------------------------------------- #
DEFAULT_MESSAGE_FIELDS = {
    "direction": "in", "peer": "", "text": "", "ts": "", "encoding": "gsm7",
    "raw_pdu": "", "concat_total": 1, "concat_seq": 1, "parts": "{}",
    "complete": 1, "status": "received", "error": "", "is_read": 0,
    "forwarded": 0, "forward_log": "", "device_id": "",
}


def insert_message(msg: dict[str, Any]) -> int | None:
    """按 fingerprint 去重插入；重复返回 None。缺失字段用表默认值，避免 NULL 覆盖。"""
    cols = (
        "direction", "peer", "text", "ts", "encoding", "raw_pdu", "modem_index",
        "concat_ref", "concat_total", "concat_seq", "concat_key", "parts",
        "complete", "status", "error", "is_read", "forwarded", "device_id",
        "fingerprint", "created_at",
    )
    values = []
    for k in cols:
        v = msg.get(k)
        if v is None:
            v = DEFAULT_MESSAGE_FIELDS.get(k)
        values.append(v)
    values[cols.index("created_at")] = msg.get("created_at") or now_iso()
    sql = (
        f"INSERT INTO messages({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        " ON CONFLICT(fingerprint) DO NOTHING"
    )
    with conn() as c:
        cur = c.execute(sql, values)
        return cur.lastrowid if cur.rowcount else None


def merge_part(peer: str, concat_key: str, seq: int, total: int, text: str,
               ref: int | None, ts: str, fingerprint: str,
               device_id: str = "") -> tuple[int | None, bool]:
    """合并长短信分片。返回 (message_id, 是否已完整)。

    分片的 SCTS 偶尔跨分钟，精确 key 匹配不到时，退化为「同号码 + 同 ref + 同总片数
    且尚未完整的最近一条记录」，避免长短信被拆成两条永不完整的记录。
    """
    with conn() as c:
        row = c.execute("SELECT id, parts FROM messages WHERE concat_key=?", (concat_key,)).fetchone()
        if row is None and ref is not None:
            row = c.execute(
                "SELECT id, parts FROM messages WHERE direction='in' AND peer=? AND concat_ref=?"
                " AND concat_total=? AND complete=0 AND device_id=? ORDER BY id DESC LIMIT 1",
                (peer, ref, total, device_id),
            ).fetchone()
            if row is not None:
                c.execute("UPDATE messages SET concat_key=? WHERE id=?", (concat_key, row["id"]))
        if row:
            parts = json.loads(row["parts"] or "{}")
            if str(seq) in parts:
                return row["id"], len(parts) >= total
            parts[str(seq)] = text
            complete = 1 if len(parts) >= total else 0
            merged = "".join(parts[k] for k in sorted(parts, key=lambda x: int(x)))
            c.execute(
                "UPDATE messages SET parts=?, text=?, complete=? WHERE id=?",
                (json.dumps(parts, ensure_ascii=False), merged, complete, row["id"]),
            )
            return row["id"], bool(complete)

    mid = insert_message({
        "direction": "in", "peer": peer, "text": text, "ts": ts,
        "concat_ref": ref, "concat_total": total, "concat_seq": seq,
        "concat_key": concat_key, "device_id": device_id,
        "parts": json.dumps({str(seq): text}, ensure_ascii=False),
        "complete": 1 if total <= 1 else 0, "status": "received",
        "fingerprint": fingerprint,
    })
    return mid, total <= 1


def list_messages(peer: str | None = None, query: str | None = None,
                  limit: int = 50, offset: int = 0, complete_only: bool = True,
                  device: str | None = None) -> list[dict]:
    where, args = [], []
    if peer:
        where.append("peer = ?")
        args.append(peer)
    if device:
        where.append("device_id = ?")
        args.append(device)
    if query:
        where.append("(text LIKE ? OR peer LIKE ?)")
        args.extend([f"%{query}%", f"%{query}%"])
    if complete_only:
        where.append("complete = 1")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    with conn() as c:
        rows = c.execute(
            f"SELECT * FROM messages {sql_where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
            (*args, limit, offset),
        ).fetchall()
    return [dict(r) for r in rows]


def get_message(mid: int) -> dict | None:
    with conn() as c:
        row = c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    return dict(row) if row else None


def delete_message(mid: int) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM messages WHERE id=?", (mid,))
        return bool(cur.rowcount)


def clear_messages(peer: str | None = None, device: str | None = None) -> int:
    where, args = [], []
    if peer:
        where.append("peer = ?")
        args.append(peer)
    if device:
        where.append("device_id = ?")
        args.append(device)
    sql = "DELETE FROM messages"
    if where:
        sql += " WHERE " + " AND ".join(where)
    with conn() as c:
        cur = c.execute(sql, args)
        return cur.rowcount


def list_threads(limit: int = 200) -> list[dict]:
    """按号码聚合会话，带最后一条消息与未读数。"""
    sql = """
    SELECT m.peer,
           m.text AS last_text,
           m.ts AS last_ts,
           m.direction AS last_direction,
           (SELECT COUNT(*) FROM messages u
             WHERE u.peer = m.peer AND u.direction='in' AND u.is_read=0 AND u.complete=1) AS unread,
           (SELECT COUNT(*) FROM messages t
             WHERE t.peer = m.peer AND t.complete=1) AS total
    FROM messages m
    JOIN (SELECT peer, MAX(id) AS max_id FROM messages WHERE complete=1 GROUP BY peer) x
      ON m.id = x.max_id
    ORDER BY m.ts DESC, m.id DESC
    LIMIT ?
    """
    with conn() as c:
        rows = c.execute(sql, (limit,)).fetchall()
    return [dict(r) for r in rows]


def mark_read(peer: str) -> None:
    with conn() as c:
        c.execute("UPDATE messages SET is_read=1 WHERE peer=? AND direction='in'", (peer,))


def mark_forwarded(mid: int, ok: bool, detail: str) -> None:
    with conn() as c:
        c.execute(
            "UPDATE messages SET forwarded=?, forward_log=? WHERE id=?",
            (1 if ok else 0, detail[:2000], mid),
        )


def set_modem_index(mid: int, index: int) -> None:
    with conn() as c:
        c.execute("UPDATE messages SET modem_index=? WHERE id=?", (index, mid))


def message_exists(fingerprint: str) -> bool:
    with conn() as c:
        return c.execute("SELECT 1 FROM messages WHERE fingerprint=?", (fingerprint,)).fetchone() is not None


def max_ts(direction: str = "in") -> str:
    with conn() as c:
        row = c.execute("SELECT MAX(ts) FROM messages WHERE direction=?", (direction,)).fetchone()
    return (row[0] or "") if row else ""


# --------------------------------------------------------------------------- #
# 转发规则
# --------------------------------------------------------------------------- #
def list_rules(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM rules"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    with conn() as c:
        rows = c.execute(sql).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["targets"] = json.loads(d["targets"] or "[]")
        out.append(d)
    return out


def upsert_rule(rule: dict[str, Any]) -> int:
    targets = json.dumps(rule.get("targets") or [], ensure_ascii=False)
    with conn() as c:
        if rule.get("id"):
            c.execute(
                "UPDATE rules SET name=?, enabled=?, match_type=?, pattern=?, targets=? WHERE id=?",
                (rule["name"], int(rule.get("enabled", 1)), rule.get("match_type", "all"),
                 rule.get("pattern", ""), targets, rule["id"]),
            )
            return int(rule["id"])
        cur = c.execute(
            "INSERT INTO rules(name, enabled, match_type, pattern, targets, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (rule["name"], int(rule.get("enabled", 1)), rule.get("match_type", "all"),
             rule.get("pattern", ""), targets, now_iso()),
        )
        return int(cur.lastrowid)


def delete_rule(rid: int) -> bool:
    with conn() as c:
        cur = c.execute("DELETE FROM rules WHERE id=?", (rid,))
        return bool(cur.rowcount)


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def add_log(level: str, source: str, message: str) -> None:
    with conn() as c:
        c.execute("INSERT INTO logs(ts, level, source, message) VALUES(?,?,?,?)",
                  (now_iso(), level, source, message[:4000]))


def list_logs(limit: int = 100, level: str | None = None) -> list[dict]:
    sql = "SELECT * FROM logs"
    args: Iterable[Any] = ()
    if level:
        sql += " WHERE level=?"
        args = (level,)
    sql += " ORDER BY id DESC LIMIT ?"
    with conn() as c:
        rows = c.execute(sql, (*args, limit)).fetchall()
    return [dict(r) for r in rows]


def prune_logs(days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with conn() as c:
        cur = c.execute("DELETE FROM logs WHERE ts < ?", (cutoff,))
        return cur.rowcount
