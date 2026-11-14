"""用驱动自身的自动探测流程识别设备（不硬编码端口）。"""
import sys

sys.path.insert(0, ".")

from app.modem import Modem

print("=" * 74)
print("自动探测候选端口")
print("=" * 74)
cands = Modem.candidate_ports()
print("  候选顺序:", cands)

print()
print("=" * 74)
print("驱动自动握手")
print("=" * 74)
m = Modem()
ok = m.open()
print(f"  结果: {'✔ 连上了' if ok else '✘ 失败'}  端口={m.port}  {m.last_error}")
if not ok:
    sys.exit(1)

print()
print("=" * 74)
print("设备状态（read_status）")
print("=" * 74)
st = m.read_status()
for k in ("connected", "port", "signal", "signal_percent", "registered", "operator",
          "sim_ready", "sim_status", "iccid", "number", "sms_mode", "hint", "error"):
    print(f"  {k:<15} {st.get(k)!r}")
print(f"  info            {st.get('info')}")

print()
print("=" * 74)
print("短信读取尝试")
print("=" * 74)
try:
    entries = m.list_sms(4)
    print(f"  list_sms 返回 {len(entries)} 条")
    for e in entries[:5]:
        d = e.decoded
        print(f"  [#{e.index}] {d.sender if d else '?'} -> {(d.text[:60] if d else e.pdu[:40])}")
except Exception as exc:
    print(f"  list_sms 异常: {exc}")

m.close()
