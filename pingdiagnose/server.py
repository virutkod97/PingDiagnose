import logging
import os
import ssl
import sys
import threading
from logging.handlers import RotatingFileHandler

from cheroot import wsgi
from cheroot.ssl.builtin import BuiltinSSLAdapter

from . import certs, db
from .config import APP_NAME, VERSION, data_dir, load_config
from .monitor import Monitor
from .web import create_app

log = logging.getLogger("pingdiagnose")


def setup_logging(console=False):
    logdir = os.path.join(data_dir(), "logs")
    os.makedirs(logdir, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handlers = [RotatingFileHandler(os.path.join(logdir, "pingdiagnose.log"), maxBytes=5_000_000,
                                    backupCount=5, encoding="utf-8")]
    if console:
        handlers.append(logging.StreamHandler())
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)


class LazyTLSAdapter(BuiltinSSLAdapter):
    def wrap(self, sock):
        s = self.context.wrap_socket(sock, do_handshake_on_connect=False, server_side=True)
        return s, {"wsgi.url_scheme": "https", "HTTPS": "on"}


class Server(wsgi.Server):
    def process_conn(self, conn):
        if not getattr(conn, "_ready", False):
            conn._ready = True
            self._connections.put(conn)
            return
        super().process_conn(conn)

    def error_log(self, msg="", level=logging.INFO, traceback=False):
        exc = sys.exc_info()[1] if traceback else None
        if isinstance(exc, (ssl.SSLError, OSError)) or "TLS" in msg or "HTTP into" in msg:
            log.debug("%s %s", msg, exc or "")
            return
        log.log(level if level >= logging.INFO else logging.INFO, msg, exc_info=traceback)


class AppServer:
    def __init__(self):
        self.monitor = None
        self.httpd = None
        self._stop = threading.Event()
        self._cert = None

    def start(self):
        cfg = load_config()
        db.init_db()
        self.monitor = Monitor()
        app = create_app()
        self.httpd = Server((cfg["host"], int(cfg["port"])), app, numthreads=32,
                                 server_name=f"{APP_NAME}/{VERSION}")
        scheme = "http"
        if cfg.get("https"):
            if cfg.get("cert_file") and cfg.get("key_file"):
                cert, key = cfg["cert_file"], cfg["key_file"]
            else:
                cert, key = certs.ensure_server_cert(cfg.get("extra_names") or [])
            self.httpd.ssl_adapter = LazyTLSAdapter(cert, key)
            self._cert = (cert, key)
            app.config["CERT_RELOAD"] = self.reload_cert
            if not (cfg.get("cert_file") and cfg.get("key_file")):
                threading.Thread(target=self._auto_renew, args=(cfg.get("extra_names") or [],),
                                 name="cert", daemon=True).start()
            scheme = "https"
        app.config["SESSION_COOKIE_SECURE"] = scheme == "https"
        self.httpd.prepare()
        self.monitor.start()
        threading.Thread(target=self._backfill, name="backfill", daemon=True).start()
        threading.Thread(target=self.httpd.serve, name="http", daemon=True).start()
        log.info("%s %s chạy tại %s://%s:%s (dữ liệu: %s)", APP_NAME, VERSION, scheme, cfg["host"],
                 cfg["port"], data_dir())
        return f"{scheme}://localhost:{cfg['port']}"

    def _backfill(self):
        try:
            n = db.backfill_hourly(self._stop)
            if n:
                log.info("Đã tổng hợp dữ liệu cũ theo giờ: %d ngày", n)
        except Exception:
            log.exception("Lỗi tổng hợp dữ liệu cũ")

    def reload_cert(self):
        self.httpd.ssl_adapter.context.load_cert_chain(*self._cert)
        log.info("Đã nạp lại chứng chỉ HTTPS")

    def _auto_renew(self, extra_names):
        while not self._stop.wait(12 * 3600):
            try:
                before = os.path.getmtime(self._cert[0])
                certs.ensure_server_cert(extra_names)
                if os.path.getmtime(self._cert[0]) != before:
                    self.reload_cert()
            except Exception:
                log.exception("Lỗi tự gia hạn chứng chỉ")

    def stop(self):
        log.info("Đang dừng...")
        self._stop.set()
        if self.httpd:
            self.httpd.stop()
        if self.monitor:
            self.monitor.stop()
