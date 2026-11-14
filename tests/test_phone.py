"""号码归一化自测：python -m tests.test_phone"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.phone import normalize_peer  # noqa: E402

FAILED = []


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(f"{name}: 期望 {expected!r}, 实际 {actual!r}")


CN = "+8613300000000"
for form in ("13300000000", "+8613300000000", "8613300000000",
             "008613300000000", "133 0000 0000", "133-0000-0000",
             " +86 133 0000 0000 "):
    check(f"归一化 {form!r}", normalize_peer(form), CN)

check("短号 10086", normalize_peer("10086"), "10086")
check("短号 95588", normalize_peer("95588"), "95588")
check("国际号 US", normalize_peer("+14155552671"), "+14155552671")
check("国际号 HK", normalize_peer("+85212345678"), "+85212345678")
check("空串", normalize_peer(""), "")
check("纯空格", normalize_peer("   "), "")
check("非数字", normalize_peer("abc"), "abc")
check("1 开头非手机号段", normalize_peer("12345678901"), "12345678901")

print()
if FAILED:
    print(f"{len(FAILED)} 项失败:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
