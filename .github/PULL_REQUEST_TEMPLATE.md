## 改了什么

<!-- 一句话概括，说「为什么」比说「改了哪行」更有用 -->

## 类型

- [ ] Bug 修复
- [ ] 新功能
- [ ] 文档 / 工程化
- [ ] 重构（行为不变）

## 关联

<!-- 关联 issue，没有就写「无」 -->

## 验证

- [ ] 8 个测试套件全绿（`python -m tests.test_*`）
- [ ] 改动涉及转发规则 → 跑过 `test_forwarder`
- [ ] 改动涉及 PDU / 桥接协议 → 跑过 `test_pdu` / `test_luatos`
- [ ] 改动涉及多设备分组 → 跑过 `test_device` / `test_multi`
- [ ] 改动涉及 `firmware/main.lua` → 说明固件版本变化，并确认用户需要重刷
- [ ] 没有把 `.env`、`data/*.db`、`dist/`、`.venv/` 加进提交
- [ ] 文档里没有真实手机号 / ICCID / IMSI / 密钥

## 备注

<!-- 破坏性变更、需要手动操作（如 --force-recreate）、已知限制 -->
