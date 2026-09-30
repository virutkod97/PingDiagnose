import logging
import os
import threading
from logging.handlers import RotatingFileHandler

from cheroot import wsgi

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


class AppServer:
    def __init__(self):
        self.monitor = None
        self.httpd = None

    def start(self):
        cfg = load_config()
        db.init_db()
        self.monitor = Monitor()
        app = create_app(self.monitor)
        self.httpd = wsgi.Server((cfg["host"], int(cfg["port"])), app, numthreads=16,
                                 server_name=f"{APP_NAME}/{VERSION}")
        scheme = "http"
        if cfg.get("https"):
            from cheroot.ssl.builtin import BuiltinSSLAdapter
            if cfg.get("cert_file") and cfg.get("key_file"):
                cert, key = cfg["cert_file"], cfg["key_file"]
            else:
                cert, key = certs.ensure_server_cert(cfg.get("extra_names") or [])
            self.httpd.ssl_adapter = BuiltinSSLAdapter(cert, key)
            scheme = "https"
        app.config["SESSION_COOKIE_SECURE"] = scheme == "https"
        self.httpd.prepare()
        self.monitor.start()
        threading.Thread(target=self.httpd.serve, name="http", daemon=True).start()
        log.info("%s %s chạy tại %s://%s:%s (dữ liệu: %s)", APP_NAME, VERSION, scheme, cfg["host"],
                 cfg["port"], data_dir())
        return f"{scheme}://localhost:{cfg['port']}"

    def stop(self):
        log.info("Đang dừng...")
        if self.httpd:
            self.httpd.stop()
        if self.monitor:
            self.monitor.stop()
