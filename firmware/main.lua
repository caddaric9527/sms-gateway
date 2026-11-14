--- 短信网关桥接脚本（Air780EPM / LuatOS）
--
-- 作用：把模块的短信能力通过 USB 虚拟串口暴露给上位机（飞牛 NAS 上的 sms-gateway）。
--   Air780EPM 不支持标准 AT 短信指令，所以由本脚本在模块侧调用 sms 库，
--   上位机只负责解析与转发。
--
-- 协议：UTF-8 文本行，\r\n 结尾，负载为单行 JSON。
--   上位机 -> 模块：PING | STATUS
--                   SEND   {"id":1,"num":"+8613...","txt":"..."}
--                   SETPIN {"pin":"1234"}            验证 SIM PIN
--                   SETPIN {"pin":"1234","disable":true}  永久关闭 PIN
--   模块 -> 上位机：+BOOT | +PONG | +STATUS | +SMS | +SENT | +READY | +PIN | +HB | +ERR
--
-- 依赖：LuatOS 内核需包含 sms / mobile / uart / json 库

PROJECT = "sms_bridge"
VERSION = "1.1.1"

local UART_ID = uart.VUART_0          -- USB 虚拟串口；nil 表示本固件不支持
local HEARTBEAT = 30000               -- 心跳间隔
local MAX_BUFFER = 8192               -- 接收行缓冲上限

local rx_buffer = ""
-- 保险开关：如果日志显示卡在 uart.setup 上（表现为 "等待 USB 就绪…" 之后直接 Lua VM exit），
-- 把这里改成 false 再刷一次，跳过 setup 直接收发（VUART 是 USB CDC，波特率本来无意义）。
local DO_SETUP = true

local function logi(...) log.info("bridge", ...) end
local function loge(...) log.error("bridge", ...) end

---------------------------------------------------------------- 保活
-- 放在最前面：即使业务任务异常退出，Lua VM 也不会因为没有任务而重启。
-- 之前「Lua VM exit!! reboot in 15000ms」就是业务任务死掉后无人接管导致的。
sys.taskInit(function()
    while true do
        sys.wait(60000)
    end
end)

---------------------------------------------------------------- 发送
local function tx(line)
    if not UART_ID then return false end
    local ok, err = pcall(uart.write, UART_ID, line .. "\r\n")
    if not ok then loge("uart.write 失败", tostring(err)) end
    return ok
end

local function reply(tag, payload)
    local ok, encoded = pcall(json.encode, payload)
    tx(tag .. " " .. (ok and encoded or "{}"))
end

---------------------------------------------------------------- 状态
local function status_table()
    local st = {}
    pcall(function()
        st.csq = mobile.csq() or 0
        st.reg = mobile.status() or 0
        st.iccid = mobile.iccid() or ""
        st.imsi = mobile.imsi() or ""
        st.num = mobile.number() or ""
        st.imei = mobile.imei() or ""
        -- simPin() 不带参数 = 查询 SIM 是否已就绪（含 PIN 已验证）
        if mobile.simPin then st.pin = mobile.simPin() and true or false end
    end)
    st.sim = st.iccid ~= nil and st.iccid ~= ""
    st.fw = rtos.version() or ""
    st.bsp = rtos.bsp() or ""
    st.ts = os.time()
    return st
end

---------------------------------------------------------------- SIM PIN
-- 上位机通过 SETPIN 下发，PIN 不落在脚本里（脚本要烧到模块，避免明文泄露）。
-- 支持 {"pin":"1234"} 验证，和 {"pin":"1234","disable":true} 永久关闭 PIN。
-- 注意：验证失败是累计计数的，连续错 3 次卡会被锁，需要用 PUK 解，所以上位机必须限次。
local function do_setpin(payload)
    local pin = payload.pin
    if type(pin) ~= "string" or #pin < 4 or #pin > 8 then
        reply("+PIN", { ok = false, err = "PIN 应为 4~8 位" })
        return
    end
    if not mobile or not mobile.simPin then
        reply("+PIN", { ok = false, err = "固件不支持 mobile.simPin" })
        return
    end
    -- 常量缺失时用底层枚举值兜底：PIN_VERIFY=0, PIN_DISABLE=3
    local op = payload.disable and (mobile.PIN_DISABLE or 3) or (mobile.PIN_VERIFY or 0)
    local called, succ = pcall(mobile.simPin, 0, op, pin, "")
    if not called then
        reply("+PIN", { ok = false, err = tostring(succ) })
        return
    end
    local iccid = ""
    pcall(function() iccid = mobile.iccid() or "" end)
    reply("+PIN", {
        ok = succ and true or false,
        disable = payload.disable and true or false,
        iccid = iccid,
        err = succ and "" or "验证未通过（PIN 错误或已锁卡）",
    })
end

---------------------------------------------------------------- 发送短信
-- 会阻塞最长 60 秒等网络回执，必须跑在独立 task 里
local function do_send(payload)
    local num, txt = payload.num, payload.txt
    if type(num) ~= "string" or type(txt) ~= "string" or num == "" or txt == "" then
        reply("+SENT", { id = payload.id, ok = false, err = "号码或内容为空" })
        return
    end
    if not sms then
        reply("+SENT", { id = payload.id, ok = false, err = "固件缺少 sms 库" })
        return
    end

    local long = #txt > 140
    local ok = false
    local err = ""
    if long then
        local task = sms.sendLong(num, txt)
        if task then
            ok = task.wait() and true or false
        end
        err = ok and "" or "长短信提交失败"
    else
        ok = sms.send(num, txt) and true or false
        err = ok and "" or "提交失败"
    end

    if ok then
        local arrived, success = sys.waitUntil("SMS_SENT", long and 60000 or 15000)
        if arrived then
            ok = success and true or false
            err = ok and "" or "网络拒绝"
        end
    end

    reply("+SENT", { id = payload.id, ok = ok, err = err, long = long })
end

---------------------------------------------------------------- 指令分发
local function handle_line(line)
    if line == "PING" then
        tx("+PONG")
    elseif line == "STATUS" then
        reply("+STATUS", status_table())
    elseif string.sub(line, 1, 4) == "SEND" then
        local body = string.sub(line, 6)
        local ok, payload = pcall(json.decode, body)
        if ok and type(payload) == "table" then
            sys.taskInit(function() do_send(payload) end)
        else
            reply("+SENT", { ok = false, err = "指令 JSON 解析失败" })
        end
    elseif string.sub(line, 1, 6) == "SETPIN" then
        local body = string.sub(line, 8)
        local ok, payload = pcall(json.decode, body)
        if ok and type(payload) == "table" then
            do_setpin(payload)
        else
            reply("+PIN", { ok = false, err = "指令 JSON 解析失败" })
        end
    else
        tx("+ERR unknown:" .. string.sub(line, 1, 24))
    end
end

---------------------------------------------------------------- 串口接收
local function on_uart(id, len)
    local data = uart.read(id, len)
    if not data or data == "" then return end
    rx_buffer = rx_buffer .. data
    if #rx_buffer > MAX_BUFFER then
        rx_buffer = ""
        return
    end
    while true do
        local pos = string.find(rx_buffer, "\n", 1, true)
        if not pos then break end
        local line = string.sub(rx_buffer, 1, pos - 1)
        rx_buffer = string.sub(rx_buffer, pos + 1)
        line = string.gsub(line, "\r", "")
        if #line > 0 then
            sys.publish("GW_LINE", line)
        end
    end
end

---------------------------------------------------------------- 主流程（逐步打点，便于定位崩溃位置）
local function bridge_body()
    logi("启动", VERSION, "VUART_0=", tostring(UART_ID))
    if not UART_ID then
        loge("本固件没有 uart.VUART_0，无法通过 USB 与上位机通信")
        return
    end

    -- 1.0.1/1.0.3 的 "Lua VM exit" 真凶是缺 sys.run()，与 setup 时机无关。
    -- 这里仍保留 3 秒等待，让 USB CDC 枚举完成后再注册回调，避免漏掉首帧。
    logi("等待 USB 就绪…")
    sys.wait(3000)

    if DO_SETUP then
        local ok, err = pcall(uart.setup, UART_ID, 115200, 8, 1)
        logi("uart.setup", ok and "ok" or ("失败 " .. tostring(err)))
    else
        logi("uart.setup 已跳过（DO_SETUP=false）")
    end
    sys.wait(300)

    local ok2, err2 = pcall(uart.on, UART_ID, "receive", on_uart)
    logi("uart.on", ok2 and "ok" or ("失败 " .. tostring(err2)))

    -- 启动横幅：上位机一连上就能看到，确认脚本真的在跑
    tx("+BOOT {\"proj\":\"" .. PROJECT .. "\",\"ver\":\"" .. VERSION .. "\"}")
    logi("虚拟串口已就绪，等待上位机 PING")

    logi("sms 库", sms and "存在" or "缺失")
    if sms then
        pcall(sms.autoLong, true)
    else
        tx("+ERR firmware_missing_sms_lib")
    end

    sys.subscribe("SMS_INC", function(num, txt)
        reply("+SMS", { num = num or "", txt = txt or "", ts = os.time() })
    end)

    -- 网络就绪后补发一次完整状态
    sys.taskInit(function()
        sys.waitUntil("IP_READY", 30000)
        sys.wait(5000)
        reply("+READY", status_table())
    end)

    while true do
        local arrived, line = sys.waitUntil("GW_LINE", HEARTBEAT)
        if not arrived then
            reply("+HB", status_table())
        else
            local hok, herr = pcall(handle_line, line)
            if not hok then loge("处理指令出错", tostring(herr)) end
        end
    end
end

sys.taskInit(function()
    local ok, err = xpcall(bridge_body, function(e)
        return tostring(e) .. "\n" .. debug.traceback()
    end)
    if not ok then
        -- 业务任务不允许死掉：出错后打日志再拉起来
        loge("任务异常退出，5 秒后重启", err)
        sys.wait(5000)
        rtos.reboot()
    end
end)

-- 用户代码已结束 ---------------------------------------------
-- LuatOS 的调度主循环，必须有；没有它 main.lua 返回后 1 毫秒就 "Lua VM exit"。
-- 此句之后不要写任何代码（系统重启前不会返回）。
sys.run()
