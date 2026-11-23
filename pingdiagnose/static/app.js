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

const STATUS_TEXT = { up: 'Hoạt động', down: 'Mất kết nối', warning: 'Cảnh báo', unknown: 'Chưa kiểm tra' };
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

function pager(el, total, page, size, go) {
  const pages = Math.max(1, Math.ceil(total / size));
  const from = total ? (page - 1) * size + 1 : 0, to = Math.min(total, page * size);
  el.innerHTML = `<span class="muted">${from}–${to} / ${total}</span>
    <button class="btn-sm" data-p="1" ${page <= 1 ? 'disabled' : ''}>«</button>
    <button class="btn-sm" data-p="${page - 1}" ${page <= 1 ? 'disabled' : ''}>‹</button>
    <span>Trang ${page} / ${pages}</span>
    <button class="btn-sm" data-p="${page + 1}" ${page >= pages ? 'disabled' : ''}>›</button>
    <button class="btn-sm" data-p="${pages}" ${page >= pages ? 'disabled' : ''}>»</button>
    <select class="btn-sm">${[20, 50, 100].map(n => `<option value="${n}" ${n === size ? 'selected' : ''}>${n}/trang</option>`).join('')}</select>`;
  el.querySelectorAll('button').forEach(b => b.onclick = () => go(+b.dataset.p, size));
  el.querySelector('select').onchange = e => go(1, +e.target.value);
}

async function loadGroups(sel) {
  const d = await api('/api/groups');
  sel.innerHTML = '<option value="*">Tất cả nhóm</option>' + d.groups.map(g =>
    `<option value="${esc(g.grp)}">${g.grp ? esc(g.grp) : '(Không có nhóm)'} (${g.n})</option>`).join('');
  return d.groups;
}

function setGroup(p, sel) {
  if (sel && sel.value !== '*' && sel.value !== undefined && sel.options.length) p.set('group', sel.value);
}

function debounce(fn, ms = 300) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function openModal(id) { document.getElementById(id).classList.add('show'); }
function closeModal(id) { document.getElementById(id).classList.remove('show'); }

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-bg.show').forEach(m => m.classList.remove('show'));
});

const Alerts = (() => {
  let ctx = null, lastId = null, mode = 2, sound = 1;
  const LS = 'pd_last_event', MUTE = 'pd_mute';
  const ls = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } };

  function audio() {
    if (!ctx) { try { ctx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) { return null; } }
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }
  function tone(freq, start, dur, type = 'sine', vol = 0.25) {
    const a = audio(); if (!a) return;
    const o = a.createOscillator(), g = a.createGain(), t = a.currentTime + start;
    o.type = type; o.frequency.setValueAtTime(freq, t);
    g.gain.setValueAtTime(0, t); g.gain.linearRampToValueAtTime(vol, t + 0.02);
    g.gain.setValueAtTime(vol, t + dur - 0.05); g.gain.linearRampToValueAtTime(0, t + dur);
    o.connect(g); g.connect(a.destination); o.start(t); o.stop(t + dur + 0.02);
  }
  function play(type) {
    if (type === 'down') { for (let i = 0; i < 3; i++) { tone(880, i * 0.5, 0.22, 'square', 0.15); tone(660, i * 0.5 + 0.24, 0.22, 'square', 0.15); } }
    else { tone(523, 0, 0.18); tone(659, 0.18, 0.18); tone(784, 0.36, 0.35); }
  }
  const muted = () => ls(MUTE) === '1';

  function desktop(title, body, tag) {
    if (!('Notification' in window) || Notification.permission !== 'granted') return;
    try {
      const n = new Notification(title, { body, tag, icon: '/static/apple-touch-icon.png', requireInteraction: title.startsWith('⚠') });
      n.onclick = () => { window.focus(); n.close(); };
    } catch (e) { }
  }

  function notify(events) {
    events = events.filter(e => e.type === 'down' || mode === 2);
    if (!events.length) return;
    const downs = events.filter(e => e.type === 'down');
    events.slice(-5).forEach(e => toast(e.type === 'down' ? '⚠ Mất kết nối' : '✅ Phục hồi', `${e.text} (${e.time})`, e.type, e.type === 'down' ? 60000 : 15000));
    if (events.length <= 3) events.forEach(e => desktop(e.type === 'down' ? '⚠ Mất kết nối' : '✅ Phục hồi', e.text, 'ev' + e.id));
    else desktop(downs.length ? `⚠ ${downs.length} địa chỉ mất kết nối` : `✅ ${events.length} địa chỉ phục hồi`,
      events.slice(-5).map(e => (e.type === 'down' ? '⚠ ' : '✅ ') + e.text).join('\n'), 'evbatch');
    if (sound && !muted()) play(downs.length ? 'down' : 'up');
    document.dispatchEvent(new CustomEvent('pd-events', { detail: events }));
  }

  async function poll() {
    const stored = parseInt(ls(LS));
    const after = Math.max(lastId ?? -1, isNaN(stored) ? -1 : stored);
    let d;
    try { d = await api('/api/events/new' + (after >= 0 ? '?after=' + after : '')); } catch (e) { return; }
    mode = d.notify_mode; sound = d.notify_sound;
    const cur = parseInt(ls(LS));
    if (!isNaN(cur) && cur > after) { lastId = cur; return; }
    lastId = d.last_id; ls(LS, d.last_id);
    if (mode && d.events.length) notify(d.events);
  }

  function renderControls() {
    const box = document.getElementById('alertCtl');
    if (!box) return;
    const perm = 'Notification' in window ? Notification.permission : 'unsupported';
    box.innerHTML = (perm === 'default' ? '<button type="button" id="btnNotif">🔔 Bật thông báo</button>' : '') +
      `<button type="button" id="btnMute">${muted() ? '🔇 Đang tắt tiếng' : '🔊 Âm thanh: Bật'}</button>`;
    const bn = document.getElementById('btnNotif');
    if (bn) bn.onclick = async () => { audio(); await Notification.requestPermission(); renderControls(); };
    document.getElementById('btnMute').onclick = () => { ls(MUTE, muted() ? '0' : '1'); if (!muted()) play('up'); renderControls(); };
  }

  function start() {
    ['click', 'keydown'].forEach(ev => document.addEventListener(ev, () => audio(), { once: true }));
    renderControls();
    poll();
    setInterval(poll, 15000);
  }
  return { start, play };
})();

if (window.APP && !APP.mustChange) Alerts.start();
