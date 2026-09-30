const CSRF = document.querySelector('meta[name="csrf-token"]').content;

async function api(url, opts = {}) {
  const o = Object.assign({ headers: {} }, opts);
  o.headers['X-CSRF-Token'] = CSRF;
  if (o.body && typeof o.body !== 'string') {
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(o.body);
  }
  const r = await fetch(url, o);
  if (r.status === 401) { location.href = '/login'; throw new Error('Chưa đăng nhập'); }
  let data = {};
  try { data = await r.json(); } catch (e) { }
  if (!r.ok) throw new Error(data.error || ('Lỗi ' + r.status));
  return data;
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())} ${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()}`;
}

function fmtAgo(ts) {
  if (!ts) return '—';
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return s + ' giây trước';
  if (s < 3600) return Math.floor(s / 60) + ' phút trước';
  if (s < 86400) return Math.floor(s / 3600) + ' giờ trước';
  return Math.floor(s / 86400) + ' ngày trước';
}

function fmtPct(v) { return v === null || v === undefined ? '—' : v.toFixed(2).replace(/\.?0+$/, '') + '%'; }

function pctCell(v) {
  if (v === null || v === undefined) return '<span class="muted">—</span>';
  const color = v >= 99 ? 'var(--ok)' : v >= 90 ? 'var(--warn)' : 'var(--down)';
  return `<div class="pct-cell"><div class="bar"><span style="width:${v}%;background:${color}"></span></div><span>${fmtPct(v)}</span></div>`;
}

const STATUS_TEXT = { up: 'Hoạt động', down: 'Mất kết nối', warning: 'Đang lỗi', unknown: 'Chưa kiểm tra' };
function statusBadge(status, enabled = 1) {
  if (!enabled) return '<span class="badge b-off">Tạm dừng</span>';
  return `<span class="badge b-${status}">${STATUS_TEXT[status] || status}</span>`;
}

function toast(title, msg, type = '', ms = 8000) {
  const el = document.createElement('div');
  el.className = 'toast ' + type;
  el.innerHTML = `<b>${esc(title)}</b>${esc(msg)}`;
  el.onclick = () => el.remove();
  document.getElementById('toasts').appendChild(el);
  if (ms) setTimeout(() => el.remove(), ms);
}

function openModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }

class PushError extends Error {
  constructor(message, hint) { super(message); this.hint = hint; }
}

const Push = {
  keyStore: 'pd_push_key',
  publicKey: null,

  supported: () => 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window,
  ua: () => navigator.userAgent,

  async state() {
    if (!window.isSecureContext) return 'insecure';
    if (!this.supported()) return 'unsupported';
    if (Notification.permission === 'denied') return 'denied';
    if (Notification.permission === 'default') return 'default';
    const reg = await navigator.serviceWorker.getRegistration();
    return (reg && await reg.pushManager.getSubscription()) ? 'on' : 'off';
  },

  async key() {
    if (!this.publicKey) this.publicKey = (await api('/api/push/public-key')).publicKey;
    return this.publicKey;
  },

  bytes(b64) {
    const raw = atob((b64 + '='.repeat((4 - b64.length % 4) % 4)).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from(raw, c => c.charCodeAt(0));
  },

  b64(buf) {
    return btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },

  timeout(p, ms, err) {
    return new Promise((resolve, reject) => {
      const t = setTimeout(() => reject(err()), ms);
      p.then(v => { clearTimeout(t); resolve(v); }, e => { clearTimeout(t); reject(e); });
    });
  },

  serviceHint() {
    const u = this.ua();
    if (navigator.brave) return 'Brave tắt dịch vụ thông báo mặc định: mở brave://settings/privacy → bật "Use Google services for push messaging" → tải lại trang và bật lại.';
    if (/coc_coc_browser/i.test(u)) return 'Cốc Cốc không hỗ trợ ổn định thông báo đẩy. Hãy dùng Microsoft Edge hoặc Google Chrome.';
    if (/Edg\//.test(u)) return 'Edge cần kết nối tới dịch vụ thông báo của Microsoft (*.notify.windows.com, cổng 443). Nếu mạng cơ quan chặn: nhờ bộ phận mạng mở, hoặc thử mạng khác.';
    if (/Firefox\//.test(u)) return 'Firefox cần kết nối tới push.services.mozilla.com (cổng 443). Nếu mạng cơ quan chặn: thử Microsoft Edge.';
    return 'Chrome cần kết nối tới máy chủ thông báo của Google, mạng cơ quan thường chặn. Cách xử lý: (1) dùng Microsoft Edge; ' +
      '(2) hoặc nhờ bộ phận mạng mở: mtalk.google.com cổng 5228 và 443, android.clients.google.com, fcm.googleapis.com, fcmregistrations.googleapis.com (cổng 443).';
  },

  certHint() {
    return `Máy tính này chưa tin cậy chứng chỉ của PingDiagnose. Tải và chạy file <a href="/install-ca.bat">PingDiagnose-cai-chung-chi.bat</a> ` +
      `(hoặc tải <a href="/ca.crt">ca.crt</a> → mở → Install Certificate → Trusted Root Certification Authorities), ` +
      `đóng hết trình duyệt rồi mở lại, sau đó bấm Bật thông báo.`;
  },

  async registration() {
    if (!(await navigator.serviceWorker.getRegistration())) {
      try {
        await navigator.serviceWorker.register('/sw.js');
      } catch (e) {
        if (/SSL|certificate|security/i.test(e.message || '') || e.name === 'SecurityError') {
          throw new PushError('Trình duyệt chặn vì chứng chỉ HTTPS chưa được tin cậy.', this.certHint());
        }
        throw e;
      }
    }
    return this.timeout(navigator.serviceWorker.ready, 10000,
      () => new PushError('Service worker chưa khởi động được', 'Tải lại trang bằng Ctrl+F5 rồi bấm Bật thông báo lần nữa. Không dùng chế độ ẩn danh.'));
  },

  async browserSubscribe(reg, key) {
    try {
      return await this.timeout(reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: this.bytes(key) }), 20000,
        () => new PushError('Trình duyệt không kết nối được dịch vụ thông báo của hãng (quá 20 giây)', this.serviceHint()));
    } catch (e) {
      if (e instanceof PushError) throw e;
      const msg = e.message || String(e);
      if (/incognito|private|permission denied/i.test(msg)) throw new PushError('Trình duyệt từ chối đăng ký nhận thông báo', 'Chế độ ẩn danh (Incognito/InPrivate) không nhận được thông báo: mở trang bằng cửa sổ thường. Nếu đang ở cửa sổ thường: bấm ổ khoá cạnh thanh địa chỉ → Thông báo → Cho phép.');
      if (/push service|Registration failed|AbortError/i.test(msg) || e.name === 'AbortError') {
        throw new PushError('Trình duyệt không đăng ký được dịch vụ thông báo: ' + msg, this.serviceHint());
      }
      throw e;
    }
  },

  async subscribe(fresh = false) {
    const reg = await this.registration();
    const key = await this.key();
    let sub = await reg.pushManager.getSubscription();
    if (sub) {
      const raw = sub.options && sub.options.applicationServerKey;
      const cur = raw ? this.b64(raw) : null;
      let saved = null;
      try { saved = localStorage.getItem(this.keyStore); } catch (e) { }
      if (fresh || (cur && cur !== key) || (saved && saved !== key)) {
        await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } }).catch(() => undefined);
        await sub.unsubscribe();
        sub = null;
      }
    }
    if (!sub) sub = await this.browserSubscribe(reg, key);
    try { localStorage.setItem(this.keyStore, key); } catch (e) { }
    await api('/api/push/subscribe', { method: 'POST', body: { subscription: sub.toJSON(), userAgent: this.ua().slice(0, 300) } });
  },

  async disable() {
    const reg = await navigator.serviceWorker.getRegistration();
    const sub = reg && await reg.pushManager.getSubscription();
    if (!sub) return;
    await api('/api/push/unsubscribe', { method: 'POST', body: { endpoint: sub.endpoint } }).catch(() => undefined);
    await sub.unsubscribe();
    try { localStorage.removeItem(this.keyStore); } catch (e) { }
  },

  async sync() {
    try {
      if (!window.isSecureContext || !this.supported() || Notification.permission !== 'granted') return;
      await this.subscribe();
    } catch (e) {
      console.warn('[push] đồng bộ thất bại', e);
    }
  },

  async test() {
    const r = await api('/api/push/test', { method: 'POST' });
    const err = (r.results || []).find(x => !x.ok);
    if (r.sent) toast('Đã gửi', `Đã gửi tới ${r.sent}/${r.devices} thiết bị, kiểm tra thông báo.`, 'up');
    else if (err) toast('Máy chủ không gửi được', err.error, 'err', 20000);
    else toast('Chưa gửi được', 'Thiết bị chưa đăng ký. Tắt rồi bật lại thông báo.', 'err');
  },

  showError(e) {
    const box = document.getElementById('pushError');
    const html = `${esc(e.message || String(e))}` +
      (e.hint ? `<div style="margin-top:6px"><b>Cách xử lý:</b> ${e instanceof PushError && e.hint.includes('<a ') ? e.hint : esc(e.hint)}</div>` : '');
    if (box) { box.innerHTML = html; box.classList.remove('hidden'); }
    else toast('Chưa bật được thông báo', (e.message || '') + (e.hint ? ' ' + e.hint.replace(/<[^>]+>/g, '') : ''), 'err', 20000);
  },

  async turnOn(after) {
    try {
      const perm = await Notification.requestPermission();
      if (perm === 'granted') {
        toast('Đã bật thông báo', 'Cảnh báo sẽ hiện ở góc màn hình khi trang PingDiagnose đang mở (kể cả thu nhỏ).', 'up');
        try {
          await this.subscribe(true);
          toast('Đã bật thông báo qua Internet', 'Thiết bị nhận cảnh báo kể cả khi đã đóng trang.', 'up');
        } catch (e) {
          e.message = 'Chưa bật được thông báo qua Internet (vẫn nhận trong mạng nội bộ khi mở trang). ' + (e.message || '');
          this.showError(e);
        }
      } else if (perm === 'denied') {
        toast('Thông báo bị chặn', 'Bấm biểu tượng ổ khoá cạnh thanh địa chỉ → Thông báo → Cho phép.', 'err', 15000);
      }
    } catch (e) {
      this.showError(e);
    }
    renderPushSide();
    if (after) after();
  },
};

const PUSH_TEXT = {
  insecure: '⚠ Trang HTTP: chỉ cảnh báo trong trang',
  unsupported: '⚠ Trình duyệt không hỗ trợ thông báo',
  denied: '🔕 Thông báo đang bị chặn',
  default: '🔔 Chưa bật thông báo',
  off: '🔔 Thông báo khi mở trang (mạng nội bộ)',
  on: '🔔 Thông báo: mạng nội bộ + Internet',
};

async function renderPushSide() {
  const el = document.getElementById('notifyState');
  if (!el) return;
  const s = await Push.state();
  const color = s === 'on' ? '#86efac' : s === 'default' || s === 'off' ? '#fde68a' : '#fca5a5';
  el.innerHTML = `<div style="margin-top:6px;color:${color}">${PUSH_TEXT[s]}</div>` +
    (s === 'default' ? '<button id="btnNotify" style="margin-top:6px">Bật thông báo</button>' : '') +
    (s !== 'on' ? '<div style="margin-top:4px"><a href="/settings#push" style="color:#93c5fd">Hướng dẫn</a></div>' : '');
  const b = document.getElementById('btnNotify');
  if (b) b.onclick = () => Push.turnOn();
  return s;
}

const Alerts = {
  key: 'pd_last_event',
  lastId: null,
  live: false,
  es: null,

  save(id) {
    if (this.lastId !== null && id <= this.lastId) return;
    this.lastId = id;
    try { localStorage.setItem(this.key, String(id)); } catch (e) { }
  },

  beep() {
    try {
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      [0, 0.25, 0.5].forEach(t => {
        const o = ctx.createOscillator(), g = ctx.createGain();
        o.frequency.value = 880; o.connect(g); g.connect(ctx.destination);
        g.gain.setValueAtTime(0.2, ctx.currentTime + t);
        g.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + t + 0.2);
        o.start(ctx.currentTime + t); o.stop(ctx.currentTime + t + 0.2);
      });
    } catch (e) { }
  },

  async systemNotify(title, body, tag, sticky) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return false;
    const opts = { body, tag, icon: '/static/icon.png', badge: '/static/icon.png', requireInteraction: sticky, data: { url: '/' } };
    try {
      const reg = 'serviceWorker' in navigator && await navigator.serviceWorker.getRegistration();
      if (reg) { await reg.showNotification(title, opts); return true; }
    } catch (e) { }
    try {
      const n = new Notification(title, opts);
      n.onclick = () => { window.focus(); n.close(); };
      return true;
    } catch (e) { return false; }
  },

  handle(ev) {
    if (this.lastId !== null && ev.id <= this.lastId) return;
    this.save(ev.id);
    if (Date.now() / 1000 - ev.ts > 900) return;
    const down = ev.type === 'down';
    const title = (down ? '⚠ Mất kết nối: ' : '✅ Đã phục hồi: ') + `${ev.name || ''} (${ev.ip || ''})`;
    toast(title, ev.message, down ? 'down' : 'up', down ? 0 : 10000);
    if (down) this.beep();
    this.systemNotify(title, ev.message, 'pd-ev-' + ev.id, down);
    if (window.onNewEvents) window.onNewEvents([ev]);
  },

  renderDown(down) {
    const el = document.getElementById('downBanner');
    const base = document.title.replace(/^\(\d+\) /, '');
    if (!down.length) { el.classList.add('hidden'); document.title = base; return; }
    el.classList.remove('hidden');
    el.innerHTML = `<b>⚠ ${down.length} địa chỉ đang mất kết nối:</b> ` +
      down.map(h => `${esc(h.name)} (${esc(h.ip)}) – từ ${fmtAgo(h.last_change)}`).join('; ');
    document.title = `(${down.length}) ` + base;
  },

  setLive(v) {
    this.live = v;
    const el = document.getElementById('liveState');
    if (el) el.innerHTML = v ? '<span style="color:#86efac">● Đang nhận cảnh báo trực tiếp</span>'
      : '<span style="color:#fca5a5">● Mất kết nối tới máy chủ, đang thử lại...</span>';
  },

  async poll() {
    try {
      const d = await api('/api/events?after=' + (this.lastId ?? 2147483647));
      if (this.lastId === null || this.lastId > d.last_id) {
        this.lastId = null;
        this.save(d.last_id);
      } else {
        d.events.forEach(ev => this.handle(ev));
      }
      this.renderDown(d.down);
    } catch (e) { }
  },

  connect() {
    if (!('EventSource' in window)) { setInterval(() => this.poll(), 20000); return; }
    this.es = new EventSource('/api/stream?after=' + this.lastId);
    this.es.onopen = () => this.setLive(true);
    this.es.onerror = () => this.setLive(false);
    this.es.addEventListener('alert', e => this.handle(JSON.parse(e.data)));
    this.es.addEventListener('state', e => { this.setLive(true); this.renderDown(JSON.parse(e.data).down); });
  },

  async start() {
    try { const v = parseInt(localStorage.getItem(this.key)); if (Number.isFinite(v)) this.lastId = v; } catch (e) { }
    await this.poll();
    this.connect();
    setInterval(() => this.poll(), 60000);
  },
};

document.addEventListener('DOMContentLoaded', async () => {
  if (window.isSecureContext && 'serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => undefined);
  }
  renderPushSide();
  if (window.APP.mustChange) return;
  Push.sync();
  Alerts.start();
});

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-bg.show').forEach(m => m.classList.remove('show'));
});
