"""PDU 编解码自测：python -m tests.test_pdu"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.pdu import (  # noqa: E402
    build_submit_pdus,
    decode_pdu,
    gsm7_decode,
    gsm7_encode,
    pdu_length,
)

FAILED = []


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        print(f"      expect: {expected!r}")
        print(f"      actual: {actual!r}")
        FAILED.append(name)


# 1. GSM7 基本表往返
text_en = "Hello SMS gateway! 12345"
data, septet_count = gsm7_encode(text_en)
check("gsm7 encode/decode 英文", gsm7_decode(data, septet_count), text_en)

# 2. GSM7 扩展表 (€ { } [ ] | ^ ~ \)
text_ext = "Price 5€ [ok] {x} | ~ ^ \\"
data, n = gsm7_encode(text_ext)
check("gsm7 扩展字符往返", gsm7_decode(data, n), text_ext)

# 3. 手工构造的 SMS-DELIVER (UCS2 / 中文)，独立验证解析器
#    +8613800138000 @ 2026-11-11 07:09:00 +08:00  "测试"
deliver_pdu = (
    "00"          # SCA 长度 0
    "04"          # first octet: DELIVER, MMS=1
    "0D"          # 发起方号码 13 位
    "91"          # 国际
    "683108108300F0"  # 8613800138000
    "00"          # PID
    "08"          # DCS: UCS2
    "62111170900023"  # SCTS
    "04"          # UDL: 4 octets
    "6D4B8BD5"    # "测试"
)
d = decode_pdu(deliver_pdu)
check("deliver 号码解析", d.sender, "+8613800138000")
check("deliver 中文正文", d.text, "测试")
check("deliver 时间戳", d.timestamp, "2026-11-11T07:09:00+08:00")
check("deliver 编码识别", d.encoding, "ucs2")

# 4. 手工构造的 SMS-DELIVER (GSM7)：UD 用编码器生成，验证 UDL/septet 对齐
ud_hex, ud_septets = gsm7_encode("HelloHello")
gsm7_deliver = (
    "00"              # SCA 长度 0
    "00"              # first octet: DELIVER，无 UDHI
    "0D"              # 号码 13 位
    "91"              # 国际
    "683108108300F0"  # +8613800138000
    "00"              # PID
    "00"              # DCS: GSM7
    "62111170900023"  # SCTS
    f"{ud_septets:02X}"
    + ud_hex.hex().upper()
)
d2 = decode_pdu(gsm7_deliver)
check("deliver GSM7 正文", d2.text, "HelloHello")
check("deliver GSM7 编码识别", d2.encoding, "gsm7")

# 5. 发送：短英文
pdus = build_submit_pdus("+8613800138000", "Test 123")
check("submit 短英文单条", len(pdus), 1)
sub = decode_pdu(pdus[0])
check("submit 回解号码", sub.sender, "+8613800138000")
check("submit 回解正文", sub.text, "Test 123")
check("submit TPDU 长度", pdu_length(pdus[0]), (len(pdus[0]) - 2) // 2)

# 6. 发送：短中文
pdus = build_submit_pdus("+8613800138000", "你的验证码是 123456")
check("submit 中文单条", len(pdus), 1)
check("submit 中文回解", decode_pdu(pdus[0]).text, "你的验证码是 123456")

# 7. 发送：长中文（>70 字）自动分片
long_cn = "这是一条很长的中文短信用于测试长短信分片功能" * 6
pdus = build_submit_pdus("+8613800138000", long_cn, concat_ref=42)
check("长中文分片条数", len(pdus), -(-len(long_cn) // 67))
seqs = [decode_pdu(p).concat_seq for p in pdus]
check("长中文分片序号", seqs, list(range(1, len(pdus) + 1)))
check("长中文分片总数字段", decode_pdu(pdus[0]).concat_total, len(pdus))
rebuilt = "".join(decode_pdu(p).text for p in pdus)
check("长中文拼回原文", rebuilt, long_cn)

# 8. 发送：长英文（>160 字，GSM7 分片，每片 153）
long_en = "A" * 400
pdus = build_submit_pdus("+8613800138000", long_en, concat_ref=7)
check("长英文分片条数", len(pdus), -(-400 // 153))
check("长英文拼回原文", "".join(decode_pdu(p).text for p in pdus), long_en)
check("长英文每片 UDL", [decode_pdu(p).concat_total for p in pdus], [3, 3, 3])

# 9. 带 SMSC 前缀的 PDU（部分模块会带）
with_smsc = "07913396050000F0" + deliver_pdu[2:]
d3 = decode_pdu(with_smsc)
check("含 SMSC 的 PDU 正文", d3.text, "测试")
check("含 SMSC 的 PDU 号码", d3.sender, "+8613800138000")

print()
if FAILED:
    print(f"{len(FAILED)} 项失败: {FAILED}")
    sys.exit(1)
print("全部通过")
