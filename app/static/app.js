/* 短信网关前端：原生 JS，无构建依赖 */
(() => {
  'use strict';

  // ---------------------------------------------------------------- 状态
  const S = {
    token: localStorage.getItem('sms_token') || '',
    view: 'sms',
    peer: null,
    messages: [],
    threads: [],
    status: {},
    settings: {},
    rules: [],
    query: '',
    ws: null,
    wsRetry: 0,
    draftPeer: null,
    devices: [],
    deviceSig: '',
    sendDevice: localStorage.getItem('sms_send_device') || '',
  };
  const SEND_DEVICE_KEY = 'sms_send_device';

  const $ = (id) => document.getElementById(id);
  const el = (tag, cls, html) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (html != null) n.innerHTML = html;
    return n;
  };
  const esc = (s) =>
    String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  // ---------------------------------------------------------------- 工具
  const pad = (n) => String(n).padStart(2, '0');

  function fmtTime(iso, mode) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const y = new Date(now); y.setDate(now.getDate() - 1);
    const isYest = d.toDateString() === y.toDateString();
    const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
    if (mode === 'list') {
      if (sameDay) return hm;
      if (isYest) return '昨天';
      if (d.getFullYear() === now.getFullYear()) return `${d.getMonth() + 1}/${d.getDate()}`;
      return `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
    }
    if (mode === 'day') {
      if (sameDay) return '今天';
      if (isYest) return '昨天';
      return `${d.getMonth() + 1}月${d.getDate()}日`;
    }
    if (sameDay) return hm;
    if (isYest) return `昨天 ${hm}`;
    return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
  }

  function shortPeer(peer) {
    const p = String(peer || '');
    return p.replace(/^\+86/, '') || p || '未知号码';
  }

  function peerInitial(peer) {
    const digits = String(peer || '').replace(/^\+86/, '').replace(/\D/g, '');
    return digits.slice(0, 3) || '#';
  }

  function toast(msg, type = '') {
    const n = el('div', `toast ${type}`, esc(msg));
    $('toast-wrap').appendChild(n);
    setTimeout(() => {
      n.style.transition = 'opacity .25s, transform .25s';
      n.style.opacity = '0';
      n.style.transform = 'translateY(-6px)';
      setTimeout(() => n.remove(), 260);
    }, type === 'err' ? 4200 : 2400);
  }

  // ---------------------------------------------------------------- API
  async function api(path, opts = {}) {
    const headers = Object.assign({ 'Content-Type': 'application/json' }, opts.headers || {});
    if (S.token) headers.Authorization = `Bearer ${S.token}`;
    let res;
    try {
      res = await fetch('/api' + path, Object.assign({}, opts, { headers }));
    } catch (e) {
      throw new Error('网络错误，无法连接服务');
    }
    if (res.status === 401) {
      S.token = '';
      localStorage.removeItem('sms_token');
      showLogin();
      throw new Error('登录已过期');
    }
    const ct = res.headers.get('content-type') || '';
    const data = ct.includes('json') ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = (data && (data.detail || data.error)) || `HTTP ${res.status}`;
      const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail));
      err.payload = data;
      throw err;
    }
    return data;
  }

  // ---------------------------------------------------------------- 登录
  function showLogin() {
    $('login').classList.remove('hidden');
    $('app').classList.add('hidden');
    setTimeout(() => $('login-password').focus(), 60);
  }

  function showApp() {
    $('login').classList.add('hidden');
    $('app').classList.remove('hidden');
    connectWS();
  }

  async function boot() {
    let info;
    try {
      info = await api('/ping');
    } catch (e) {
      toast(e.message, 'err');
      return;
    }
    if (info.title) { $('site-title').textContent = info.title; document.title = info.title; }
    if (!info.auth_required) {
      showApp();
      start();
      return;
    }
    if (S.token) {
      try {
        await api('/auth/check');
        showApp();
        start();
        return;
      } catch (e) { /* 落到登录 */ }
    }
    showLogin();
  }

  $('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const pwd = $('login-password').value;
    try {
      const r = await api('/auth/login', { method: 'POST', body: JSON.stringify({ password: pwd }) });
      S.token = r.token || '';
      if (S.token) localStorage.setItem('sms_token', S.token);
      $('login-password').value = '';
      showApp();
      start();
    } catch (err) {
      toast(err.message, 'err');
    }
  });

  // ---------------------------------------------------------------- WebSocket
  function connectWS() {
    if (S.ws) { try { S.ws.close(); } catch (e) {} }
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const url = `${proto}://${location.host}/api/ws?token=${encodeURIComponent(S.token)}`;
    let ws;
    try { ws = new WebSocket(url); } catch (e) { return; }
    S.ws = ws;
    ws.onopen = () => { S.wsRetry = 0; };
    ws.onmessage = (ev) => {
      let msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      handleEvent(msg);
    };
    ws.onclose = () => {
      S.ws = null;
      S.wsRetry = Math.min(S.wsRetry + 1, 6);
      setTimeout(() => { if (!$('app').classList.contains('hidden')) connectWS(); }, 1000 * S.wsRetry);
    };
    ws.onerror = () => {};
  }

  let threadsTimer = null;
  function handleEvent(ev) {
    if (ev.type === 'status') {
      renderStatus(ev.data);
    } else if (ev.type === 'message') {
      const m = ev.data;
      if (S.view === 'sms' && S.peer && m.peer === S.peer) {
        if (!S.messages.some((x) => x.id === m.id)) {
          S.messages.push(m);
          renderMessages(true);
        }
        if (m.direction === 'in') api(`/threads/${encodeURIComponent(m.peer)}/read`, { method: 'POST' }).catch(() => {});
      }
      scheduleThreads();
    } else if (ev.type === 'message_update') {
      const { id, forwarded, forward_log } = ev.data;
      const m = S.messages.find((x) => x.id === id);
      if (m) { m.forwarded = forwarded; m.forward_log = forward_log; renderMessages(); }
    } else if (ev.type === 'threads_dirty') {
      scheduleThreads();
    }
  }

  function scheduleThreads() {
    if (threadsTimer) return;
    threadsTimer = setTimeout(() => { threadsTimer = null; loadThreads(); }, 350);
  }

  // ---------------------------------------------------------------- 状态
  /** 统一成设备数组：新后端给 devices[]，老后端只有顶层字段时降级成一台。 */
  function normalizeDevices(st) {
    const list = Array.isArray(st && st.devices) ? st.devices.filter(Boolean) : [];
    if (list.length) return list;
    if (st && (st.device_id || st.port)) {
      return [Object.assign({}, st, { device_id: st.device_id || st.port })];
    }
    return [];
  }

  function deviceName(d) {
    // 别名（用户手填）> 本机号码 > 库里的名字 > IMEI 尾号
    // 境外卡（如香港卡）不写 EF_MSISDN，number 为空，只能靠别名区分
    const alias = String(d.alias || '').trim();
    if (alias) return alias;
    const num = String(d.number || '').trim();
    if (num) return shortPeer(num);
    const named = String(d.name || '').trim();
    if (named && !/^usb-/i.test(named)) return named;
    return String(d.device_id || d.key || '').slice(-6) || '设备';
  }

  function deviceLabel(deviceId) {
    if (!deviceId) return '';
    const d = S.devices.find((x) => String(x.device_id) === String(deviceId));
    return d ? deviceName(d) : String(deviceId).slice(-4);
  }

  function deviceStateText(d) {
    if (!d.connected) return '未连接';
    if (d.registered) return '在线';
    if (d.sim_ready) return '未注册';
    return '无 SIM';
  }

  function barsHtml(pct, title) {
    const lit = Math.round((Math.max(0, Math.min(100, Number(pct) || 0)) / 100) * 4);
    let bars = '';
    for (let i = 0; i < 4; i++) bars += `<i${i < lit ? ' class="on"' : ''}></i>`;
    return `<span class="signal-bars" title="${esc(title)}">${bars}</span>`;
  }

  /** 选中的发送设备失效时回退：顶层主设备 → 已连上的 → 第一台。 */
  function ensureSendDevice() {
    const saved = S.sendDevice;
    if (saved && S.devices.some((d) => String(d.device_id) === String(saved))) return saved;
    const primary = S.devices.find((d) => String(d.device_id) === String((S.status || {}).device_id))
      || S.devices.find((d) => d.connected)
      || S.devices[0];
    S.sendDevice = primary ? String(primary.device_id || '') : '';
    if (S.sendDevice) localStorage.setItem(SEND_DEVICE_KEY, S.sendDevice);
    else localStorage.removeItem(SEND_DEVICE_KEY);
    return S.sendDevice;
  }

  function deviceOptionsHtml(selected) {
    return S.devices.map((d) => {
      const id = String(d.device_id || '');
      const suffix = d.connected ? (d.registered ? '' : '（未注册）') : '（离线）';
      return `<option value="${esc(id)}"${id === String(selected) ? ' selected' : ''}>${esc(deviceName(d) + suffix)}</option>`;
    }).join('');
  }

  /** composer 与新建短信弹窗里的设备下拉，跟着设备列表走。 */
  function syncDeviceSelects() {
    const wrap = $('compose-device-wrap');
    const sel = $('compose-device');
    const multi = S.devices.length > 1;
    if (wrap && sel) {
      wrap.classList.toggle('hidden', !multi);
      if (multi && document.activeElement !== sel) {
        sel.innerHTML = deviceOptionsHtml(S.sendDevice);
        sel.value = String(S.sendDevice || '');
      }
    }
    const modalSel = $('new-device');
    if (modalSel && document.activeElement !== modalSel) {
      modalSel.innerHTML = deviceOptionsHtml(S.sendDevice);
      modalSel.value = String(S.sendDevice || '');
    }
  }

  function renderDevices(st) {
    S.devices = normalizeDevices(st);
    const box = $('device-list');
    if (!box) return;
    ensureSendDevice();

    if (!S.devices.length) {
      S.deviceSig = '';
      box.innerHTML = `<div class="device-item">
        <div class="device-top"><i class="status-dot err"></i><span class="device-name">未发现模块</span></div>
      </div>`;
      syncDeviceSelects();
      return;
    }

    // 只在关键字段变化时重建，避免每 45 秒的 status 推送把 DOM 刷掉
    const sig = S.devices.map((d) => [d.device_id, d.connected, d.registered, d.sim_ready,
      d.signal_percent, d.operator, d.number, d.port].join('|')).join(';');
    if (sig !== S.deviceSig || !box.querySelector('.device-item[data-device]')) {
      S.deviceSig = sig;
      box.innerHTML = S.devices.map((d) => {
        const dotCls = d.connected ? (d.registered ? 'on' : '') : 'err';
        const meta = [d.operator || '', d.port || '', deviceStateText(d)].filter(Boolean).join(' · ');
        return `<div class="device-item${String(d.device_id) === String(S.sendDevice) ? ' active' : ''}"
            data-device="${esc(d.device_id)}" title="点击选为发送设备">
          <div class="device-top">
            <i class="status-dot ${dotCls}"></i>
            <span class="device-name">${esc(deviceName(d))}</span>
            ${barsHtml(d.signal_percent, d.signal == null ? '无信号数据' : `CSQ ${d.signal} (${d.signal_percent}%)`)}
          </div>
          <div class="device-meta">${esc(meta)}</div>
        </div>`;
      }).join('');
    }
    // 高亮始终跟随发送设备（切换时不重建也行）
    box.querySelectorAll('.device-item[data-device]').forEach((n) => {
      n.classList.toggle('active', String(n.dataset.device) === String(S.sendDevice));
    });
    syncDeviceSelects();
  }

  function renderStatus(st) {
    S.status = st || {};
    renderDevices(S.status);
    const syncEl = $('st-sync');
    if (syncEl) syncEl.textContent = S.status.last_sync ? fmtTime(S.status.last_sync, 'list') : '—';
    const model = (S.status.info && (S.status.info.model || S.status.info.manufacturer)) || 'Air780EPM';
    $('brand-sub').textContent = S.devices.length > 1 ? `${model} ×${S.devices.length}` : model;
    updateTopSub();
  }

  // 点设备卡直接切换发送设备
  $('device-list').addEventListener('click', (e) => {
    const item = e.target.closest('.device-item[data-device]');
    if (!item) return;
    const id = item.dataset.device;
    if (String(id) === String(S.sendDevice)) return;
    S.sendDevice = id;
    localStorage.setItem(SEND_DEVICE_KEY, id);
    renderDevices(S.status);
  });

  $('compose-device').addEventListener('change', (e) => {
    S.sendDevice = e.target.value;
    if (S.sendDevice) localStorage.setItem(SEND_DEVICE_KEY, S.sendDevice);
    renderDevices(S.status);
  });

  // ---------------------------------------------------------------- 会话列表
  async function loadThreads() {
    try {
      S.threads = await api('/threads');
      renderThreads();
    } catch (e) { /* 静默 */ }
  }

  function renderThreads() {
    const box = $('thread-list');
    box.innerHTML = '';
    const list = S.threads;
    if (!list.length) {
      box.appendChild(el('div', 'empty', '<div class="empty-title">还没有短信</div><div style="font-size:12px">插好模块后点右上角同步</div>'));
      return;
    }
    for (const t of list) {
      const node = el('div', 'thread' + (t.peer === S.peer ? ' active' : ''));
      const initial = peerInitial(t.peer);
      node.innerHTML = `
        <div class="avatar">${esc(initial)}</div>
        <div class="thread-main">
          <div class="thread-top">
            <span class="thread-peer">${esc(shortPeer(t.peer))}</span>
            <span class="thread-time">${esc(fmtTime(t.last_ts, 'list'))}</span>
          </div>
          <div class="thread-bottom">
            <span class="thread-prev">${t.last_direction === 'out' ? '我: ' : ''}${esc(t.last_text || '')}</span>
            ${t.unread ? `<span class="badge">${t.unread}</span>` : ''}
          </div>
        </div>`;
      node.addEventListener('click', () => openThread(t.peer));
      box.appendChild(node);
    }
  }

  // ---------------------------------------------------------------- 消息
  async function openThread(peer) {
    S.peer = peer;
    S.view = 'sms';
    setView('sms', true);
    $('top-title').textContent = shortPeer(peer);
    updateTopSub();
    $('msg-input').placeholder = '回复 ' + shortPeer(peer);
    renderThreads();
    $('sidebar').classList.remove('show');
    await loadMessages();
    markRead(peer);
  }

  async function loadMessages(scrollBottom = true) {
    if (!S.peer) { renderMessages(); return; }
    try {
      const r = await api(`/messages?peer=${encodeURIComponent(S.peer)}&limit=200`);
      S.messages = r.items.reverse();
      renderMessages(scrollBottom);
    } catch (e) {
      toast(e.message, 'err');
    }
  }

  async function markRead(peer) {
    if (S.settings && S.settings.auto_mark_read === '0') return;
    try {
      await api(`/threads/${encodeURIComponent(peer)}/read`, { method: 'POST' });
      const t = S.threads.find((x) => x.peer === peer);
      if (t) { t.unread = 0; renderThreads(); }
    } catch (e) {}
  }

  let renderToken = 0;
  function renderMessages(scrollBottom) {
    const view = $('chat-view');
    if (!S.peer) {
      view.innerHTML = `
        <div class="empty">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 11.5a8.38 8.38 0 01-.9 3.8 8.5 8.5 0 01-7.6 4.7 8.38 8.38 0 01-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 01-.9-3.8 8.5 8.5 0 014.7-7.6 8.38 8.38 0 013.8-.9h.5a8.48 8.48 0 018 8v.5z"/>
          </svg>
          <div class="empty-title">选择一个会话</div>
          <div style="font-size:12px">或点右上角「新短信」直接发送</div>
        </div>`;
      $('composer').classList.add('hidden');
      return;
    }
    $('composer').classList.remove('hidden');

    const inner = el('div', 'chat-inner');
    let lastDay = '';
    for (const m of S.messages) {
      const day = fmtTime(m.ts, 'day');
      if (day !== lastDay) {
        inner.appendChild(el('div', 'day-sep', esc(day)));
        lastDay = day;
      }
      inner.appendChild(messageNode(m));
    }
    if (!S.messages.length) {
      inner.appendChild(el('div', 'empty', '<div class="empty-title">暂无消息</div>'));
    }
    view.innerHTML = '';
    view.appendChild(inner);
    if (scrollBottom !== false) {
      requestAnimationFrame(() => { view.scrollTop = view.scrollHeight; });
    }
  }

  function messageNode(m) {
    const wrap = el('div', 'msg ' + (m.direction === 'out' ? 'out' : 'in'));
    const bubble = el('div', 'bubble');
    const text = el('div');
    text.textContent = m.text || '';
    bubble.appendChild(text);

    const meta = el('div', 'bubble-meta');
    let metaHtml = `<span>${esc(fmtTime(m.ts))}</span>`;
    // 多设备时才标来源，单机不制造噪音
    if (S.devices.length > 1 && m.device_id) {
      metaHtml += `<span class="tag dev">${esc(deviceLabel(m.device_id))}</span>`;
    }
    if (m.direction === 'out') {
      if (m.status === 'failed') metaHtml += `<span class="tag fail">失败</span>`;
      else if (m.forwarded) metaHtml += '';
    } else if (m.forwarded) {
      metaHtml += `<span class="tag">已转发</span>`;
    }
    if (m.concat_total > 1) metaHtml += `<span class="tag">${m.concat_total}条合并</span>`;
    meta.innerHTML = metaHtml;
    bubble.appendChild(meta);

    const tools = el('div', 'bubble-tools');
    const mk = (title, icon, fn, cls) => {
      const b = el('button', cls || '', icon);
      b.title = title;
      b.addEventListener('click', (e) => { e.stopPropagation(); fn(); });
      return b;
    };
    const ICON_COPY = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/></svg>';
    const ICON_RESEND = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M21 12a9 9 0 11-3-6.7L21 8"/><path d="M21 3v5h-5"/></svg>';
    const ICON_FWD = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12h13M13 6l6 6-6 6"/></svg>';
    const ICON_DEL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg>';

    tools.appendChild(mk('复制', ICON_COPY, () => {
      navigator.clipboard.writeText(m.text || '').then(() => toast('已复制'), () => toast('复制失败', 'err'));
    }));
    if (m.direction === 'out') {
      tools.appendChild(mk('重发', ICON_RESEND, async () => {
        try {
          const r = await api(`/messages/${m.id}/resend`, { method: 'POST' });
          toast(r.ok ? '已重新发送' : (r.error || '发送失败'), r.ok ? 'ok' : 'err');
        } catch (e) { toast(e.message, 'err'); }
      }));
    } else {
      tools.appendChild(mk('手动转发', ICON_FWD, async () => {
        try { await api(`/messages/${m.id}/forward`, { method: 'POST' }); toast('已触发转发', 'ok'); loadMessages(false); }
        catch (e) { toast(e.message, 'err'); }
      }));
    }
    tools.appendChild(mk('删除', ICON_DEL, async () => {
      if (!confirm('删除这条短信？')) return;
      try {
        await api(`/messages/${m.id}`, { method: 'DELETE' });
        S.messages = S.messages.filter((x) => x.id !== m.id);
        renderMessages(false);
        scheduleThreads();
      } catch (e) { toast(e.message, 'err'); }
    }, 'del'));

    bubble.appendChild(tools);
    wrap.appendChild(bubble);
    return wrap;
  }

  // ---------------------------------------------------------------- 发送
  /** 当前选中的设备还在一览里才带上，避免设备拔掉后 400。 */
  function sendDeviceParam() {
    if (!S.sendDevice) return undefined;
    return S.devices.some((d) => String(d.device_id) === String(S.sendDevice))
      ? S.sendDevice : undefined;
  }

  async function sendMessage() {
    const input = $('msg-input');
    const text = input.value.trim();
    if (!text) return;
    if (!S.peer) { toast('请先选择或填写接收号码', 'err'); return; }
    const btn = $('btn-send');
    btn.disabled = true;
    try {
      const payload = { peer: S.peer, text };
      const dev = sendDeviceParam();
      if (dev) payload.device = dev;
      const r = await api('/messages', { method: 'POST', body: JSON.stringify(payload) });
      input.value = '';
      autoGrow();
      if (r.message) {
        S.messages.push(r.message);
        renderMessages(true);
      } else {
        await loadMessages();
      }
      scheduleThreads();
    } catch (e) {
      toast(e.message, 'err');
    } finally {
      btn.disabled = false;
      input.focus();
    }
  }

  function autoGrow() {
    const t = $('msg-input');
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight, 150) + 'px';
    const n = t.value.length;
    $('compose-count').textContent = `${n} 字` + (n > 70 ? ` · 约 ${Math.ceil(n / 67)} 条` : '');
  }

  $('btn-send').addEventListener('click', sendMessage);
  $('msg-input').addEventListener('input', autoGrow);
  $('msg-input').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(); }
  });
  $('btn-refresh').addEventListener('click', async () => {
    try {
      const r = await api('/modem/resync', { method: 'POST' });
      toast(r.ok ? `同步完成，新增 ${r.new} 条` : (r.error || '同步失败'), r.ok ? 'ok' : 'err');
      loadThreads();
      if (S.peer) loadMessages(false);
    } catch (e) { toast(e.message, 'err'); }
  });

  // ---------------------------------------------------------------- 新短信
  $('btn-compose').addEventListener('click', () => {
    const devField = S.devices.length > 1
      ? `<div class="field"><label>发送设备</label><select id="new-device"></select></div>`
      : '';
    openModal('发送新短信', `
      <div class="field"><label>接收号码</label><input id="new-peer" placeholder="+8613800138000" value="${esc(S.draftPeer || '')}"></div>
      ${devField}
      <div class="field"><label>短信内容</label><textarea id="new-text" rows="4" placeholder="最多 70 个汉字/条，超出自动分片"></textarea></div>
    `, [
      { text: '取消', cls: 'btn', fn: closeModal },
      { text: '发送', cls: 'btn primary', fn: async () => {
          const peer = $('new-peer').value.trim();
          const text = $('new-text').value.trim();
          if (!peer || !text) { toast('号码和内容不能为空', 'err'); return; }
          const payload = { peer, text };
          const picked = $('new-device') ? $('new-device').value : '';
          if (picked) { payload.device = picked; S.sendDevice = picked; localStorage.setItem(SEND_DEVICE_KEY, picked); }
          try {
            const r = await api('/messages', { method: 'POST', body: JSON.stringify(payload) });
            closeModal();
            toast(r.ok ? '已发送' : (r.error || '发送失败'), r.ok ? 'ok' : 'err');
            S.draftPeer = null;
            await loadThreads();
            openThread(peer);
          } catch (e) { toast(e.message, 'err'); }
        } },
    ]);
    syncDeviceSelects();
  });

  // ---------------------------------------------------------------- 视图切换
  const VIEW_TITLE = { sms: '短信', rules: '转发规则', settings: '设置', logs: '运行日志' };

  function setView(view, keepPeer, syncHash) {
    S.view = view;
    if (syncHash !== false) {
      try { history.replaceState(null, '', '#' + view); } catch (e) {}
    }
    document.querySelectorAll('.nav button').forEach((b) => {
      b.classList.toggle('active', b.dataset.view === view);
    });
    $('search-wrap').classList.toggle('hidden', view !== 'sms');
    $('top-title').textContent = VIEW_TITLE[view] || view;
    $('btn-refresh').classList.toggle('hidden', view !== 'sms');
    $('btn-compose').classList.toggle('hidden', view !== 'sms');

    if (view === 'sms') {
      $('chat-view').classList.remove('hidden');
      $('panel-view').classList.add('hidden');
      if (keepPeer !== true) {
        S.peer = S.peer || null;
      }
      updateTopSub();
      renderMessages(false);
      if (S.peer) $('composer').classList.remove('hidden');
    } else {
      $('chat-view').classList.add('hidden');
      $('composer').classList.add('hidden');
      $('panel-view').classList.remove('hidden');
      updateTopSub();
      if (view === 'rules') renderRulesView();
      if (view === 'settings') renderSettingsView();
      if (view === 'logs') renderLogsView();
    }
  }

  document.querySelectorAll('.nav button').forEach((b) => {
    b.addEventListener('click', () => {
      if (b.dataset.view === 'sms') { setView('sms'); $('sidebar').classList.remove('show'); return; }
      setView(b.dataset.view);
    });
  });

  // 浏览器前进/后退或手动改 hash 时同步视图
  window.addEventListener('hashchange', () => {
    const v = (location.hash || '').replace('#', '');
    if (['sms', 'rules', 'settings', 'logs'].includes(v) && v !== S.view) setView(v, false, false);
  });

  $('search').addEventListener('input', async (e) => {
    const q = e.target.value.trim();
    if (!q) { loadThreads(); return; }
    try {
      const r = await api(`/messages?q=${encodeURIComponent(q)}&limit=80`);
      const box = $('thread-list');
      box.innerHTML = '';
      if (!r.items.length) {
        box.appendChild(el('div', 'empty', '<div class="empty-title">无匹配结果</div>'));
        return;
      }
      for (const m of r.items) {
        const node = el('div', 'thread');
        node.innerHTML = `
          <div class="avatar">${m.direction === 'out' ? '我' : esc(peerInitial(m.peer))}</div>
          <div class="thread-main">
            <div class="thread-top">
              <span class="thread-peer">${esc(shortPeer(m.peer))}</span>
              <span class="thread-time">${esc(fmtTime(m.ts, 'list'))}</span>
            </div>
            <div class="thread-bottom"><span class="thread-prev">${esc(m.text)}</span></div>
          </div>`;
        node.addEventListener('click', () => { e.target.value = ''; loadThreads(); openThread(m.peer); });
        box.appendChild(node);
      }
    } catch (err) { toast(err.message, 'err'); }
  });

  // ---------------------------------------------------------------- 规则视图
  async function renderRulesView() {
    const box = $('panel-view');
    box.innerHTML = '<div class="panel-inner"><div class="card">加载中…</div></div>';
    try {
      S.rules = await api('/rules');
    } catch (e) {
      box.innerHTML = `<div class="panel-inner"><div class="card">${esc(e.message)}</div></div>`;
      return;
    }
    const inner = el('div', 'panel-inner');
    const head = el('div', 'card');
    head.innerHTML = `
      <div class="card-title">转发规则</div>
      <div class="card-desc">短信入库后按规则顺序匹配，匹配即推送到目标。支持 Webhook、邮件、短信转发三种通道。<br>
      占位符：<code>{from}</code> <code>{text}</code> <code>{time}</code> <code>{device}</code></div>`;
    const addBtn = el('button', 'btn primary sm', '＋ 新建规则');
    addBtn.addEventListener('click', () => editRule(null));
    head.appendChild(addBtn);
    inner.appendChild(head);

    if (!S.rules.length) {
      inner.appendChild(el('div', 'card', '<div class="empty"><div class="empty-title">还没有规则</div></div>'));
    }

    for (const r of S.rules) {
      const node = el('div', 'rule' + (r.enabled ? '' : ' off'));
      const targetsDesc = (r.targets || []).map((t) => {
        const label = { webhook: 'Webhook', smtp: '邮件', sms: '短信' }[t.type] || t.type;
        const dest = t.url || t.to || '';
        return `<span class="chip ${t.type === 'webhook' ? 'blue' : 'green'}">${esc(label)}</span> <span class="rule-meta" style="display:inline">${esc(dest)}</span>`;
      }).join('<br>') || '<span class="chip">未配置目标</span>';
      const matchDesc = {
        all: '所有短信',
        contains: `包含「${esc(r.pattern)}」`,
        regex: `正则 /${esc(r.pattern)}/`,
        sender: `号码包含 ${esc(r.pattern)}`,
      }[r.match_type] || r.match_type;

      node.innerHTML = `
        <div class="rule-main">
          <div class="rule-name">${esc(r.name)} ${r.enabled ? '<span class="chip green">启用</span>' : '<span class="chip">停用</span>'}</div>
          <div class="rule-meta">匹配：${matchDesc}</div>
          <div class="rule-meta" style="margin-top:8px">${targetsDesc}</div>
        </div>`;
      const ops = el('div');
      ops.style.cssText = 'display:flex;flex-direction:column;gap:6px';
      const toggle = el('button', 'btn sm', r.enabled ? '停用' : '启用');
      toggle.addEventListener('click', async () => {
        try {
          await api(`/rules/${r.id}`, { method: 'PUT', body: JSON.stringify(Object.assign({}, r, { enabled: r.enabled ? 0 : 1 })) });
          renderRulesView();
        } catch (e) { toast(e.message, 'err'); }
      });
      const edit = el('button', 'btn sm', '编辑');
      edit.addEventListener('click', () => editRule(r));
      const del = el('button', 'btn sm danger', '删除');
      del.addEventListener('click', async () => {
        if (!confirm(`删除规则「${r.name}」？`)) return;
        try { await api(`/rules/${r.id}`, { method: 'DELETE' }); renderRulesView(); }
        catch (e) { toast(e.message, 'err'); }
      });
      ops.append(toggle, edit, del);
      node.appendChild(ops);
      inner.appendChild(node);
    }
    box.innerHTML = '';
    box.appendChild(inner);
  }

  function targetEditor(t, idx) {
    const wrap = el('div', 'target-item');
    const type = t.type || 'webhook';
    wrap.innerHTML = `
      <div class="target-head">
        <select class="t-type">
          <option value="webhook"${type === 'webhook' ? ' selected' : ''}>Webhook</option>
          <option value="smtp"${type === 'smtp' ? ' selected' : ''}>邮件</option>
          <option value="sms"${type === 'sms' ? ' selected' : ''}>短信转发</option>
        </select>
        <label class="switch" style="margin-left:6px">
          <input type="checkbox" class="t-enabled" ${t.enabled === false ? '' : 'checked'}>
          <span class="track"></span>
        </label>
        <button class="btn sm danger t-del">移除</button>
      </div>
      <div class="t-fields"></div>`;
    const fields = wrap.querySelector('.t-fields');

    function paint() {
      const tp = wrap.querySelector('.t-type').value;
      if (tp === 'webhook') {
        fields.innerHTML = `
          <div class="field"><label>URL</label><input class="t-url" value="${esc(t.url || '')}" placeholder="http://192.168.1.10:5678/webhook/sms"></div>
          <div class="field"><label>自定义 Body 模板（可选，留空则发标准 JSON）</label>
            <textarea class="t-template" rows="2" placeholder='{"from":"{from}","text":"{text}"}'>${esc(t.template || '')}</textarea></div>`;
      } else if (tp === 'smtp') {
        fields.innerHTML = `
          <div class="field"><label>收件人（逗号分隔，留空用设置里的默认收件人）</label><input class="t-to" value="${esc(t.to || '')}" placeholder="me@example.com"></div>
          <div class="field"><label>邮件标题模板</label><input class="t-subject" value="${esc(t.subject || '')}" placeholder="[{device}] 来自 {from} 的短信"></div>`;
      } else {
        fields.innerHTML = `
          <div class="field"><label>转发到号码</label><input class="t-to" value="${esc(t.to || '')}" placeholder="+8613800138000"></div>
          <div class="field"><label>内容前缀</label><input class="t-prefix" value="${esc(t.prefix || '')}" placeholder="[SMS]"></div>`;
      }
    }
    paint();
    wrap.querySelector('.t-type').addEventListener('change', paint);

    wrap.read = () => {
      const tp = wrap.querySelector('.t-type').value;
      const out = { type: tp, enabled: wrap.querySelector('.t-enabled').checked };
      const g = (sel) => { const n = wrap.querySelector(sel); return n ? n.value : ''; };
      if (tp === 'webhook') { out.url = g('.t-url').trim(); out.template = g('.t-template'); }
      if (tp === 'smtp') { out.to = g('.t-to').trim(); out.subject = g('.t-subject'); }
      if (tp === 'sms') { out.to = g('.t-to').trim(); out.prefix = g('.t-prefix'); }
      return out;
    };
    return wrap;
  }

  function editRule(rule) {
    const isNew = !rule;
    const r = rule || { name: '', enabled: 1, match_type: 'all', pattern: '', targets: [{ type: 'webhook', url: '', enabled: true }] };
    openModal(isNew ? '新建转发规则' : '编辑规则', `
      <div class="field"><label>规则名称</label><input id="r-name" value="${esc(r.name)}" placeholder="例如：验证码转发"></div>
      <div class="grid2">
        <div class="field"><label>匹配方式</label>
          <select id="r-type">
            <option value="all">所有短信</option>
            <option value="contains">内容包含</option>
            <option value="regex">正则匹配</option>
            <option value="sender">号码包含</option>
          </select>
        </div>
        <div class="field"><label>启用</label>
          <label class="switch" style="padding-top:8px"><input type="checkbox" id="r-enabled" ${r.enabled ? 'checked' : ''}><span class="track"></span></label>
        </div>
      </div>
      <div class="field"><label>匹配内容</label><input id="r-pattern" value="${esc(r.pattern || '')}" placeholder="验证码 / 1069 / ^\\d{6}$"></div>
      <div class="field"><label>转发目标</label><div id="r-targets"></div>
        <button class="btn sm" id="r-add-target">＋ 添加目标</button></div>
    `, [
      { text: '取消', cls: 'btn', fn: closeModal },
      { text: '保存', cls: 'btn primary', fn: async () => {
          const name = $('r-name').value.trim();
          if (!name) { toast('请填写规则名称', 'err'); return; }
          const targets = [...$('r-targets').children].map((n) => n.read());
          const body = {
            name,
            enabled: $('r-enabled').checked ? 1 : 0,
            match_type: $('r-type').value,
            pattern: $('r-pattern').value,
            targets,
          };
          if (!isNew) body.id = r.id;
          try {
            await api(isNew ? '/rules' : `/rules/${r.id}`, { method: isNew ? 'POST' : 'PUT', body: JSON.stringify(body) });
            closeModal();
            toast('已保存', 'ok');
            renderRulesView();
          } catch (e) { toast(e.message, 'err'); }
        } },
    ]);

    $('r-type').value = r.match_type || 'all';
    const tbox = $('r-targets');
    (r.targets || []).forEach((t, i) => {
      const ed = targetEditor(t, i);
      ed.querySelector('.t-del').addEventListener('click', () => ed.remove());
      tbox.appendChild(ed);
    });
    $('r-add-target').addEventListener('click', () => {
      const ed = targetEditor({ type: 'webhook', url: '', enabled: true });
      ed.querySelector('.t-del').addEventListener('click', () => ed.remove());
      tbox.appendChild(ed);
    });
  }

  // ---------------------------------------------------------------- 设置视图
  async function renderSettingsView() {
    const box = $('panel-view');
    try {
      S.settings = await api('/settings');
    } catch (e) {
      box.innerHTML = `<div class="panel-inner"><div class="card">${esc(e.message)}</div></div>`;
      return;
    }
    const s = S.settings;
    const st = S.status;
    const devList = (st.devices && st.devices.length) ? st.devices : [st];
    const devCards = devList.map((d) => {
      const id = String(d.device_id || d.key || '');
      const dotCls = d.connected ? (d.registered ? 'on' : '') : 'err';
      return `<div class="dev-block">
        <div class="dev-head">
          <i class="status-dot ${dotCls}"></i>
          <span class="dev-name">${esc(deviceName(d))}</span>
          <span class="chip ${d.connected ? 'green' : ''}">${esc(deviceStateText(d))}</span>
          ${barsHtml(d.signal_percent, d.signal == null ? '无信号数据' : `CSQ ${d.signal} (${d.signal_percent}%)`)}
        </div>
        <div class="grid2">
          <div class="field"><label>串口</label><div class="mono" style="padding-top:8px">${esc(d.port || '—')}</div></div>
          <div class="field"><label>运营商</label><div class="mono" style="padding-top:8px">${esc(d.operator || '—')}</div></div>
          <div class="field"><label>IMEI</label><div class="mono" style="padding-top:8px">${esc(d.device_id || '—')}</div></div>
          <div class="field"><label>本机号码</label><div class="mono" style="padding-top:8px">${esc(d.number || '卡未写入')}</div></div>
          <div class="field"><label>ICCID</label><div class="mono" style="padding-top:8px">${esc(d.iccid || '—')}</div></div>
          <div class="field"><label>型号 / 固件</label><div class="mono" style="padding-top:8px">${esc((d.model || '—') + ' · ' + (d.firmware || '—'))}</div></div>
        </div>
        ${d.number ? '' : '<div class="dev-hint">这张卡没有把号码写进 SIM（境外卡常见，由运营商决定），网关读不到。给它起个名字便于区分：</div>'}
        <div class="dev-alias-row">
          <input class="dev-alias" data-device="${esc(id)}" maxlength="40"
                 placeholder="设备别名，例如 香港卡 / 联通卡" value="${esc(d.alias || '')}">
          <button class="btn sm dev-alias-save" data-device="${esc(id)}">保存</button>
        </div>
        <div style="display:flex;gap:8px;margin-top:6px">
          <button class="btn sm dev-reconnect" data-device="${esc(id)}">重连</button>
          <button class="btn sm dev-resync" data-device="${esc(id)}">立即同步</button>
        </div>
      </div>`;
    }).join('<div class="dev-sep"></div>');

    box.innerHTML = `
      <div class="panel-inner">
        <div class="card">
          <div class="card-title">设备 <span class="chip">${devList.length} 台</span>${st.mock ? ' <span class="chip blue">模拟</span>' : ''}</div>
          <div class="card-desc">每台模块独立轮询、互不阻塞。点左侧状态栏里的设备卡可切换发信设备。</div>
          ${devCards}
        </div>

        <div class="card">
          <div class="card-title">短信与转发行为</div>
          <div class="field">
            <label class="switch"><input type="checkbox" id="set-auto-forward" ${s.auto_forward === '1' ? 'checked' : ''}><span class="track"></span>
            <span>收到短信自动按规则转发</span></label>
          </div>
          <div class="field">
            <label class="switch"><input type="checkbox" id="set-auto-mark-read" ${s.auto_mark_read === '1' ? 'checked' : ''}><span class="track"></span>
            <span>打开会话时自动标记已读</span></label>
          </div>
          <div class="grid2">
            <div class="field"><label>发送签名（追加到每条发出短信末尾）</label>
              <input id="set-signature" value="${esc(s.signature || '')}" placeholder="留空不追加"></div>
            <div class="field"><label>转发失败重试次数</label>
              <input id="set-retry" type="number" min="1" max="5" value="${esc(s.retry_limit || '3')}"></div>
          </div>
        </div>

        <div class="card">
          <div class="card-title">邮件转发 (SMTP)</div>
          <div class="card-desc">用于「邮件」类型的转发目标。密码保存在本地数据库，仅局域网可读。</div>
          <div class="grid2">
            <div class="field"><label>SMTP 服务器</label><input id="smtp-host" value="${esc(s.smtp_host || '')}" placeholder="smtp.qq.com"></div>
            <div class="field"><label>端口</label><input id="smtp-port" value="${esc(s.smtp_port || '465')}" placeholder="465"></div>
            <div class="field"><label>账号</label><input id="smtp-user" value="${esc(s.smtp_user || '')}" placeholder="you@qq.com"></div>
            <div class="field"><label>密码 / 授权码</label><input id="smtp-pass" type="password" value="" placeholder="${s.smtp_pass_set ? '已保存，留空不修改' : '未设置'}"></div>
            <div class="field"><label>发件人</label><input id="smtp-from" value="${esc(s.smtp_from || '')}" placeholder="默认同账号"></div>
            <div class="field"><label>默认收件人</label><input id="smtp-to" value="${esc(s.smtp_to || '')}" placeholder="me@example.com"></div>
          </div>
          <div class="field">
            <label class="switch"><input type="checkbox" id="smtp-ssl" ${s.smtp_ssl === '1' ? 'checked' : ''}><span class="track"></span>
            <span>使用 SSL（465 端口勾选，587 端口取消）</span></label>
          </div>
        </div>

        <div class="card">
          <div class="card-title">AT 调试</div>
          <div class="card-desc">直接向模块发 AT 指令，排障用。例：<code>AT+CSQ</code> <code>AT+COPS?</code> <code>AT+CPIN?</code></div>
          <div style="display:flex;gap:8px">
            <input id="at-cmd" placeholder="AT+CSQ" class="mono">
            <button class="btn primary sm" id="at-send" style="flex:0 0 auto">执行</button>
          </div>
          <pre id="at-out" class="mono" style="margin-top:10px;background:var(--panel-2);border:1px solid var(--border-soft);border-radius:10px;padding:10px;font-size:11.5px;max-height:220px;overflow:auto;white-space:pre-wrap;display:none"></pre>
        </div>

        <div style="display:flex;justify-content:flex-end;gap:9px;padding-bottom:20px">
          <button class="btn primary" id="btn-save-settings">保存设置</button>
        </div>
      </div>`;

    box.querySelectorAll('.dev-reconnect').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const dev = btn.dataset.device;
        toast('正在重连…');
        try {
          const r = await api(`/modem/reconnect?device=${encodeURIComponent(dev)}`, { method: 'POST' });
          toast(r.ok ? `已连接 ${r.port}` : (r.error || '重连失败'), r.ok ? 'ok' : 'err');
          await refreshStatus();
          renderSettingsView();
        } catch (e) { toast(e.message, 'err'); }
      });
    });
    box.querySelectorAll('.dev-resync').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const dev = btn.dataset.device;
        try {
          const r = await api(`/modem/resync?device=${encodeURIComponent(dev)}`, { method: 'POST' });
          toast(r.ok ? `同步完成，新增 ${r.new} 条` : (r.error || '同步失败'), r.ok ? 'ok' : 'err');
          loadThreads();
        } catch (e) { toast(e.message, 'err'); }
      });
    });
    box.querySelectorAll('.dev-alias-save').forEach((btn) => {
      btn.addEventListener('click', async () => {
        const dev = btn.dataset.device;
        const input = box.querySelector(`.dev-alias[data-device="${dev}"]`)
          || box.querySelector('.dev-alias');
        const alias = (input && input.value || '').trim();
        btn.disabled = true;
        try {
          await api(`/devices/${encodeURIComponent(dev)}/alias`, {
            method: 'POST', body: JSON.stringify({ alias }),
          });
          toast(alias ? `已命名为「${alias}」` : '已清除别名', 'ok');
          await refreshStatus();
          renderSettingsView();
        } catch (e) {
          toast(e.message, 'err');
          btn.disabled = false;
        }
      });
    });
    $('at-send').addEventListener('click', async () => {
      const cmd = $('at-cmd').value.trim();
      if (!cmd) return;
      const out = $('at-out');
      out.style.display = 'block';
      out.textContent = '执行中…';
      try {
        const r = await api('/modem/at', { method: 'POST', body: JSON.stringify({ cmd }) });
        out.textContent = (r.lines && r.lines.join('\n')) || (r.error || '(无输出)');
      } catch (e) { out.textContent = e.message; }
    });
    $('at-cmd').addEventListener('keydown', (e) => { if (e.key === 'Enter') $('at-send').click(); });

    $('btn-save-settings').addEventListener('click', async () => {
      const payload = {
        auto_forward: $('set-auto-forward').checked ? '1' : '0',
        auto_mark_read: $('set-auto-mark-read').checked ? '1' : '0',
        signature: $('set-signature').value,
        retry_limit: $('set-retry').value,
        smtp_host: $('smtp-host').value.trim(),
        smtp_port: $('smtp-port').value.trim(),
        smtp_user: $('smtp-user').value.trim(),
        smtp_pass: $('smtp-pass').value,
        smtp_from: $('smtp-from').value.trim(),
        smtp_to: $('smtp-to').value.trim(),
        smtp_ssl: $('smtp-ssl').checked ? '1' : '0',
      };
      try {
        await api('/settings', { method: 'PUT', body: JSON.stringify(payload) });
        toast('设置已保存', 'ok');
        renderSettingsView();
      } catch (e) { toast(e.message, 'err'); }
    });
  }

  // ---------------------------------------------------------------- 日志视图
  async function renderLogsView() {
    const box = $('panel-view');
    box.innerHTML = '<div class="panel-inner"><div class="card">加载中…</div></div>';
    let logs = [];
    try { logs = await api('/logs?limit=200'); }
    catch (e) {
      box.innerHTML = `<div class="panel-inner"><div class="card">${esc(e.message)}</div></div>`;
      return;
    }
    const inner = el('div', 'panel-inner');
    const card = el('div', 'card');
    card.innerHTML = `<div class="card-title">运行日志 <span class="chip">最近 ${logs.length} 条</span></div>`;
    if (!logs.length) {
      card.innerHTML += '<div class="empty"><div class="empty-title">暂无日志</div></div>';
    }
    for (const l of logs) {
      const line = el('div', 'log-line');
      line.innerHTML = `<span class="log-time">${esc(fmtTime(l.ts))}</span>
        <span class="log-lv ${esc(l.level)}">${esc(l.level)}</span>
        <span class="log-msg">[${esc(l.source)}] ${esc(l.message)}</span>`;
      card.appendChild(line);
    }
    inner.appendChild(card);
    box.innerHTML = '';
    box.appendChild(inner);
  }

  // ---------------------------------------------------------------- 弹窗
  let modalButtons = [];
  function openModal(title, bodyHtml, buttons) {
    $('modal-title').textContent = title;
    $('modal-body').innerHTML = bodyHtml;
    const foot = $('modal-foot');
    foot.innerHTML = '';
    modalButtons = buttons || [];
    modalButtons.forEach((b) => {
      const btn = el('button', b.cls || 'btn', esc(b.text));
      btn.addEventListener('click', b.fn);
      foot.appendChild(btn);
    });
    $('modal-mask').classList.add('show');
  }
  function closeModal() { $('modal-mask').classList.remove('show'); }
  $('modal-close').addEventListener('click', closeModal);
  $('modal-mask').addEventListener('click', (e) => { if (e.target === $('modal-mask')) closeModal(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && $('modal-mask').classList.contains('show')) closeModal();
  });

  // ---------------------------------------------------------------- 启动
  // 顶部副标题：设备异常提示 > 当前会话号码 > 网络概况
  function updateTopSub() {
    const st = S.status || {};
    const el = $('top-sub');
    if (!el) return;
    if (S.view === 'settings' || S.view === 'logs') { el.textContent = ''; return; }
    if (st.hint) { el.textContent = st.hint; return; }
    if (S.view === 'sms' && S.peer) { el.textContent = S.peer; return; }
    if (S.devices.length > 1) {
      const online = S.devices.filter((d) => d.connected).length;
      const main = S.devices.find((d) => String(d.device_id) === String(st.device_id)) || S.devices[0];
      const pct = main.signal_percent == null ? '—' : main.signal_percent + '%';
      el.textContent = `${online}/${S.devices.length} 台在线 · ${main.operator || '—'} 信号 ${pct}${st.mock ? ' · MOCK' : ''}`;
      return;
    }
    el.textContent = st.connected
      ? `${st.operator || '—'} · 信号 ${st.signal_percent == null ? '—' : st.signal_percent + '%'}${st.mock ? ' · MOCK' : ''}`
      : (st.error || '模块未连接');
  }

  async function refreshStatus() {
    try {
      renderStatus(await api('/status'));
    } catch (e) {
      // 别静默：拉不到状态时设备栏会一直停在「等待模块…」，看着像模块没插好
      const box = $('device-list');
      if (box && !box.querySelector('.device-item[data-device]')) {
        box.innerHTML = `<div class="device-item">
          <div class="device-top"><i class="status-dot err"></i><span class="device-name">状态获取失败</span></div>
          <div class="device-meta">${esc(e.message || '未知错误')}，30 秒后自动重试</div>
        </div>`;
      }
    }
  }

  async function start() {
    await Promise.all([refreshStatus(), loadThreads()]);
    try { S.settings = await api('/settings'); } catch (e) { S.settings = {}; }
    const hashView = (location.hash || '').replace('#', '');
    const initView = ['sms', 'rules', 'settings', 'logs'].includes(hashView) ? hashView : 'sms';
    setView(initView);
    // 自动选第一个会话
    if (initView === 'sms' && !S.peer && S.threads.length) openThread(S.threads[0].peer);
  }

  setInterval(() => {
    if ($('app').classList.contains('hidden')) return;
    if (!S.ws || S.ws.readyState !== 1) refreshStatus();
  }, 30000);

  boot();
})();
