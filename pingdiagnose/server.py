"""Khởi chạy web server (cheroot) + luồng giám sát."""
import datetime
import ipaddress
import logging
import os
import socket
import threading
from logging.handlers import RotatingFileHandler

from cheroot import wsgi

from . import db
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
    fh = RotatingFileHandler(os.path.join(logdir, "pingdiagnose.log"), maxBytes=5_000_000,
                             backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if console:
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        root.addHandler(sh)


def ensure_self_signed(cert, key):
    """Tạo chứng chỉ tự ký nếu bật HTTPS mà chưa có chứng chỉ."""
    if os.path.exists(cert) and os.path.exists(key):
        return
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    hostname = socket.gethostname()
    alt = [x509.DNSName(hostname), x509.DNSName("localhost"),
           x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        for info in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = ipaddress.ip_address(info[4][0])
            if x509.IPAddress(ip) not in alt:
                alt.append(x509.IPAddress(ip))
    except OSError:
        pass
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.datetime.now(datetime.timezone.utc)
    c = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(k.public_key())
         .serial_number(x509.random_serial_number())
         .not_valid_before(now - datetime.timedelta(days=1))
         .not_valid_after(now + datetime.timedelta(days=3650))
         .add_extension(x509.SubjectAlternativeName(alt), critical=False)
         .sign(k, hashes.SHA256()))
    with open(key, "wb") as f:
        f.write(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                serialization.NoEncryption()))
    with open(cert, "wb") as f:
        f.write(c.public_bytes(serialization.Encoding.PEM))
    log.info("Đã tạo chứng chỉ tự ký: %s", cert)


class AppServer:
    def __init__(self):
        self.monitor = None
        self.httpd = None
        self._thread = None

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
            cert = cfg.get("cert_file") or os.path.join(data_dir(), "server.crt")
            key = cfg.get("key_file") or os.path.join(data_dir(), "server.key")
            ensure_self_signed(cert, key)
            self.httpd.ssl_adapter = BuiltinSSLAdapter(cert, key)
            scheme = "https"
        app.config["SESSION_COOKIE_SECURE"] = scheme == "https"
        self.httpd.prepare()
        self.monitor.start()
        self._thread = threading.Thread(target=self.httpd.serve, name="http", daemon=True)
        self._thread.start()
        log.info("%s %s đang chạy tại %s://%s:%s (dữ liệu: %s)", APP_NAME, VERSION, scheme,
                 cfg["host"], cfg["port"], data_dir())
        return f"{scheme}://localhost:{cfg['port']}"

    def stop(self):
        log.info("Đang dừng...")
        if self.httpd:
            self.httpd.stop()
        if self.monitor:
            self.monitor.stop()

    def wait(self, stop_event):
        stop_event.wait()
