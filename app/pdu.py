"""SMS PDU (3GPP TS 23.040) 编解码。

只实现网关需要的部分：
  - 解码 SMS-DELIVER / SMS-SUBMIT / SMS-STATUS-REPORT
  - GSM 7bit (含扩展表) 与 UCS2 双向编解码
  - 长短信 UDH 拆解 (concat 8bit / 16bit)
  - 构造 SMS-SUBMIT，支持自动按长短信分片
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# GSM 7bit 字母表
# --------------------------------------------------------------------------- #
GSM7_BASIC = (
    "@£$¥èéùìòÇ\nØø\rÅå"
    "Δ_ΦΓΛΩΠΨΣΘΞ\x1bÆæßÉ"
    " !\"#¤%&'()*+,-./"
    "0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNO"
    "PQRSTUVWXYZÄÖÑÜ§"
    "¿abcdefghijklmno"
    "pqrstuvwxyzäöñüà"
)

GSM7_EXT = {
    0x0A: "\f",
    0x14: "^",
    0x28: "{",
    0x29: "}",
    0x2F: "\\",
    0x3C: "[",
    0x3D: "~",
    0x3E: "]",
    0x40: "|",
    0x65: "€",
}

_GSM7_BASIC_INDEX = {ch: i for i, ch in enumerate(GSM7_BASIC)}
_GSM7_EXT_INDEX = {ch: k for k, ch in GSM7_EXT.items()}

# UCS2 单条上限：140 octets。UDH 占 6 octets。
MAX_UD_OCTETS = 140
CONCAT_UDH_OCTETS = 6


# --------------------------------------------------------------------------- #
# 7bit 打包 / 解包
# --------------------------------------------------------------------------- #
def unpack_septets(data: bytes, septet_count: int, skip_septets: int = 0) -> list[int]:
    """从字节流中解出 septet 列表，可跳过前 N 个 septet（用于 UDH 填充）。"""
    septets: list[int] = []
    total = min(septet_count, (len(data) * 8) // 7)
    for i in range(skip_septets, total):
        bit_pos = i * 7
        byte_idx = bit_pos >> 3
        shift = bit_pos & 7
        lo = data[byte_idx] if byte_idx < len(data) else 0
        hi = data[byte_idx + 1] if byte_idx + 1 < len(data) else 0
        septets.append(((lo | (hi << 8)) >> shift) & 0x7F)
    return septets


def pack_septets(septets: list[int]) -> bytes:
    out = bytearray()
    acc = 0
    bits = 0
    for septet in septets:
        acc |= (septet & 0x7F) << bits
        bits += 7
        while bits >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            bits -= 8
    if bits > 0:
        out.append(acc & 0xFF)
    return bytes(out)


def gsm7_decode(data: bytes, septet_count: int, skip_septets: int = 0) -> str:
    septets = unpack_septets(data, septet_count, skip_septets)
    chars: list[str] = []
    escape = False
    for s in septets:
        if escape:
            chars.append(GSM7_EXT.get(s, GSM7_BASIC[s]))
            escape = False
            continue
        if s == 0x1B:
            escape = True
            continue
        chars.append(GSM7_BASIC[s])
    return "".join(chars)


def gsm7_encode(text: str) -> tuple[bytes, int]:
    """返回 (打包字节, septet 数)。文本必须全部在 GSM7 表内。"""
    septets: list[int] = []
    for ch in text:
        if ch in _GSM7_BASIC_INDEX:
            septets.append(_GSM7_BASIC_INDEX[ch])
        elif ch in _GSM7_EXT_INDEX:
            septets.append(0x1B)
            septets.append(_GSM7_EXT_INDEX[ch])
        else:
            raise ValueError(f"字符 {ch!r} 不在 GSM7 字母表内")
    return pack_septets(septets), len(septets)


def is_gsm7_compatible(text: str) -> bool:
    return all(ch in _GSM7_BASIC_INDEX or ch in _GSM7_EXT_INDEX for ch in text)


# --------------------------------------------------------------------------- #
# 地址 / 时间编解码
# --------------------------------------------------------------------------- #
def decode_semi_octets(hexstr: str) -> str:
    """交换半字节顺序：'419 ...' -> 号码字符串。"""
    out = []
    for i in range(0, len(hexstr), 2):
        pair = hexstr[i : i + 2]
        if len(pair) == 2:
            out.append(pair[1] + pair[0])
        else:
            out.append(pair[0])
    return "".join(out).replace("F", "").replace("f", "")


def encode_semi_octets(number: str) -> str:
    digits = "".join(ch for ch in number if ch.isdigit() or ch == "*" or ch == "#")
    if len(digits) % 2:
        digits += "F"
    out = []
    for i in range(0, len(digits), 2):
        out.append(digits[i + 1] + digits[i])
    return "".join(out)


def decode_address(toa: int, raw_hex: str, addr_len: int) -> str:
    ton = (toa >> 4) & 0x07
    if ton == 5:  # alphanumeric
        try:
            data = bytes.fromhex(raw_hex)
            return gsm7_decode(data, (addr_len * 4) // 7)
        except Exception:
            return decode_semi_octets(raw_hex)
    number = decode_semi_octets(raw_hex)
    if (toa >> 7) & 1:  # international
        return "+" + number
    return number


def decode_scts(hexstr: str) -> str:
    """7 octets 时间戳 -> ISO8601 字符串（带时区）。"""
    b = bytes.fromhex(hexstr[:14])

    def swap(x: int) -> int:
        return ((x & 0x0F) * 10) + ((x >> 4) & 0x0F)

    year, month, day, hour, minute, second = (swap(b[i]) for i in range(6))
    tz_byte = b[6]
    # 时区：半字节交换后的 BCD，单位 15 分钟，bit3 为符号位
    negative = bool(tz_byte & 0x08)
    quarters = (tz_byte & 0x07) * 10 + ((tz_byte >> 4) & 0x0F)
    offset_minutes = quarters * 15
    if negative:
        offset_minutes = -offset_minutes
    try:
        tzinfo = timezone(timedelta(minutes=offset_minutes))
        full_year = 2000 + year if year < 70 else 1900 + year
        dt = datetime(full_year, month, day, hour, minute, second, tzinfo=tzinfo)
        return dt.isoformat()
    except ValueError:
        return ""


# --------------------------------------------------------------------------- #
# 解码
# --------------------------------------------------------------------------- #
@dataclass
class DecodedSms:
    kind: str = "deliver"  # deliver | submit | status_report
    sender: str = ""
    text: str = ""
    timestamp: str = ""
    encoding: str = "gsm7"  # gsm7 | ucs2 | 8bit
    dcs: int = 0
    smsc: str = ""
    concat_ref: int | None = None
    concat_total: int = 1
    concat_seq: int = 1
    raw: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def is_concat(self) -> bool:
        return self.concat_total > 1


def _decode_ud(ud: bytes, dcs: int, udl: int, udhi: bool) -> tuple[str, str, tuple[int, int, int] | None]:
    alphabet = (dcs >> 2) & 0x03
    encoding = {0: "gsm7", 1: "8bit", 2: "ucs2"}.get(alphabet, "gsm7")

    udh_len = 0
    concat = None
    if udhi and ud:
        udh_len = ud[0] + 1  # 含 UDHL 自身
        concat = _parse_concat(ud[:udh_len])

    if encoding == "ucs2":
        payload = ud[udh_len:]
        try:
            text = payload.decode("utf-16-be", errors="replace")
        except Exception:
            text = ""
    elif encoding == "8bit":
        text = ud[udh_len:].decode("latin-1", errors="replace")
    else:
        # GSM7：UDL 是 septet 数，UDH 需要按 septet 边界填充
        if udhi:
            fill_bits = (7 - ((udh_len * 8) % 7)) % 7
            skip_septets = ((udh_len * 8) + fill_bits) // 7
        else:
            skip_septets = 0
        total_septets = udl if udl else (len(ud) * 8) // 7
        text = gsm7_decode(ud, total_septets, skip_septets)
    return text, encoding, concat


def _parse_concat(udh: bytes) -> tuple[int, int, int] | None:
    i = 1
    while i + 1 < len(udh):
        iei = udh[i]
        iedl = udh[i + 1]
        ied = udh[i + 2 : i + 2 + iedl]
        if iei == 0x00 and iedl >= 3:
            return (ied[0], ied[1], ied[2])
        if iei == 0x08 and iedl >= 4:
            return ((ied[0] << 8) | ied[1], ied[2], ied[3])
        i += 2 + iedl
    return None


def decode_pdu(pdu_hex: str) -> DecodedSms:
    pdu_hex = pdu_hex.strip().replace(" ", "")
    data = bytes.fromhex(pdu_hex)
    pos = 0

    sca_len = data[pos]
    pos += 1
    smsc = ""
    if sca_len:
        toa = data[pos]
        raw = data[pos + 1 : pos + sca_len].hex().upper()
        smsc = decode_address(toa, raw, sca_len - 1)
        pos += sca_len

    first_octet = data[pos]
    pos += 1
    mti = first_octet & 0x03
    udhi = bool(first_octet & 0x40)

    sms = DecodedSms(raw=pdu_hex, smsc=smsc, concat_total=1, concat_seq=1)

    if mti == 0x00:  # SMS-DELIVER
        addr_len = data[pos]
        pos += 1
        toa = data[pos]
        pos += 1
        addr_octets = (addr_len + 1) // 2
        raw_addr = data[pos : pos + addr_octets].hex().upper()
        pos += addr_octets
        sms.sender = decode_address(toa, raw_addr, addr_len)

        pos += 1  # PID
        dcs = data[pos]
        pos += 1
        sms.timestamp = decode_scts(data[pos : pos + 7].hex().upper())
        pos += 7
        udl = data[pos]
        pos += 1
        ud = data[pos:]
        text, encoding, concat = _decode_ud(ud, dcs, udl, udhi)
        sms.text = text
        sms.encoding = encoding
        sms.dcs = dcs
        if concat:
            sms.concat_ref, sms.concat_total, sms.concat_seq = concat
        return sms

    if mti == 0x01:  # SMS-SUBMIT（读模块存储时可能出现）
        pos += 1  # MR
        addr_len = data[pos]
        pos += 1
        toa = data[pos]
        pos += 1
        addr_octets = (addr_len + 1) // 2
        raw_addr = data[pos : pos + addr_octets].hex().upper()
        pos += addr_octets
        sms.kind = "submit"
        sms.sender = decode_address(toa, raw_addr, addr_len)
        pos += 1  # PID
        dcs = data[pos]
        pos += 1
        vpf = (first_octet >> 3) & 0x03
        pos += {0: 0, 1: 1, 2: 1, 3: 7}[vpf]
        udl = data[pos]
        pos += 1
        text, encoding, concat = _decode_ud(data[pos:], dcs, udl, udhi)
        sms.text = text
        sms.encoding = encoding
        sms.dcs = dcs
        if concat:
            sms.concat_ref, sms.concat_total, sms.concat_seq = concat
        return sms

    if mti == 0x02:  # SMS-STATUS-REPORT
        pos += 1  # MR
        addr_len = data[pos]
        pos += 1
        toa = data[pos]
        pos += 1
        addr_octets = (addr_len + 1) // 2
        raw_addr = data[pos : pos + addr_octets].hex().upper()
        pos += addr_octets
        sms.kind = "status_report"
        sms.sender = decode_address(toa, raw_addr, addr_len)
        sms.timestamp = decode_scts(data[pos : pos + 7].hex().upper())
        pos += 7
        sms.timestamp = decode_scts(data[pos : pos + 7].hex().upper()) if pos + 7 <= len(data) else sms.timestamp
        pos += 7
        sms.extra["status"] = data[pos] if pos < len(data) else None
        return sms

    sms.kind = "unknown"
    return sms


# --------------------------------------------------------------------------- #
# 编码：构造 SMS-SUBMIT
# --------------------------------------------------------------------------- #
def _build_submit_single(number: str, text: str, *, udhi: bool, ref: int, total: int, seq: int,
                         report: bool) -> str:
    stripped = number.strip()
    digits = "".join(ch for ch in stripped if ch.isdigit())
    if stripped.startswith("+") or (len(digits) == 13 and digits.startswith("86")):
        toa = 0x91
    else:
        toa = 0x81

    da = encode_semi_octets(digits)
    da_len = len(digits) + (1 if len(digits) % 2 else 0)

    first_octet = 0x11  # MTI=SUBMIT, VPF=relative(1 octet)
    if report:
        first_octet |= 0x20
    if udhi:
        first_octet |= 0x40

    udh = b""
    if udhi:
        udh = bytes([0x05, 0x00, 0x03, ref & 0xFF, total & 0xFF, seq & 0xFF])

    if is_gsm7_compatible(text):
        dcs = 0x00
        septets: list[int] = []
        for ch in text:
            if ch in _GSM7_BASIC_INDEX:
                septets.append(_GSM7_BASIC_INDEX[ch])
            else:
                septets.append(0x1B)
                septets.append(_GSM7_EXT_INDEX[ch])
        if udhi:
            fill_bits = (7 - ((len(udh) * 8) % 7)) % 7
            ud_bytes = _pack_with_udh(udh, septets, fill_bits)
            udl = ((len(udh) * 8) + fill_bits) // 7 + len(septets)
        else:
            ud_bytes = pack_septets(septets)
            udl = len(septets)
    else:
        dcs = 0x08
        body = text.encode("utf-16-be")
        ud_bytes = udh + body
        udl = len(ud_bytes)

    parts = ["00", f"{first_octet:02X}", "00", f"{da_len:02X}", f"{toa:02X}", da,
             "00", f"{dcs:02X}", "AA", f"{udl:02X}", ud_bytes.hex().upper()]
    return "".join(parts)


def _pack_with_udh(udh: bytes, septets: list[int], fill_bits: int) -> bytes:
    """UDH 字节在前，septet 数据在其后按位拼接。"""
    udh_bits = "".join(f"{b:08b}"[::-1] for b in udh)  # LSB first
    udh_bits += "0" * fill_bits
    data_bits = "".join(f"{s:07b}"[::-1] for s in septets)
    all_bits = udh_bits + data_bits
    pad = (-len(all_bits)) % 8
    all_bits += "0" * pad
    out = bytearray()
    for i in range(0, len(all_bits), 8):
        chunk = all_bits[i : i + 8]
        val = sum((1 << idx) for idx, ch in enumerate(chunk) if ch == "1")
        out.append(val)
    return bytes(out)


def build_submit_pdus(number: str, text: str, *, concat_ref: int | None = None,
                      report: bool = False) -> list[str]:
    """把一条逻辑短信拆成若干 PDU（自动处理长短信）。"""
    if concat_ref is None:
        concat_ref = int(datetime.now().timestamp()) & 0xFF

    gsm7 = is_gsm7_compatible(text)
    if gsm7:
        single_limit = 160
        multi_limit = 153
    else:
        single_limit = 70
        multi_limit = 67

    if len(text) <= single_limit:
        return [_build_submit_single(number, text, udhi=False, ref=concat_ref,
                                    total=1, seq=1, report=report)]

    chunks = [text[i : i + multi_limit] for i in range(0, len(text), multi_limit)]
    total = len(chunks)
    return [
        _build_submit_single(number, chunk, udhi=True, ref=concat_ref,
                             total=total, seq=i + 1, report=report)
        for i, chunk in enumerate(chunks)
    ]


def pdu_length(pdu_hex: str) -> int:
    """TPDU 长度（不含 SMSC 部分），AT+CMGS=<n> 需要它。"""
    data = bytes.fromhex(pdu_hex)
    sca_len = data[0]
    return len(data) - 1 - sca_len
