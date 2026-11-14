"""从 IMSI 反推运营商。

LuatOS 的 mobile 库没有直接给运营商名称，但 IMSI 前 5 位就是 MCC+MNC，
足以判断归属运营商，且不需要重新刷固件。
"""
from __future__ import annotations

# MCC+MNC -> 运营商名称（中国大陆）
_CN_MNC = {
    # 中国移动
    "46000": "中国移动", "46002": "中国移动", "46004": "中国移动",
    "46007": "中国移动", "46008": "中国移动", "46020": "中国移动",
    # 中国联通
    "46001": "中国联通", "46006": "中国联通", "46009": "中国联通", "46010": "中国联通",
    # 中国电信
    "46003": "中国电信", "46005": "中国电信", "46011": "中国电信", "46012": "中国电信",
    # 中国广电
    "46015": "中国广电",
}

# MCC -> 地区（无法细分到具体运营商时显示）
_MCC_AREA = {
    "460": "中国", "461": "中国",
    "454": "中国香港", "455": "中国澳门", "466": "中国台湾",
}


def operator_from_imsi(imsi: str) -> str:
    """IMSI 前 5 位匹配运营商；匹配不到时退回地区名，再不行返回空串。"""
    digits = "".join(ch for ch in (imsi or "") if ch.isdigit())
    if len(digits) < 5:
        return ""
    mcc_mnc = digits[:5]
    if mcc_mnc in _CN_MNC:
        return _CN_MNC[mcc_mnc]
    return _MCC_AREA.get(digits[:3], "")
