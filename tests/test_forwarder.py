"""转发通道自测：python -m tests.test_forwarder

锁住一件事：企业微信/钉钉这类群机器人**出错时 HTTP 仍是 200**，只在 body 里回
`{"errcode":93000,...}`。只看状态码会把"key 填错 / 频率超限 / 内容超长"当成发送成功，
用户界面上完全看不到异常。所以 errcode 必须单独校验，且不能误伤正常 webhook。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app.forwarder as fw  # noqa: E402

FAILED = []


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(f"{name}: 期望 {expected!r}, 实际 {actual!r}")


def check_true(name, cond):
    check(name, bool(cond), True)


# ---------------------------------------------------------------- 机器人 errcode
check("企业微信成功（errcode=0）不报错",
      fw.bot_error({"errcode": 0, "errmsg": "ok"}), "")
check("errcode 是字符串 0 也算成功",
      fw.bot_error({"errcode": "0", "errmsg": "ok"}), "")
check_true("key 填错（93000）要报错",
           "93000" in fw.bot_error({"errcode": 93000, "errmsg": "invalid webhook url"}))
check_true("频率超限（45009）要报错",
           "45009" in fw.bot_error({"errcode": 45009, "errmsg": "api freq out of limit"}))
check_true("内容超长（40058）要报错",
           "40058" in fw.bot_error({"errcode": 40058, "errmsg": "content exceed max length"}))

# 普通 webhook 返回任意 JSON 不能被误判
check("普通 webhook（无 errcode 字段）不报错",
      fw.bot_error({"ok": True, "id": 123}), "")
check("返回 list 不报错", fw.bot_error([1, 2, 3]), "")
check("返回字符串不报错", fw.bot_error("success"), "")
check("返回 None 不报错", fw.bot_error(None), "")

# ---------------------------------------------------------------- 模板渲染
CTX = {
    "from": "106900", "text": "验证码 123456", "time": "2026-11-15 07:20",
    "device": "air780epm", "device_id": "860000000000001",
    "rule": "验证码", "id": 7,
}
check("占位符渲染",
      fw._render("来自 {from}：{text}", CTX), "来自 106900：验证码 123456")
check("未知占位符原样保留（不抛异常）",
      fw._render("来自 {unknown}", CTX), "来自 {unknown}")

# 企业微信模板必须是合法 JSON，否则会被当成纯文本发出去（企业微信报 40008）
wx_tpl = '{"msgtype":"text","text":{"content":"【短信】{from}\\n{text}"}}'
import json  # noqa: E402

rendered = fw._render(wx_tpl, CTX)
try:
    payload = json.loads(rendered)
    ok_json = payload["msgtype"] == "text" and "106900" in payload["text"]["content"]
except (json.JSONDecodeError, KeyError, TypeError):
    ok_json = False
check_true("企业微信模板渲染后是合法 JSON 且字段正确", ok_json)

check_true("正文里真的有换行符（\\n 被 JSON 解码）",
           "\n" in json.loads(rendered)["text"]["content"])

# ---------------------------------------------- 正文含引号/换行时 JSON 不能被打散
# 短信里出现 `"` 或换行是常态（`你的"验证码"是\n123456`）。若直接替换进
# `{"content":"{text}"}` 会变成非法 JSON，退回 text/plain 后企业微信报 40058。
dirty_ctx = dict(CTX, text='你的"验证码"是\n123456')
payload = fw._render_json(wx_tpl, dirty_ctx)
check_true("含双引号+换行的正文仍能渲染成合法 JSON", isinstance(payload, dict))
check("双引号被正确转义",
      payload["text"]["content"], '【短信】106900\n你的"验证码"是\n123456')

backslash_ctx = dict(CTX, text="C:\\temp\\a.txt")
payload2 = fw._render_json(wx_tpl, backslash_ctx)
check_true("含反斜杠的正文仍是合法 JSON", isinstance(payload2, dict))
check_true("反斜杠保持原样", payload2["text"]["content"].endswith("C:\\temp\\a.txt"))

# 数字占位符不能因为走转义分支被加引号
num_tpl = '{"id":{id},"text":"{text}"}'
num_payload = fw._render_json(num_tpl, CTX)
check("数字占位符保持 int 类型", num_payload["id"], 7)
check_true("数字占位符不是字符串", isinstance(num_payload["id"], int))

# 非 JSON 模板必须返回 None（交给调用方按纯文本发）
check("纯文本模板不走 JSON 分支",
      fw._render_json("{from}：{text}", CTX), None)
check("JSON 合法但类型是标量的模板也按纯文本处理",
      fw._render_json("{id}", CTX), None)

# 用户显式指定的 Content-Type 优先
check("用户指定 text/plain 时按纯文本处理",
      fw._wants_plain_text({"headers": {"Content-Type": "text/plain"}}), True)
check("用户指定 application/json 时走 JSON",
      fw._wants_plain_text({"headers": {"content-type": "application/json"}}), False)
check("没指定 Content-Type 时默认走 JSON",
      fw._wants_plain_text({"headers": {"Title": "x"}}), False)

# ---------------------------------------------------------------- SMTP 主题净化
# 主题模板里带 {text} 时，正文的换行会进 Header，smtplib 直接抛
# "Header values may not contain linefeed"。压成一行即可。
cap_mail = {}


class FakeSMTP:
    def __init__(self, *a, **kw):
        pass

    def login(self, u, p):
        pass

    def sendmail(self, sender, rcpt, text):
        cap_mail["text"] = text

    def quit(self):
        pass


fw.smtplib.SMTP_SSL = FakeSMTP
fw.db.get_settings = lambda: {
    "smtp_host": "smtp.example.com", "smtp_port": "465", "smtp_ssl": "1",
    "smtp_user": "bot@example.com", "smtp_pass": "secret", "smtp_from": "bot@example.com",
}

try:
    fw._send_smtp_sync(
        {"to": "me@example.com", "subject": "[{from}] {text}"},
        {"peer": "106900", "text": "验证码\n123456", "ts": "2026-11-15 07:20", "id": 7},
        {"name": "验证码"},
    )
    header = [ln for ln in cap_mail["text"].splitlines() if ln.startswith("Subject:")]
    check_true("主题含 {text} 换行时邮件仍能构造", bool(header))
    # 中文主题会被 RFC 2047 编码成 =?utf-8?b?...?=，得先解码才能比对
    from email.header import decode_header  # noqa: E402
    raw_subject = header[0].split("Subject:", 1)[1].strip() if header else ""
    subject = "".join(
        p.decode(enc or "utf-8") if isinstance(p, bytes) else p
        for p, enc in decode_header(raw_subject)
    )
    check_true("主题里没有残留裸换行", "验证码 123456" in subject)
except Exception as exc:  # noqa: BLE001
    check(f"SMTP 主题净化（异常: {exc}）", "未抛异常", "抛异常了")

print()
if FAILED:
    print(f"{len(FAILED)} 项失败:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
