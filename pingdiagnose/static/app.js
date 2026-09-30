/* Tiện ích chung + thông báo cảnh báo qua trình duyệt */
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
  try { data = await r.json(); } catch (e) { /* ignore */ }
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

function fmtDur(s) {
  s = Math.max(0, Math.floor(s));
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return (d ? d + ' ngày ' : '') + (h ? h + ' giờ ' : '') + m + ' phút';
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

/* ---------------- Thông báo trình duyệt ---------------- */
const Notify = {
  key: 'pd_last_event',
  canNative: 'Notification' in window && window.isSecureContext,

  renderState() {
    const el = document.getElementById('notifyState');
    if (!el) return;
    if (!('Notification' in window)) {
      el.innerHTML = '<div class="muted" style="margin-top:6px">Trình duyệt không hỗ trợ thông báo – dùng cảnh báo trong trang.</div>';
    } else if (!window.isSecureContext) {
      el.innerHTML = '<div class="muted" style="margin-top:6px" title="Trình duyệt chỉ cho phép thông báo hệ thống khi truy cập qua HTTPS hoặc localhost. Xem README.">⚠ Thông báo hệ thống cần HTTPS/localhost – đang dùng cảnh báo trong trang.</div>';
    } else if (Notification.permission === 'granted') {
      el.innerHTML = '<div style="margin-top:6px;color:#86efac">🔔 Đã bật thông báo</div>';
    } else if (Notification.permission === 'denied') {
      el.innerHTML = '<div style="margin-top:6px;color:#fca5a5">🔕 Thông báo đã bị chặn trong trình duyệt</div>';
    } else {
      el.innerHTML = '<button id="btnNotify" style="margin-top:8px">🔔 Bật thông báo</button>';
      document.getElementById('btnNotify').onclick = () => Notification.requestPermission().then(() => this.renderState());
    }
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
    } catch (e) { /* ignore */ }
  },

  show(ev) {
    const down = ev.type === 'down';
    const title = down ? '⚠ Mất kết nối: ' + (ev.ip || '') : '✅ Đã phục hồi: ' + (ev.ip || '');
    toast(title, ev.message, down ? 'down' : 'up', down ? 0 : 10000);
    if (this.canNative && Notification.permission === 'granted') {
      try {
        const n = new Notification(title, { body: ev.message, tag: 'pd-' + ev.id, requireInteraction: down });
        n.onclick = () => { window.focus(); location.href = '/'; n.close(); };
      } catch (e) { /* ignore */ }
    }
  },

  renderDown(down) {
    const el = document.getElementById('downBanner');
    if (!down.length) { el.classList.add('hidden'); document.title = document.title.replace(/^\(\d+\) /, ''); return; }
    el.classList.remove('hidden');
    el.innerHTML = `<b>⚠ ${down.length} địa chỉ đang mất kết nối:</b> ` +
      down.map(h => `${esc(h.name)} (${esc(h.ip)}) – từ ${fmtAgo(h.last_change)}`).join('; ');
    document.title = `(${down.length}) ` + document.title.replace(/^\(\d+\) /, '');
  },

  async poll() {
    let last = null;
    try { last = parseInt(localStorage.getItem(this.key)); } catch (e) { /* ignore */ }
    try {
      const url = Number.isFinite(last) ? '/api/events?after=' + last : '/api/events?after=2147483647';
      const d = await api(url);
      if (Number.isFinite(last) && last <= d.last_id) {
        let hasDown = false;
        d.events.forEach(ev => { this.show(ev); if (ev.type === 'down') hasDown = true; });
        if (hasDown) this.beep();
        if (d.events.length && window.onNewEvents) window.onNewEvents(d.events);
      }
      const newLast = d.events.length && Number.isFinite(last) && last <= d.last_id ? d.events[d.events.length - 1].id : d.last_id;
      try { localStorage.setItem(this.key, String(newLast)); } catch (e) { /* ignore */ }
      this.renderDown(d.down);
    } catch (e) { /* mạng lỗi: bỏ qua, thử lại lần sau */ }
  },

  start() {
    this.renderState();
    if (window.APP.mustChange) return;
    this.poll();
    setInterval(() => this.poll(), 20000);
  },
};

document.addEventListener('DOMContentLoaded', () => Notify.start());
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-bg.show').forEach(m => m.classList.remove('show'));
});
