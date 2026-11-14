"""号码归一化。

同一个号码在不同来源下写法不同，不收敛就会把会话拆成两个：
  - 模块上报的发送方：`13300000000`（不带国家码）
  - 网页里手输的：`+8613300000000` / `8613300000000` / `133 0000 0000`
"""
from __future__ import annotations

import re

# 中国大陆手机号：1 开头 11 位
_CN_MOBILE = re.compile(r"^1[3-9]\d{9}$")


def normalize_peer(raw: str) -> str:
    """收敛成 `+86` 前缀的纯数字；无法识别时保留原样（仅去掉分隔符）。"""
    s = (raw or "").strip()
    if not s:
        return ""

    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return s

    if digits.startswith("0086"):
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    elif has_plus:
        # 已经带国家码的号码不再猜（否则 +14155552671 会被误判成国内号）
        return "+" + digits

    if _CN_MOBILE.match(digits):
        return "+86" + digits
    return ("+" if has_plus else "") + digits
