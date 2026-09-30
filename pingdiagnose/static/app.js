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

document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-bg.show').forEach(m => m.classList.remove('show'));
});
