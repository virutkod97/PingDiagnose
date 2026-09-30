"""Vòng lặp giám sát: định kỳ ping tất cả địa chỉ và sinh cảnh báo."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import db
from .pinger import ping

log = logging.getLogger("pingdiagnose.monitor")


def process_result(host, result, threshold, now=None):
    """Ghi kết quả một lần kiểm tra và cập nhật trạng thái/cảnh báo.

    Trạng thái:
      up       - có phản hồi
      warning  - mất phản hồi nhưng chưa đủ `threshold` chu kỳ liên tiếp
      down     - mất phản hồi >= `threshold` chu kỳ liên tiếp (đã cảnh báo)
    """
    now = now or int(time.time())
    ok = result.received > 0
    status = host["status"]
    fails = host["consecutive_fail"]
    event = None

    if ok:
        if status == "down":
            dur = now - (host["last_change"] or now)
            event = ("up", f"{host['name']} ({host['ip']}) đã phản hồi trở lại sau {fmt_duration(dur)}")
        fails = 0
        new_status = "up"
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
        conn.execute(
            "INSERT INTO checks(host_id, ts, sent, received, rtt_avg) VALUES (?,?,?,?,?)",
            (host["id"], now, result.sent, result.received, result.rtt_avg),
        )
        conn.execute(
            "UPDATE hosts SET status=?, consecutive_fail=?, last_check=?, last_rtt=?, "
            "last_change=CASE WHEN ? THEN ? ELSE last_change END WHERE id=?",
            (new_status, fails, now, result.rtt_avg, 1 if changed else 0, now, host["id"]),
        )
        if event:
            conn.execute(
                "INSERT INTO events(host_id, ts, type, message) VALUES (?,?,?,?)",
                (host["id"], now, event[0], event[1]),
            )
    if event:
        log.warning(event[1])
    return new_status, event


def fmt_duration(sec):
    sec = int(sec)
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    parts = []
    if d:
        parts.append(f"{d} ngày")
    if h:
        parts.append(f"{h} giờ")
    parts.append(f"{m} phút")
    return " ".join(parts)


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
        settings = db.get_settings()
        hosts = db.query("SELECT * FROM hosts WHERE enabled = 1")
        if not hosts:
            return
        count = max(1, settings["ping_count"])
        timeout = max(100, settings["ping_timeout_ms"])
        threshold = max(1, settings["fail_threshold"])
        now = int(time.time())
        with ThreadPoolExecutor(max_workers=min(64, len(hosts))) as ex:
            results = list(ex.map(lambda h: ping(h["ip"], count, timeout), hosts))
        for host, res in zip(hosts, results):
            try:
                # đọc lại trạng thái mới nhất (tránh ghi đè khi host vừa bị sửa/xoá)
                cur = db.query_one("SELECT * FROM hosts WHERE id = ?", (host["id"],))
                if cur and cur["enabled"]:
                    process_result(cur, res, threshold, now)
            except Exception:
                log.exception("Lỗi khi ghi kết quả cho %s", host["ip"])
        log.info("Hoàn tất chu kỳ: %d địa chỉ", len(hosts))

    def cleanup(self):
        days = db.get_settings()["retention_days"]
        if days <= 0:
            return
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
                log.exception("Lỗi trong chu kỳ giám sát")
            if start - self._last_cleanup > 86400:
                try:
                    self.cleanup()
                    self._last_cleanup = start
                except Exception:
                    log.exception("Lỗi khi dọn dữ liệu cũ")
            try:
                interval = max(30, db.get_settings()["interval_seconds"])
            except Exception:
                interval = 180
            self.next_cycle = int(start + interval)
            self._stop.wait(max(1, start + interval - time.time()))
        log.info("Dừng giám sát")
