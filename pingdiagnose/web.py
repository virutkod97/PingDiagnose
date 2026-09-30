"""Giao diện web + REST API."""
import csv
import io
import logging
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import db
from .config import APP_NAME, VERSION, resource_dir, secret_key
from .pinger import ping, valid_target

log = logging.getLogger("pingdiagnose.web")

SETTING_LIMITS = {
    "interval_seconds": (30, 86400),
    "ping_count": (1, 10),
    "ping_timeout_ms": (100, 10000),
    "fail_threshold": (1, 100),
    "retention_days": (0, 3650),
}

# chống dò mật khẩu: ip -> [số lần sai, thời điểm khoá đến]
_login_fail = {}


def create_app(monitor=None):
    app = Flask(
        __name__,
        template_folder=f"{resource_dir()}/templates",
        static_folder=f"{resource_dir()}/static",
    )
    app.config.update(
        SECRET_KEY=secret_key(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        JSON_AS_ASCII=False,
    )
    app.json.ensure_ascii = False
    app.monitor = monitor
    db.init_db()

    # ------------------------------------------------------------ auth helpers
    def current_user():
        uid = session.get("uid")
        if not uid:
            return None
        return db.query_one("SELECT id, username, role, must_change FROM users WHERE id = ?", (uid,))

    @app.before_request
    def load_user():
        g.user = current_user()
        if g.user is None and session.get("uid"):
            session.clear()
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.endpoint != "login":
            token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
            if not token or token != session.get("csrf"):
                abort(403)

    @app.context_processor
    def inject():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"user": g.get("user"), "csrf_token": session["csrf"], "app_name": APP_NAME,
                "version": VERSION}

    def login_required(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if g.user is None:
                if request.path.startswith("/api/"):
                    return jsonify(error="Chưa đăng nhập"), 401
                return redirect(url_for("login", next=request.path))
            if g.user["must_change"] and request.endpoint not in ("account", "api_change_password", "logout"):
                if request.path.startswith("/api/"):
                    return jsonify(error="Cần đổi mật khẩu trước khi sử dụng"), 403
                return redirect(url_for("account"))
            return fn(*a, **kw)
        return wrapper

    def admin_required(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            if g.user["role"] != "admin":
                if request.path.startswith("/api/"):
                    return jsonify(error="Bạn chỉ có quyền xem"), 403
                abort(403)
            return fn(*a, **kw)
        return wrapper

    def body():
        return request.get_json(silent=True) or {}

    # ------------------------------------------------------------ pages
    @app.route("/login", methods=["GET", "POST"])
    def login():
        error = None
        if request.method == "POST":
            ip = request.remote_addr or "?"
            fails, until = _login_fail.get(ip, (0, 0))
            if until > time.time():
                error = f"Đăng nhập sai quá nhiều lần. Thử lại sau {int(until - time.time()) // 60 + 1} phút."
            else:
                username = (request.form.get("username") or "").strip()
                password = request.form.get("password") or ""
                u = db.query_one("SELECT * FROM users WHERE username = ?", (username,))
                if u and check_password_hash(u["password_hash"], password):
                    _login_fail.pop(ip, None)
                    session.clear()
                    session.permanent = True
                    session["uid"] = u["id"]
                    session["csrf"] = secrets.token_urlsafe(32)
                    log.info("Đăng nhập: %s từ %s", username, ip)
                    nxt = request.args.get("next") or "/"
                    if not nxt.startswith("/") or nxt.startswith("//"):
                        nxt = "/"
                    return redirect(nxt)
                fails += 1
                # sai 5 lần liên tiếp -> khoá 5 phút
                _login_fail[ip] = (0, time.time() + 300) if fails >= 5 else (fails, 0)
                error = "Sai tên đăng nhập hoặc mật khẩu"
        if g.user:
            return redirect("/")
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/")
    @login_required
    def dashboard():
        return render_template("dashboard.html", page="dashboard")

    @app.route("/hosts")
    @login_required
    def hosts_page():
        return render_template("hosts.html", page="hosts")

    @app.route("/reports")
    @login_required
    def reports_page():
        return render_template("reports.html", page="reports")

    @app.route("/events")
    @login_required
    def events_page():
        return render_template("events.html", page="events")

    @app.route("/users")
    @admin_required
    def users_page():
        return render_template("users.html", page="users")

    @app.route("/settings")
    @login_required
    def settings_page():
        return render_template("settings.html", page="settings")

    @app.route("/account")
    @login_required
    def account():
        return render_template("account.html", page="account")

    @app.errorhandler(403)
    def forbidden(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Không có quyền"), 403
        return render_template("error.html", code=403, message="Bạn không có quyền truy cập trang này."), 403

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="Không tìm thấy"), 404
        return render_template("error.html", code=404, message="Không tìm thấy trang."), 404

    # ------------------------------------------------------------ API: dashboard
    @app.route("/api/dashboard")
    @login_required
    def api_dashboard():
        now = int(time.time())
        day = now - 86400
        week = now - 7 * 86400
        hosts = db.query("SELECT * FROM hosts ORDER BY name COLLATE NOCASE")
        stats24 = {r["host_id"]: r for r in db.query(
            "SELECT host_id, COUNT(*) n, SUM(received > 0) ok, SUM(sent) sent, SUM(received) recv, "
            "AVG(rtt_avg) rtt FROM checks WHERE ts >= ? GROUP BY host_id", (day,))}
        stats7 = {r["host_id"]: r for r in db.query(
            "SELECT host_id, COUNT(*) n, SUM(received > 0) ok FROM checks WHERE ts >= ? GROUP BY host_id",
            (week,))}
        out = []
        tot_n = tot_ok = tot_sent = tot_recv = 0
        for h in hosts:
            s = stats24.get(h["id"])
            s7 = stats7.get(h["id"])
            item = {k: h[k] for k in ("id", "name", "ip", "description", "enabled", "status",
                                      "consecutive_fail", "last_check", "last_rtt", "last_change")}
            item["avail_24h"] = pct(s["ok"], s["n"]) if s else None
            item["packet_24h"] = pct(s["recv"], s["sent"]) if s else None
            item["rtt_24h"] = round(s["rtt"], 1) if s and s["rtt"] is not None else None
            item["avail_7d"] = pct(s7["ok"], s7["n"]) if s7 else None
            out.append(item)
            if s and h["enabled"]:
                tot_n += s["n"]; tot_ok += s["ok"]; tot_sent += s["sent"]; tot_recv += s["recv"]
        enabled = [h for h in hosts if h["enabled"]]
        counts = {
            "total": len(hosts),
            "enabled": len(enabled),
            "up": sum(1 for h in enabled if h["status"] == "up"),
            "warning": sum(1 for h in enabled if h["status"] == "warning"),
            "down": sum(1 for h in enabled if h["status"] == "down"),
            "unknown": sum(1 for h in enabled if h["status"] == "unknown"),
        }
        off = tz_offset()
        trend = db.query(
            "SELECT ((ts + ?) / 3600) * 3600 - ? AS b, COUNT(*) n, SUM(received > 0) ok, AVG(rtt_avg) rtt "
            "FROM checks c JOIN hosts h ON h.id = c.host_id AND h.enabled = 1 "
            "WHERE ts >= ? GROUP BY b ORDER BY b", (off, off, day))
        events = db.query(
            "SELECT e.*, h.name, h.ip FROM events e LEFT JOIN hosts h ON h.id = e.host_id "
            "ORDER BY e.id DESC LIMIT 15")
        m = app.monitor
        return jsonify(
            counts=counts,
            avail_24h=pct(tot_ok, tot_n),
            packet_24h=pct(tot_recv, tot_sent),
            hosts=out,
            trend=[{"ts": r["b"], "label": time.strftime("%H:%M %d/%m", time.localtime(r["b"])),
                    "avail": pct(r["ok"], r["n"]),
                    "rtt": round(r["rtt"], 1) if r["rtt"] is not None else None} for r in trend],
            events=[fmt_event(e) for e in events],
            last_cycle=m.last_cycle if m else None,
            next_cycle=m.next_cycle if m else None,
            now=now,
            settings=db.get_settings(),
        )

    # ------------------------------------------------------------ API: hosts
    @app.route("/api/hosts")
    @login_required
    def api_hosts():
        return jsonify(hosts=db.query("SELECT * FROM hosts ORDER BY name COLLATE NOCASE"))

    def clean_host(data):
        ip = (data.get("ip") or "").strip()
        name = (data.get("name") or "").strip() or ip
        desc = (data.get("description") or "").strip()
        if not valid_target(ip):
            return None, f"Địa chỉ không hợp lệ: {ip or '(trống)'}"
        if len(name) > 100 or len(desc) > 500:
            return None, "Tên hoặc mô tả quá dài"
        return {"ip": ip, "name": name, "description": desc,
                "enabled": 1 if data.get("enabled", True) else 0}, None

    @app.route("/api/hosts", methods=["POST"])
    @admin_required
    def api_host_create():
        h, err = clean_host(body())
        if err:
            return jsonify(error=err), 400
        if db.query_one("SELECT id FROM hosts WHERE ip = ?", (h["ip"],)):
            return jsonify(error=f"Địa chỉ {h['ip']} đã tồn tại"), 400
        hid = db.execute(
            "INSERT INTO hosts(name, ip, description, enabled, created_at) VALUES (?,?,?,?,?)",
            (h["name"], h["ip"], h["description"], h["enabled"], int(time.time())))
        log.info("%s thêm địa chỉ %s", g.user["username"], h["ip"])
        return jsonify(id=hid)

    @app.route("/api/hosts/import", methods=["POST"])
    @admin_required
    def api_host_import():
        text = body().get("text") or ""
        added, errors = 0, []
        for ln, line in enumerate(text.splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.replace(";", ",").replace("\t", ",").split(",")]
            h, err = clean_host({"ip": parts[0], "name": parts[1] if len(parts) > 1 else "",
                                 "description": parts[2] if len(parts) > 2 else ""})
            if err:
                errors.append(f"Dòng {ln}: {err}")
                continue
            if db.query_one("SELECT id FROM hosts WHERE ip = ?", (h["ip"],)):
                errors.append(f"Dòng {ln}: {h['ip']} đã tồn tại")
                continue
            db.execute("INSERT INTO hosts(name, ip, description, enabled, created_at) VALUES (?,?,?,?,?)",
                       (h["name"], h["ip"], h["description"], 1, int(time.time())))
            added += 1
        return jsonify(added=added, errors=errors)

    @app.route("/api/hosts/<int:hid>", methods=["PUT"])
    @admin_required
    def api_host_update(hid):
        cur = db.query_one("SELECT * FROM hosts WHERE id = ?", (hid,))
        if not cur:
            return jsonify(error="Không tìm thấy"), 404
        h, err = clean_host(body())
        if err:
            return jsonify(error=err), 400
        dup = db.query_one("SELECT id FROM hosts WHERE ip = ? AND id != ?", (h["ip"], hid))
        if dup:
            return jsonify(error=f"Địa chỉ {h['ip']} đã tồn tại"), 400
        reset = h["ip"].lower() != cur["ip"].lower() or (h["enabled"] and not cur["enabled"])
        with db.tx() as conn:
            conn.execute("UPDATE hosts SET name=?, ip=?, description=?, enabled=? WHERE id=?",
                         (h["name"], h["ip"], h["description"], h["enabled"], hid))
            if reset:
                conn.execute("UPDATE hosts SET status='unknown', consecutive_fail=0, last_change=NULL "
                             "WHERE id=?", (hid,))
        return jsonify(ok=True)

    @app.route("/api/hosts/<int:hid>", methods=["DELETE"])
    @admin_required
    def api_host_delete(hid):
        with db.tx() as conn:
            conn.execute("DELETE FROM checks WHERE host_id = ?", (hid,))
            conn.execute("DELETE FROM events WHERE host_id = ?", (hid,))
            conn.execute("DELETE FROM hosts WHERE id = ?", (hid,))
        log.info("%s xoá địa chỉ id=%s", g.user["username"], hid)
        return jsonify(ok=True)

    @app.route("/api/hosts/test", methods=["POST"])
    @admin_required
    def api_host_test():
        ip = (body().get("ip") or "").strip()
        if not valid_target(ip):
            return jsonify(error="Địa chỉ không hợp lệ"), 400
        s = db.get_settings()
        return jsonify(ping(ip, s["ping_count"], s["ping_timeout_ms"]).to_dict())

    # ------------------------------------------------------------ API: events
    @app.route("/api/events")
    @login_required
    def api_events():
        after = request.args.get("after", type=int)
        limit = min(request.args.get("limit", 200, type=int), 1000)
        if after is not None:
            rows = db.query(
                "SELECT e.*, h.name, h.ip FROM events e LEFT JOIN hosts h ON h.id = e.host_id "
                "WHERE e.id > ? ORDER BY e.id LIMIT ?", (after, limit))
        else:
            params, where = [], []
            if request.args.get("host_id", type=int):
                where.append("e.host_id = ?"); params.append(request.args.get("host_id", type=int))
            if request.args.get("type") in ("up", "down"):
                where.append("e.type = ?"); params.append(request.args["type"])
            sql = ("SELECT e.*, h.name, h.ip FROM events e LEFT JOIN hosts h ON h.id = e.host_id "
                   + (("WHERE " + " AND ".join(where)) if where else "") + " ORDER BY e.id DESC LIMIT ?")
            rows = db.query(sql, params + [limit])
        last = db.query_one("SELECT MAX(id) m FROM events")["m"] or 0
        down = db.query("SELECT id, name, ip, last_change FROM hosts WHERE enabled = 1 AND status = 'down'")
        return jsonify(events=[fmt_event(e) for e in rows], last_id=last, down=down)

    # ------------------------------------------------------------ API: reports
    def report_params():
        today = datetime.now().date()
        try:
            d_from = datetime.strptime(request.args.get("from") or "", "%Y-%m-%d")
        except ValueError:
            d_from = datetime.combine(today - timedelta(days=6), datetime.min.time())
        try:
            d_to = datetime.strptime(request.args.get("to") or "", "%Y-%m-%d")
        except ValueError:
            d_to = datetime.combine(today, datetime.min.time())
        if d_to < d_from:
            d_from, d_to = d_to, d_from
        start = int(time.mktime(d_from.timetuple()))
        end = int(time.mktime((d_to + timedelta(days=1)).timetuple()))
        bucket = request.args.get("bucket") or ("hour" if end - start <= 3 * 86400 else "day")
        if bucket not in ("hour", "day"):
            bucket = "day"
        host_id = request.args.get("host_id", type=int)
        return start, end, bucket, host_id, d_from, d_to

    def build_report():
        start, end, bucket, host_id, d_from, d_to = report_params()
        size = 3600 if bucket == "hour" else 86400
        off = tz_offset()
        hfilter, params = "", []
        if host_id:
            hfilter = " AND c.host_id = ?"
            params = [host_id]
        rows = db.query(
            f"SELECT ((c.ts + ?) / {size}) * {size} - ? AS b, COUNT(*) n, SUM(c.received > 0) ok, "
            "SUM(c.sent) sent, SUM(c.received) recv, AVG(c.rtt_avg) rtt, "
            "COUNT(DISTINCT c.host_id) hosts FROM checks c "
            f"WHERE c.ts >= ? AND c.ts < ?{hfilter} GROUP BY b ORDER BY b",
            [off, off, start, end] + params)
        by_b = {r["b"]: r for r in rows}
        fmt = "%H:00 %d/%m" if bucket == "hour" else "%d/%m/%Y"
        series = []
        t = start
        while t < end:
            r = by_b.get(t)
            series.append({
                "ts": t,
                "label": time.strftime(fmt, time.localtime(t)),
                "checks": r["n"] if r else 0,
                "ok": r["ok"] if r else 0,
                "fail": (r["n"] - r["ok"]) if r else 0,
                "avail": pct(r["ok"], r["n"]) if r else None,
                "packet": pct(r["recv"], r["sent"]) if r else None,
                "rtt": round(r["rtt"], 1) if r and r["rtt"] is not None else None,
            })
            # cộng theo lịch để không lệch khi đổi giờ
            nxt = datetime.fromtimestamp(t) + timedelta(seconds=size)
            t = int(time.mktime(nxt.timetuple()))
        interval = db.get_settings()["interval_seconds"]
        summary = db.query(
            "SELECT h.id, h.name, h.ip, COUNT(c.id) n, SUM(c.received > 0) ok, SUM(c.sent) sent, "
            "SUM(c.received) recv, AVG(c.rtt_avg) rtt, MIN(c.rtt_avg) rtt_min, MAX(c.rtt_avg) rtt_max "
            "FROM hosts h LEFT JOIN checks c ON c.host_id = h.id AND c.ts >= ? AND c.ts < ? "
            + ("WHERE h.id = ? " if host_id else "") +
            "GROUP BY h.id ORDER BY h.name COLLATE NOCASE", [start, end] + params)
        downs = {r["host_id"]: r["n"] for r in db.query(
            "SELECT host_id, COUNT(*) n FROM events WHERE type='down' AND ts >= ? AND ts < ? GROUP BY host_id",
            (start, end))}
        table = []
        tot = {"checks": 0, "ok": 0, "sent": 0, "recv": 0, "down_events": sum(downs.values())}
        for s in summary:
            tot["checks"] += s["n"] or 0
            tot["ok"] += s["ok"] or 0
            tot["sent"] += s["sent"] or 0
            tot["recv"] += s["recv"] or 0
            n, ok = s["n"] or 0, s["ok"] or 0
            table.append({
                "id": s["id"], "name": s["name"], "ip": s["ip"],
                "checks": n, "ok": ok, "fail": n - ok,
                "avail": pct(ok, n), "packet": pct(s["recv"], s["sent"]),
                "rtt": round(s["rtt"], 1) if s["rtt"] is not None else None,
                "rtt_min": s["rtt_min"], "rtt_max": s["rtt_max"],
                "down_events": downs.get(s["id"], 0),
                "downtime": (n - ok) * interval,
                "downtime_text": fmt_dur((n - ok) * interval),
            })
        ev_sql = ("SELECT e.*, h.name, h.ip FROM events e LEFT JOIN hosts h ON h.id = e.host_id "
                  "WHERE e.ts >= ? AND e.ts < ?" + (" AND e.host_id = ?" if host_id else "") +
                  " ORDER BY e.ts DESC LIMIT 500")
        events = db.query(ev_sql, [start, end] + params)
        return {
            "from": d_from.strftime("%Y-%m-%d"), "to": d_to.strftime("%Y-%m-%d"),
            "bucket": bucket, "host_id": host_id,
            "series": series, "table": table,
            "totals": dict(tot, avail=pct(tot["ok"], tot["checks"]), packet=pct(tot["recv"], tot["sent"])),
            "events": [fmt_event(e) for e in events],
        }

    @app.route("/api/report")
    @login_required
    def api_report():
        return jsonify(build_report())

    @app.route("/api/report.csv")
    @login_required
    def api_report_csv():
        rep = build_report()
        buf = io.StringIO()
        buf.write("﻿")  # BOM để Excel đọc đúng tiếng Việt
        w = csv.writer(buf)
        w.writerow([f"Báo cáo kết nối từ {rep['from']} đến {rep['to']}"])
        w.writerow([])
        w.writerow(["Tên", "Địa chỉ", "Số lần kiểm tra", "Thành công", "Thất bại",
                    "Tỷ lệ kết nối (%)", "Tỷ lệ gói nhận (%)", "RTT TB (ms)", "RTT min", "RTT max",
                    "Số lần cảnh báo", "Thời gian mất kết nối (ước tính)"])
        for r in rep["table"]:
            w.writerow([r["name"], r["ip"], r["checks"], r["ok"], r["fail"], r["avail"], r["packet"],
                        r["rtt"], r["rtt_min"], r["rtt_max"], r["down_events"], r["downtime_text"]])
        w.writerow([])
        w.writerow(["Thời gian", "Số lần kiểm tra", "Thành công", "Thất bại", "Tỷ lệ kết nối (%)",
                    "Tỷ lệ gói nhận (%)", "RTT TB (ms)"])
        for s in rep["series"]:
            w.writerow([s["label"], s["checks"], s["ok"], s["fail"], s["avail"], s["packet"], s["rtt"]])
        w.writerow([])
        w.writerow(["Thời điểm", "Loại", "Địa chỉ", "Nội dung"])
        for e in rep["events"]:
            w.writerow([e["time"], "Mất kết nối" if e["type"] == "down" else "Phục hồi", e["ip"], e["message"]])
        fname = f"baocao_{rep['from']}_{rep['to']}.csv"
        return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

    # ------------------------------------------------------------ API: users
    @app.route("/api/users")
    @admin_required
    def api_users():
        return jsonify(users=db.query("SELECT id, username, role, must_change, created_at FROM users ORDER BY id"))

    @app.route("/api/users", methods=["POST"])
    @admin_required
    def api_user_create():
        d = body()
        username = (d.get("username") or "").strip()
        password = d.get("password") or ""
        role = d.get("role")
        if not username or len(username) > 50 or not username.replace("_", "").replace(".", "").replace("-", "").isalnum():
            return jsonify(error="Tên đăng nhập không hợp lệ (chữ, số, . _ -)"), 400
        if role not in ("admin", "viewer"):
            return jsonify(error="Quyền không hợp lệ"), 400
        if len(password) < 6:
            return jsonify(error="Mật khẩu tối thiểu 6 ký tự"), 400
        if db.query_one("SELECT id FROM users WHERE username = ?", (username,)):
            return jsonify(error="Tên đăng nhập đã tồn tại"), 400
        uid = db.execute(
            "INSERT INTO users(username, password_hash, role, must_change, created_at) VALUES (?,?,?,?,?)",
            (username, generate_password_hash(password), role, 1 if d.get("must_change") else 0, int(time.time())))
        return jsonify(id=uid)

    def admin_count():
        return db.query_one("SELECT COUNT(*) n FROM users WHERE role = 'admin'")["n"]

    @app.route("/api/users/<int:uid>", methods=["PUT"])
    @admin_required
    def api_user_update(uid):
        u = db.query_one("SELECT * FROM users WHERE id = ?", (uid,))
        if not u:
            return jsonify(error="Không tìm thấy"), 404
        d = body()
        role = d.get("role", u["role"])
        if role not in ("admin", "viewer"):
            return jsonify(error="Quyền không hợp lệ"), 400
        if u["role"] == "admin" and role != "admin" and admin_count() <= 1:
            return jsonify(error="Phải còn ít nhất một tài khoản quản trị"), 400
        password = d.get("password") or ""
        if password and len(password) < 6:
            return jsonify(error="Mật khẩu tối thiểu 6 ký tự"), 400
        with db.tx() as conn:
            conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, uid))
            if password:
                conn.execute("UPDATE users SET password_hash = ?, must_change = ? WHERE id = ?",
                             (generate_password_hash(password), 1 if d.get("must_change") else 0, uid))
        return jsonify(ok=True)

    @app.route("/api/users/<int:uid>", methods=["DELETE"])
    @admin_required
    def api_user_delete(uid):
        u = db.query_one("SELECT * FROM users WHERE id = ?", (uid,))
        if not u:
            return jsonify(error="Không tìm thấy"), 404
        if uid == g.user["id"]:
            return jsonify(error="Không thể tự xoá tài khoản đang đăng nhập"), 400
        if u["role"] == "admin" and admin_count() <= 1:
            return jsonify(error="Phải còn ít nhất một tài khoản quản trị"), 400
        db.execute("DELETE FROM users WHERE id = ?", (uid,))
        return jsonify(ok=True)

    @app.route("/api/account/password", methods=["POST"])
    @login_required
    def api_change_password():
        d = body()
        u = db.query_one("SELECT * FROM users WHERE id = ?", (g.user["id"],))
        if not check_password_hash(u["password_hash"], d.get("old_password") or ""):
            return jsonify(error="Mật khẩu hiện tại không đúng"), 400
        new = d.get("new_password") or ""
        if len(new) < 6:
            return jsonify(error="Mật khẩu mới tối thiểu 6 ký tự"), 400
        if new == d.get("old_password"):
            return jsonify(error="Mật khẩu mới phải khác mật khẩu cũ"), 400
        db.execute("UPDATE users SET password_hash = ?, must_change = 0 WHERE id = ?",
                   (generate_password_hash(new), u["id"]))
        return jsonify(ok=True)

    # ------------------------------------------------------------ API: settings
    @app.route("/api/settings")
    @login_required
    def api_settings():
        return jsonify(settings=db.get_settings())

    @app.route("/api/settings", methods=["PUT"])
    @admin_required
    def api_settings_update():
        d = body()
        values = {}
        for k, (lo, hi) in SETTING_LIMITS.items():
            if k in d:
                try:
                    v = int(d[k])
                except (TypeError, ValueError):
                    return jsonify(error=f"Giá trị không hợp lệ: {k}"), 400
                if not lo <= v <= hi:
                    return jsonify(error=f"{k} phải trong khoảng {lo} - {hi}"), 400
                values[k] = v
        db.set_settings(values)
        log.info("%s cập nhật cấu hình %s", g.user["username"], values)
        return jsonify(settings=db.get_settings())

    return app


# ---------------------------------------------------------------- helpers
def pct(a, b):
    if not b:
        return None
    return round(100.0 * (a or 0) / b, 2)


def tz_offset():
    return time.localtime().tm_gmtoff or 0


def fmt_dur(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "0"
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    parts = []
    if d:
        parts.append(f"{d} ngày")
    if h:
        parts.append(f"{h} giờ")
    if m or not parts:
        parts.append(f"{m} phút")
    return " ".join(parts)


def fmt_event(e):
    e = dict(e)
    e["time"] = time.strftime("%H:%M:%S %d/%m/%Y", time.localtime(e["ts"]))
    return e
