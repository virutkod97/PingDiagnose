import base64
import hashlib
import hmac
import json
import os
import struct
import tempfile

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@pytest.fixture()
def data(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("PINGDIAGNOSE_DATA", d)
    from pingdiagnose import db, webpush
    db._local.__dict__.clear()
    webpush._key = None
    db.init_db()
    yield d
    db._local.__dict__.clear()
    webpush._key = None


def b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def hkdf(salt, ikm, info, n):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:n]


def test_encrypt_roundtrip(data):
    from pingdiagnose.webpush import b64u, encrypt
    ua = ec.generate_private_key(ec.SECP256R1())
    ua_pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    auth = os.urandom(16)
    msg = json.dumps({"title": "⚠ Mất kết nối"}).encode()
    body = encrypt(msg, b64u(ua_pub), b64u(auth))
    salt, (rs, idlen) = body[:16], struct.unpack("!IB", body[16:21])
    as_pub = body[21:21 + idlen]
    assert rs == 4096 and idlen == 65
    shared = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
    ikm = hkdf(auth, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    cek = hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, body[21 + idlen:], None)
    assert plain == msg + b"\x02"


def test_vapid_header(data):
    from pingdiagnose.webpush import public_key, vapid_header
    h = vapid_header("https://fcm.googleapis.com/fcm/send/abc")
    t = h.split("t=")[1].split(",")[0]
    k = h.split("k=")[1]
    assert k == public_key()
    head, claims, sig = t.split(".")
    c = json.loads(b64d(claims))
    assert c["aud"] == "https://fcm.googleapis.com" and c["sub"].startswith("https://")
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64d(k))
    raw = b64d(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_allowed_endpoint():
    from pingdiagnose.webpush import allowed_endpoint
    assert allowed_endpoint("https://fcm.googleapis.com/fcm/send/x")
    assert allowed_endpoint("https://wns2-sg2p.notify.windows.com/w/?token=x")
    assert allowed_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert not allowed_endpoint("https://evil.com/fcm.googleapis.com")
    assert not allowed_endpoint("http://fcm.googleapis.com/x")
    assert not allowed_endpoint("https://fcm.googleapis.com:8443/x")
    assert not allowed_endpoint("https://192.168.1.1/")


def test_server_cert_chain(data):
    from pingdiagnose import certs
    crt, key = certs.ensure_server_cert(["ping.congty.local", "10.1.2.3"])
    pem = open(crt, "rb").read()
    leaf, ca = x509.load_pem_x509_certificates(pem)
    assert leaf.issuer == ca.subject
    ca.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(leaf.signature_hash_algorithm))
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "ping.congty.local" in san.get_values_for_type(x509.DNSName)
    assert "10.1.2.3" in [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    assert "127.0.0.1" in [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    mtime = os.path.getmtime(crt)
    assert certs.ensure_server_cert(["ping.congty.local", "10.1.2.3"])[0] == crt
    assert os.path.getmtime(crt) == mtime
    bat = certs.install_ca_bat()
    assert "certutil -user -addstore Root" in bat and "BEGIN CERTIFICATE" in bat


def test_push_api(data):
    import re
    from pingdiagnose import db
    from pingdiagnose.web import create_app
    app = create_app()
    c = app.test_client()
    tok = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
    c.post("/login", data={"username": "admin", "password": "admin", "csrf_token": tok})
    db.execute("UPDATE users SET must_change = 0")
    tok = re.search(r'name="csrf-token" content="([^"]+)"', c.get("/").get_data(as_text=True)).group(1)
    h = {"X-CSRF-Token": tok}
    assert len(b64d(c.get("/api/push/public-key").get_json()["publicKey"])) == 65
    good = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "x", "auth": "y"}}
    bad = {"endpoint": "https://10.0.0.1/", "keys": {"p256dh": "x", "auth": "y"}}
    assert c.post("/api/push/subscribe", json={"subscription": bad}, headers=h).status_code == 400
    assert c.post("/api/push/subscribe", json={"subscription": good}, headers=h).status_code == 200
    assert c.post("/api/push/subscribe", json={"subscription": good}, headers=h).status_code == 200
    assert len(c.get("/api/push/devices").get_json()["devices"]) == 1
    new = dict(good, endpoint="https://fcm.googleapis.com/fcm/send/new")
    r = app.test_client().post("/api/push/resubscribe", json={"oldEndpoint": good["endpoint"], "subscription": new})
    assert r.status_code == 200
    assert [x["endpoint"] for x in db.query("SELECT endpoint FROM push_subs")] == [new["endpoint"]]
    assert app.test_client().post("/api/push/resubscribe", json={"oldEndpoint": "nope", "subscription": new}).status_code == 404
    assert c.get("/sw.js").status_code == 200
    assert b"BEGIN CERTIFICATE" in app.test_client().get("/ca.crt").data


def test_notify_groups(data, monkeypatch):
    from pingdiagnose import webpush
    sent = []
    monkeypatch.setattr(webpush, "send", lambda m, user_id=None: sent.append(m) or [])
    webpush.notify_events([("down", "a", "A"), ("down", "b", "B"), ("up", "c", "C")])
    assert len(sent) == 3
    sent.clear()
    webpush.notify_events([("down", str(i), f"H{i}") for i in range(5)])
    assert len(sent) == 1 and "5 địa chỉ" in sent[0]["title"]


def test_send_to_push_service(data, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pingdiagnose import db, webpush
    from pingdiagnose.webpush import b64u
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["headers"] = dict(self.headers)
            got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(201 if "ok" in self.path else 410)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(webpush, "allowed_endpoint", lambda u: True)
    monkeypatch.setattr(webpush, "_opener", lambda: __import__("urllib.request").request.build_opener(
        __import__("urllib.request").request.ProxyHandler({})))
    ua = ec.generate_private_key(ec.SECP256R1())
    pub = b64u(ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    base = f"http://127.0.0.1:{srv.server_port}"
    for ep in ("/ok", "/gone"):
        db.execute("INSERT INTO push_subs(user_id, endpoint, p256dh, auth, created_at) VALUES (1,?,?,?,0)",
                   (base + ep, pub, b64u(os.urandom(16))))
    res = webpush.send({"title": "t", "body": "b"})
    srv.shutdown()
    assert sorted(r["ok"] for r in res) == [False, True]
    assert got["headers"]["Content-Encoding"] == "aes128gcm"
    assert got["headers"]["Authorization"].startswith("vapid t=")
    assert [r["endpoint"] for r in db.query("SELECT endpoint FROM push_subs")] == [base + "/ok"]
    assert db.query_one("SELECT last_ok FROM push_subs")["last_ok"]
