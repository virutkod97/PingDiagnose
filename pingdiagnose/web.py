import csv
import io
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, g, jsonify, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from . import certs, db
from .config import APP_NAME, VERSION, data_dir, load_config, resource_dir, secret_key
from .pinger import ping, valid_target

log = logging.getLogger("pingdiagnose.web")

SETTING_LIMITS = {
    "interval_seconds": (30, 86400),
    "ping_count": (1, 10),
    "ping_timeout_ms": (100, 10000),
    "fail_threshold": (1, 100),
    "retention_days": (0, 3650),
}

_login_fail = {}


def create_app():
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
    )
    app.json.ensure_ascii = False
    db.init_db()

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
        return render_template("dashboard.html", page="dashboard", title=db.get_text("dashboard_title"))

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

    def page_args(default=20):
        page = max(1, request.args.get("page", 1, type=int))
        size = min(max(5, request.args.get("size", default, type=int)), 200)
        return page, size, (page - 1) * size

    def host_filter():
        where, params = [], []
        q = (request.args.get("q") or "").strip()
        if q:
            where.append("(h.name LIKE ? OR h.ip LIKE ? OR h.description LIKE ?)")
            params += [f"%{q}%"] * 3
        st = request.args.get("status")
        if st in ("up", "down", "warning", "unknown"):
            where.append("h.enabled = 1 AND h.status = ?")
            params.append(st)
        elif st == "off":
            where.append("h.enabled = 0")
        return (" WHERE " + " AND ".join(where)) if where else "", params

    @app.route("/api/dashboard")
    @login_required
    def api_dashboard():
        now = int(time.time())
        day, week = now - 86400, now - 7 * 86400
        c = db.query_one(
            "SELECT COUNT(*) total, SUM(enabled) enabled, "
            "SUM(enabled AND status='up') up, SUM(enabled AND status='warning') warning, "
            "SUM(enabled AND status='down') down, SUM(enabled AND status='unknown') unknown FROM hosts")
        counts = {k: c[k] or 0 for k in c}
        tot = db.query_one(
            "SELECT COUNT(*) n, SUM(c.received > 0) ok, SUM(c.sent) sent, SUM(c.received) recv "
            "FROM checks c JOIN hosts h ON h.id = c.host_id AND h.enabled = 1 WHERE c.ts >= ?", (day,))
        page, size, offset = page_args()
        where, params = host_filter()
        total = db.query_one(f"SELECT COUNT(*) n FROM hosts h{where}", params)["n"]
        hosts = db.query(
            f"SELECT h.* FROM hosts h{where} ORDER BY h.enabled DESC, "
            "CASE h.status WHEN 'down' THEN 0 WHEN 'warning' THEN 1 WHEN 'unknown' THEN 2 ELSE 3 END, "
            "h.name COLLATE NOCASE LIMIT ? OFFSET ?", params + [size, offset])
        ids = [h["id"] for h in hosts]
        stats24, stats7 = {}, {}
        if ids:
            marks = ",".join("?" * len(ids))
            stats24 = {r["host_id"]: r for r in db.query(
                "SELECT host_id, COUNT(*) n, SUM(received > 0) ok FROM checks "
                f"WHERE ts >= ? AND host_id IN ({marks}) GROUP BY host_id", [day] + ids)}
            stats7 = {r["host_id"]: r for r in db.query(
                "SELECT host_id, COUNT(*) n, SUM(received > 0) ok FROM checks "
                f"WHERE ts >= ? AND host_id IN ({marks}) GROUP BY host_id", [week] + ids)}
        out = []
        for h in hosts:
            s24, s7 = stats24.get(h["id"]), stats7.get(h["id"])
            item = {k: h[k] for k in ("id", "name", "ip", "description", "enabled", "status",
                                      "consecutive_fail", "last_check", "last_rtt", "last_change")}
            item["avail_24h"] = pct(s24["ok"], s24["n"]) if s24 else None
            item["avail_7d"] = pct(s7["ok"], s7["n"]) if s7 else None
            out.append(item)
        off = tz_offset()
        trend = db.query(
            "SELECT ((ts + ?) / 3600) * 3600 - ? AS b, COUNT(*) n, SUM(received > 0) ok, AVG(rtt_avg) rtt "
            "FROM checks c JOIN hosts h ON h.id = c.host_id AND h.enabled = 1 "
            "WHERE ts >= ? GROUP BY b ORDER BY b", (off, off, day))
        events = db.query(
            "SELECT e.*, h.name, h.ip FROM events e LEFT JOIN hosts h ON h.id = e.host_id "
            "ORDER BY e.id DESC LIMIT 10")
        return jsonify(
            counts=counts,
            avail_24h=pct(tot["ok"], tot["n"]),
            packet_24h=pct(tot["recv"], tot["sent"]),
            hosts=out, total=total, page=page, size=size,
            trend=[{"ts": r["b"], "label": time.strftime("%H:%M", time.localtime(r["b"])),
                    "avail": pct(r["ok"], r["n"]),
                    "rtt": round(r["rtt"], 1) if r["rtt"] is not None else None} for r in trend],
            events=[fmt_event(e) for e in events],
        )

    @app.route("/api/hosts")
    @login_required
    def api_hosts():
        page, size, offset = page_args()
        where, params = host_filter()
        total = db.query_one(f"SELECT COUNT(*) n FROM hosts h{where}", params)["n"]
        rows = db.query(f"SELECT h.* FROM hosts h{where} ORDER BY h.name COLLATE NOCASE LIMIT ? OFFSET ?",
                        params + [size, offset])
        return jsonify(hosts=rows, total=total, page=page, size=size)

    @app.route("/api/hosts/options")
    @login_required
    def api_host_options():
        return jsonify(hosts=db.query("SELECT id, name, ip FROM hosts ORDER BY name COLLATE NOCASE"))

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

    @app.route("/api/events")
    @login_required
    def api_events():
        page, size, offset = page_args()
        params, where = [], []
        if request.args.get("host_id", type=int):
            where.append("e.host_id = ?")
            params.append(request.args.get("host_id", type=int))
        if request.args.get("type") in ("up", "down"):
            where.append("e.type = ?")
            params.append(request.args["type"])
        cond = (" WHERE " + " AND ".join(where)) if where else ""
        total = db.query_one(f"SELECT COUNT(*) n FROM events e{cond}", params)["n"]
        rows = db.query("SELECT e.*, h.name, h.ip FROM events e LEFT JOIN hosts h ON h.id = e.host_id"
                        f"{cond} ORDER BY e.id DESC LIMIT ? OFFSET ?", params + [size, offset])
        return jsonify(events=[fmt_event(e) for e in rows], total=total, page=page, size=size)

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

    def build_report(paged=False):
        start, end, bucket, host_id, d_from, d_to = report_params()
        size = 3600 if bucket == "hour" else 86400
        off = tz_offset()
        scope, params = [], []
        if host_id:
            scope.append("id = ?")
            params.append(host_id)
        status = request.args.get("status")
        if status in ("up", "down", "warning"):
            scope.append("enabled = 1 AND status = ?")
            params.append(status)
        sub = f"SELECT id FROM hosts WHERE {' AND '.join(scope)}" if scope else ""
        hfilter = f" AND c.host_id IN ({sub})" if sub else ""
        rows = db.query(
            f"SELECT ((c.ts + ?) / {size}) * {size} - ? AS b, COUNT(*) n, SUM(c.received > 0) ok, "
            "SUM(c.sent) sent, SUM(c.received) recv, AVG(c.rtt_avg) rtt FROM checks c "
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
            nxt = datetime.fromtimestamp(t) + timedelta(seconds=size)
            t = int(time.mktime(nxt.timetuple()))
        agg = {"checks": sum(x["n"] for x in rows), "ok": sum(x["ok"] or 0 for x in rows),
               "sent": sum(x["sent"] or 0 for x in rows), "recv": sum(x["recv"] or 0 for x in rows)}
        ev_where = " WHERE e.ts >= ? AND e.ts < ?" + (f" AND e.host_id IN ({sub})" if sub else "")
        ev_params = [start, end] + params
        agg["down_events"] = db.query_one(f"SELECT COUNT(*) n FROM events e{ev_where} AND e.type = 'down'",
                                          ev_params)["n"]

        interval = db.get_settings()["interval_seconds"]
        h_where = f" WHERE h.id IN ({sub})" if sub else ""
        t_total = db.query_one(f"SELECT COUNT(*) n FROM hosts h{h_where}", params)["n"]
        t_limit = ""
        t_page = e_page = 1
        psize = 20
        if paged:
            psize = min(max(5, request.args.get("size", 20, type=int)), 200)
            t_page = max(1, request.args.get("tpage", 1, type=int))
            e_page = max(1, request.args.get("epage", 1, type=int))
            t_limit = f" LIMIT {psize} OFFSET {(t_page - 1) * psize}"
        summary = db.query(
            "SELECT h.id, h.name, h.ip, COUNT(c.id) n, SUM(c.received > 0) ok, SUM(c.sent) sent, "
            "SUM(c.received) recv, AVG(c.rtt_avg) rtt, MIN(c.rtt_avg) rtt_min, MAX(c.rtt_avg) rtt_max "
            "FROM hosts h LEFT JOIN checks c ON c.host_id = h.id AND c.ts >= ? AND c.ts < ?"
            f"{h_where} GROUP BY h.id ORDER BY h.name COLLATE NOCASE{t_limit}", [start, end] + params)
        ids = [x["id"] for x in summary]
        downs = {}
        if ids:
            downs = {r["host_id"]: r["n"] for r in db.query(
                "SELECT host_id, COUNT(*) n FROM events WHERE type='down' AND ts >= ? AND ts < ? "
                f"AND host_id IN ({','.join('?' * len(ids))}) GROUP BY host_id", [start, end] + ids)}
        table = []
        for x in summary:
            n, ok = x["n"] or 0, x["ok"] or 0
            table.append({
                "id": x["id"], "name": x["name"], "ip": x["ip"],
                "checks": n, "ok": ok, "fail": n - ok,
                "avail": pct(ok, n), "packet": pct(x["recv"], x["sent"]),
                "rtt": round(x["rtt"], 1) if x["rtt"] is not None else None,
                "rtt_min": x["rtt_min"], "rtt_max": x["rtt_max"],
                "down_events": downs.get(x["id"], 0),
                "downtime_text": fmt_dur((n - ok) * interval),
            })
        e_total = db.query_one(f"SELECT COUNT(*) n FROM events e{ev_where}", ev_params)["n"]
        e_limit = f" LIMIT {psize} OFFSET {(e_page - 1) * psize}" if paged else ""
        events = db.query("SELECT e.*, h.name, h.ip FROM events e LEFT JOIN hosts h ON h.id = e.host_id"
                          f"{ev_where} ORDER BY e.ts DESC{e_limit}", ev_params)
        return {
            "from": d_from.strftime("%Y-%m-%d"), "to": d_to.strftime("%Y-%m-%d"),
            "bucket": bucket, "host_id": host_id, "status": status, "size": psize,
            "series": series,
            "totals": dict(agg, avail=pct(agg["ok"], agg["checks"]), packet=pct(agg["recv"], agg["sent"])),
            "table": table, "table_total": t_total, "table_page": t_page,
            "events": [fmt_event(e) for e in events], "events_total": e_total, "events_page": e_page,
        }

    @app.route("/api/report")
    @login_required
    def api_report():
        return jsonify(build_report(paged=True))

    @app.route("/api/report.csv")
    @login_required
    def api_report_csv():
        rep = build_report()
        buf = io.StringIO()
        buf.write("﻿")
        writer = csv.writer(buf)

        def put(row):
            writer.writerow(["'" + v if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else v for v in row])

        put([f"Báo cáo kết nối từ {rep['from']} đến {rep['to']}"])
        put([])
        put(["Tên", "Địa chỉ", "Số lần kiểm tra", "Thành công", "Thất bại",
             "Tỷ lệ kết nối (%)", "Tỷ lệ gói nhận (%)", "RTT TB (ms)", "RTT min", "RTT max",
             "Số lần mất kết nối", "Thời gian mất kết nối"])
        for r in rep["table"]:
            put([r["name"], r["ip"], r["checks"], r["ok"], r["fail"], r["avail"], r["packet"],
                 r["rtt"], r["rtt_min"], r["rtt_max"], r["down_events"], r["downtime_text"]])
        put([])
        put(["Thời gian", "Số lần kiểm tra", "Thành công", "Thất bại", "Tỷ lệ kết nối (%)",
             "Tỷ lệ gói nhận (%)", "RTT TB (ms)"])
        for s in rep["series"]:
            put([s["label"], s["checks"], s["ok"], s["fail"], s["avail"], s["packet"], s["rtt"]])
        put([])
        put(["Thời điểm", "Loại", "Địa chỉ", "Nội dung"])
        for e in rep["events"]:
            put([e["time"], "Mất kết nối" if e["type"] == "down" else "Phục hồi", e["ip"], e["message"]])
        fname = f"baocao_{rep['from']}_{rep['to']}.csv"
        return Response(buf.getvalue(), mimetype="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f"attachment; filename={fname}"})

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

    @app.route("/api/settings")
    @login_required
    def api_settings():
        return jsonify(settings=db.get_settings(), dashboard_title=db.get_text("dashboard_title"))

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
        if "dashboard_title" in d:
            title = str(d["dashboard_title"] or "").strip()
            if len(title) > 200:
                return jsonify(error="Tên hệ thống tối đa 200 ký tự"), 400
            values["dashboard_title"] = title
        db.set_settings(values)
        log.info("%s cập nhật cấu hình %s", g.user["username"], values)
        return jsonify(settings=db.get_settings(), dashboard_title=db.get_text("dashboard_title"))

    @app.route("/favicon.ico")
    def favicon():
        return app.send_static_file("favicon.ico")

    @app.route("/ca.crt")
    def ca_cert():
        return Response(certs.ca_pem(), mimetype="application/x-x509-ca-cert",
                        headers={"Content-Disposition": "attachment; filename=PingDiagnose-CA.crt"})

    @app.route("/install-ca.bat")
    def install_ca():
        return Response(certs.install_ca_bat(), mimetype="application/octet-stream",
                        headers={"Content-Disposition": "attachment; filename=PingDiagnose-cai-chung-chi.bat"})

    @app.route("/api/cert")
    @login_required
    def api_cert():
        cfg = load_config()
        if not cfg.get("https"):
            return jsonify(https=False)
        custom = bool(cfg.get("cert_file") and cfg.get("key_file"))
        path = cfg["cert_file"] if custom else os.path.join(data_dir(), certs.SRV_CRT)
        try:
            info = certs.cert_info(path)
        except (OSError, ValueError):
            info = {"issuer": "?", "names": [], "expires": "?", "days_left": None}
        return jsonify(https=True, custom=custom, **info)

    @app.route("/api/cert/renew", methods=["POST"])
    @admin_required
    def api_cert_renew():
        cfg = load_config()
        if not cfg.get("https"):
            return jsonify(error="HTTPS đang tắt"), 400
        if cfg.get("cert_file") and cfg.get("key_file"):
            return jsonify(error="Đang dùng chứng chỉ riêng, hãy nhập chứng chỉ mới bằng lệnh cert --import"), 400
        ca_new = certs.renew_server_cert(cfg.get("extra_names") or [])
        reload = app.config.get("CERT_RELOAD")
        if reload:
            reload()
        log.info("%s cấp lại chứng chỉ HTTPS", g.user["username"])
        return jsonify(ca_new=ca_new, **certs.cert_info(os.path.join(data_dir(), certs.SRV_CRT)))

    return app


def pct(a, b):
    if not b:
        return None
    return round(100.0 * (a or 0) / b, 2)


def tz_offset():
    return time.localtime().tm_gmtoff or 0


def fmt_dur(sec):
    sec = int(sec or 0)
    if sec <= 0:
        return "0 phút"
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
