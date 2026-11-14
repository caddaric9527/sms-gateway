"""SIM PIN 解锁安全逻辑自测：python -m tests.test_pin

重点不是"能不能解锁"，而是**绝不能解锁失败把卡锁死**：
  - 没配 SIM_PIN 时一次都不许发
  - SIM 状态未知时不许猜
  - SIM 正常时（sim=true）不许重复验证
  - 每次启动最多 2 次（PIN 错 3 次卡会被锁，只能用 PUK 解）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.luatos import LuatosModem  # noqa: E402

FAILED = []


def check(name, actual, expected):
    ok = actual == expected
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        FAILED.append(f"{name}: 期望 {expected!r}, 实际 {actual!r}")


class FakeModem(LuatosModem):
    """只记录下发的指令，不碰串口。"""

    def __init__(self) -> None:
        super().__init__(port="fake")
        self.sent: list[str] = []
        self.reply = {"ok": False}

    def _request(self, line, tag, timeout):  # type: ignore[override]
        self.sent.append(line)
        return dict(self.reply)


def build(pin: str) -> FakeModem:
    settings.sim_pin = pin
    return FakeModem()


# 1. 没配 PIN：一次都不发
m = build("")
m._last_status = {"sim": False}
m._try_unlock_pin()
check("未配置 SIM_PIN 时不发送", len(m.sent), 0)

# 2. 状态未知：不猜（避免对正常卡乱验证）
m = build("1234")
m._last_status = {}
m._try_unlock_pin()
check("状态未知时不发送", len(m.sent), 0)

# 3. SIM 正常：不重复验证
m = build("1234")
m._last_status = {"sim": True}
m._try_unlock_pin()
check("SIM 正常时不发送", len(m.sent), 0)

# 4. SIM 未就绪：发一次，且限次 2 次
m = build("1234")
m._last_status = {"sim": False}
m._try_unlock_pin()
check("未就绪时发送 1 次", len(m.sent), 1)
check("指令格式正确", m.sent[0], 'SETPIN {"pin": "1234"}')

m._last_status = {"sim": False}
m._try_unlock_pin()
m._last_status = {"sim": False}
m._try_unlock_pin()
m._last_status = {"sim": False}
m._try_unlock_pin()
check("连续调用最多 2 次（防锁卡）", len(m.sent), 2)

# 5. 解锁成功后清空缓存，让下一轮 STATUS 拿到新状态
m = build("1234")
m._last_status = {"sim": False}
m.reply = {"ok": True, "iccid": "8986"}
m._try_unlock_pin()
check("成功后清空状态缓存", m._last_status, {})
check("成功后清空错误", m.last_error, "")

# 6. 解锁失败给出可读错误
m = build("1234")
m._last_status = {"sim": False}
m._try_unlock_pin()
check("失败写入 last_error", m.last_error, "SIM PIN 验证失败（第 1 次）")

settings.sim_pin = ""

print()
if FAILED:
    print(f"{len(FAILED)} 项失败:")
    for f in FAILED:
        print("  -", f)
    sys.exit(1)
print("全部通过")
