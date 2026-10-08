import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db
from .pinger import ping

log = logging.getLogger("pingdiagnose.monitor")

RAW_DAYS = 14


def fmt_duration(sec):
    d, rem = divmod(int(sec), 86400)
    h, rem = divmod(rem, 3600)
    parts = [f"{d} ngày"] if d else []
    if h:
        parts.append(f"{h} giờ")
    parts.append(f"{rem // 60} phút")
    return " ".join(parts)


HOURLY_UPSERT = (
    "INSERT INTO checks_hourly(host_id, hour, n, ok, sent, recv, rtt_sum, rtt_cnt, rtt_min, rtt_max) "
    "VALUES (?,?,1,?,?,?,?,?,?,?) ON CONFLICT(host_id, hour) DO UPDATE SET "
    "n = n + 1, ok = ok + excluded.ok, sent = sent + excluded.sent, recv = recv + excluded.recv, "
    "rtt_sum = rtt_sum + excluded.rtt_sum, rtt_cnt = rtt_cnt + excluded.rtt_cnt, "
    "rtt_min = COALESCE(MIN(rtt_min, excluded.rtt_min), rtt_min, excluded.rtt_min), "
    "rtt_max = COALESCE(MAX(rtt_max, excluded.rtt_max), rtt_max, excluded.rtt_max)")


def apply_result(conn, host, result, threshold, now):
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
    changed = status == "unknown" or (new_status == "up") != (status == "up")
    rtt = result.rtt_avg
    conn.execute("INSERT INTO checks(host_id, ts, sent, received, rtt_avg) VALUES (?,?,?,?,?)",
                 (host["id"], now, result.sent, result.received, rtt))
    conn.execute(HOURLY_UPSERT, (host["id"], now // 3600 * 3600, 1 if result.received > 0 else 0, result.sent,
                                 result.received, rtt or 0, 1 if rtt is not None else 0, rtt, rtt))
    conn.execute("UPDATE hosts SET status=?, consecutive_fail=?, last_check=?, last_rtt=?, "
                 "last_change=CASE WHEN ? THEN ? ELSE last_change END WHERE id=?",
                 (new_status, fails, now, rtt, 1 if changed else 0, now, host["id"]))
    if event:
        conn.execute("INSERT INTO events(host_id, ts, type, message) VALUES (?,?,?,?)",
                     (host["id"], now, event[0], event[1]))
    return new_status, event


def process_result(host, result, threshold, now=None):
    now = now or int(time.time())
    with db.tx() as conn:
        new_status, event = apply_result(conn, host, result, threshold, now)
    if event:
        log.warning(event[1])
    return new_status, event


class Monitor:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
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
        hosts = db.query("SELECT id, ip FROM hosts WHERE enabled = 1")
        if not hosts:
            return
        count, timeout, threshold = max(1, s["ping_count"]), max(100, s["ping_timeout_ms"]), max(1, s["fail_threshold"])
        now = int(time.time())
        with ThreadPoolExecutor(max_workers=min(128, len(hosts))) as ex:
            results = list(ex.map(lambda h: ping(h["ip"], count, timeout), hosts))
        events = []
        with db.tx() as conn:
            current = {r["id"]: r for r in conn.execute("SELECT * FROM hosts WHERE enabled = 1")}
            for host, res in zip(hosts, results):
                cur = current.get(host["id"])
                if cur is None:
                    continue
                try:
                    _, ev = apply_result(conn, cur, res, threshold, now)
                    if ev:
                        events.append(ev[1])
                except Exception:
                    log.exception("Lỗi ghi kết quả %s", host["ip"])
        for msg in events:
            log.warning(msg)
        log.info("Hoàn tất chu kỳ: %d địa chỉ trong %.1f giây", len(hosts), time.time() - now)

    def cleanup(self):
        days = db.get_settings()["retention_days"]
        now = int(time.time())
        raw_days = min(days, RAW_DAYS) if days > 0 else RAW_DAYS
        with db.tx() as conn:
            if db.get_text("hourly_done") == "1":
                conn.execute("DELETE FROM checks WHERE ts < ?", (now - raw_days * 86400,))
            if days > 0:
                conn.execute("DELETE FROM checks_hourly WHERE hour < ?", (now - days * 86400,))
                conn.execute("DELETE FROM events WHERE ts < ?", (now - days * 86400,))

    def _run(self):
        log.info("Bắt đầu giám sát")
        while not self._stop.is_set():
            start = time.time()
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
            while not self._stop.wait(min(5, max(0, self.next_cycle - time.time()))):
                if time.time() >= self.next_cycle:
                    break
                try:
                    self.next_cycle = int(start + max(30, db.get_settings()["interval_seconds"]))
                except Exception:
                    pass
        log.info("Dừng giám sát")
