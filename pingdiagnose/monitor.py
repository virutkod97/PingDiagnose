import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import bus, db, webpush
from .pinger import ping

log = logging.getLogger("pingdiagnose.monitor")


def fmt_duration(sec):
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    parts = [f"{d} ngày"] if d else []
    if h:
        parts.append(f"{h} giờ")
    parts.append(f"{rem // 60} phút")
    return " ".join(parts)


def process_result(host, result, threshold, now=None):
    now = now or int(time.time())
    status, fails, event = host["status"], host["consecutive_fail"], None
    if result.received > 0:
        if status == "down":
            dur = fmt_duration(now - (host["last_change"] or now))
            event = ("up", f"{host['name']} ({host['ip']}) đã phản hồi trở lại sau {dur}")
        fails, new_status = 0, "up"
    else:
        fails += 1
        if fails >= threshold:
            new_status = "down"
            if status != "down":
                event = ("down", f"{host['name']} ({host['ip']}) không phản hồi {fails} chu kỳ liên tiếp")
        else:
            new_status = "down" if status == "down" else "warning"
    changed = (new_status == "down") != (status == "down") or status == "unknown"
    with db.tx() as conn:
        conn.execute("INSERT INTO checks(host_id, ts, sent, received, rtt_avg) VALUES (?,?,?,?,?)",
                     (host["id"], now, result.sent, result.received, result.rtt_avg))
        conn.execute("UPDATE hosts SET status=?, consecutive_fail=?, last_check=?, last_rtt=?, "
                     "last_change=CASE WHEN ? THEN ? ELSE last_change END WHERE id=?",
                     (new_status, fails, now, result.rtt_avg, 1 if changed else 0, now, host["id"]))
        if event:
            cur = conn.execute("INSERT INTO events(host_id, ts, type, message) VALUES (?,?,?,?)",
                               (host["id"], now, event[0], event[1]))
            event = (event[0], event[1], cur.lastrowid)
    if event:
        log.warning(event[1])
    return new_status, event


class Monitor:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self.last_cycle = None
        self.next_cycle = None
        self._last_cleanup = 0

    def start(self):
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)

    def run_cycle(self):
        s = db.get_settings()
        hosts = db.query("SELECT * FROM hosts WHERE enabled = 1")
        if not hosts:
            return
        count, timeout, threshold = max(1, s["ping_count"]), max(100, s["ping_timeout_ms"]), max(1, s["fail_threshold"])
        now = int(time.time())
        with ThreadPoolExecutor(max_workers=min(64, len(hosts))) as ex:
            results = list(ex.map(lambda h: ping(h["ip"], count, timeout), hosts))
        events = []
        for host, res in zip(hosts, results):
            try:
                cur = db.query_one("SELECT * FROM hosts WHERE id = ?", (host["id"],))
                if cur and cur["enabled"]:
                    _, ev = process_result(cur, res, threshold, now)
                    if ev:
                        events.append((ev[0], ev[1], f"{cur['name']} ({cur['ip']})", ev[2]))
            except Exception:
                log.exception("Lỗi ghi kết quả %s", host["ip"])
        if events:
            bus.publish()
            threading.Thread(target=webpush.notify_events, args=(events,), daemon=True).start()
        log.info("Hoàn tất chu kỳ: %d địa chỉ", len(hosts))

    def cleanup(self):
        days = db.get_settings()["retention_days"]
        if days > 0:
            cutoff = int(time.time()) - days * 86400
            with db.tx() as conn:
                conn.execute("DELETE FROM checks WHERE ts < ?", (cutoff,))
                conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))

    def _run(self):
        log.info("Bắt đầu giám sát")
        while not self._stop.is_set():
            start = time.time()
            self.last_cycle = int(start)
            try:
                self.run_cycle()
            except Exception:
                log.exception("Lỗi chu kỳ giám sát")
            if start - self._last_cleanup > 86400:
                try:
                    self.cleanup()
                    self._last_cleanup = start
                except Exception:
                    log.exception("Lỗi dọn dữ liệu")
            try:
                interval = max(30, db.get_settings()["interval_seconds"])
            except Exception:
                interval = 180
            self.next_cycle = int(start + interval)
            self._stop.wait(max(1, start + interval - time.time()))
        log.info("Dừng giám sát")
