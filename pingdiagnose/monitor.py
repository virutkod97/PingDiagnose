import logging
import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db
from .pinger import PingResult, ping

log = logging.getLogger("pingdiagnose.monitor")

RAW_DAYS = 14
JITTER = 59
REFRESH = 10


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


def jitter_max(interval):
    return max(0, min(JITTER, interval - 1))


class Monitor:
    def __init__(self, workers=64):
        self._stop = threading.Event()
        self._thread = None
        self._pool = None
        self._workers = workers
        self._due = {}
        self._hosts = {}
        self._inflight = set()
        self._results = queue.Queue()
        self._settings = None
        self._refreshed = 0
        self._last_cleanup = 0
        self.submit = self._submit_pool

    def start(self):
        self._pool = ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="ping")
        self._thread = threading.Thread(target=self._run, name="monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)

    def _submit_pool(self, fn, *args):
        self._pool.submit(fn, *args)

    def _ping_task(self, hid, ip, count, timeout, started):
        try:
            res = ping(ip, count, timeout)
        except Exception as e:
            res = PingResult(sent=count, error=str(e))
        self._results.put((hid, started, res))

    def refresh(self, now):
        self._settings = db.get_settings()
        interval = max(30, self._settings["interval_seconds"])
        rows = db.query("SELECT id, ip, last_check FROM hosts WHERE enabled = 1")
        self._hosts = {r["id"]: r["ip"] for r in rows}
        for hid in list(self._due):
            if hid not in self._hosts:
                del self._due[hid]
        spread = min(interval, JITTER + 1)
        for r in rows:
            if r["id"] in self._due:
                continue
            if r["last_check"] and r["last_check"] + interval > now:
                self._due[r["id"]] = r["last_check"] + interval + random.uniform(0, jitter_max(interval))
            elif r["last_check"]:
                self._due[r["id"]] = now + random.uniform(0, spread)
            else:
                self._due[r["id"]] = now + random.uniform(0, 3)
        self._refreshed = now

    def dispatch(self, now):
        s = self._settings
        count, timeout = max(1, s["ping_count"]), max(100, s["ping_timeout_ms"])
        for hid, due in list(self._due.items()):
            if due <= now and hid not in self._inflight:
                self._inflight.add(hid)
                self.submit(self._ping_task, hid, self._hosts[hid], count, timeout, int(now))

    def collect(self):
        batch = []
        while True:
            try:
                batch.append(self._results.get_nowait())
            except queue.Empty:
                break
        if not batch:
            return 0
        s = self._settings
        interval = max(30, s["interval_seconds"])
        threshold = max(1, s["fail_threshold"])
        events = []
        with db.tx() as conn:
            for hid, started, res in batch:
                self._inflight.discard(hid)
                if hid in self._due:
                    self._due[hid] = started + interval + random.uniform(0, jitter_max(interval))
                cur = conn.execute("SELECT * FROM hosts WHERE id = ? AND enabled = 1", (hid,)).fetchone()
                if cur is None:
                    continue
                try:
                    _, ev = apply_result(conn, cur, res, threshold, started)
                    if ev:
                        events.append(ev[1])
                except Exception:
                    log.exception("Lỗi ghi kết quả host_id=%s", hid)
        for msg in events:
            log.warning(msg)
        return len(batch)

    def tick(self, now=None):
        now = now or time.time()
        if self._settings is None or now - self._refreshed >= REFRESH:
            self.refresh(now)
        self.dispatch(now)
        return self.collect()

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
        while not self._stop.wait(1):
            now = time.time()
            try:
                self.tick(now)
            except Exception:
                log.exception("Lỗi giám sát")
            if now - self._last_cleanup > 86400:
                try:
                    self.cleanup()
                    self._last_cleanup = now
                except Exception:
                    log.exception("Lỗi dọn dữ liệu")
        log.info("Dừng giám sát")
