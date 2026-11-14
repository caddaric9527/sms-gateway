"""运营商推断自测：python -m tests.test_operator"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.operator import operator_from_imsi  # noqa: E402

FAILED = []


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(f"{name}: 期望 {expected!r}, 实际 {actual!r}")


check("电信 46011（本机实测）", operator_from_imsi("460110000000000"), "中国电信")
check("电信 46003", operator_from_imsi("460030000000000"), "中国电信")
check("移动 46000", operator_from_imsi("460001234567890"), "中国移动")
check("移动 46007", operator_from_imsi("460071234567890"), "中国移动")
check("联通 46001", operator_from_imsi("460011234567890"), "中国联通")
check("联通 46009", operator_from_imsi("460091234567890"), "中国联通")
check("广电 46015", operator_from_imsi("460151234567890"), "中国广电")
check("未知 MNC 回落地区", operator_from_imsi("460991234567890"), "中国")
check("香港 454", operator_from_imsi("454001234567890"), "中国香港")
check("空串", operator_from_imsi(""), "")
check("太短", operator_from_imsi("4601"), "")
check("带空格横线", operator_from_imsi("460 11-5058721294"), "中国电信")

print()
if FAILED:
    print(f"{len(FAILED)} 项失败:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
