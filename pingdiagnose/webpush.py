import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from . import db
from .config import data_dir, load_config

log = logging.getLogger("pingdiagnose.push")

PUSH_HOSTS = ("fcm.googleapis.com", "push.apple.com", "push.services.mozilla.com", "notify.windows.com")
DEFAULT_SUBJECT = "https://github.com/virutkod97/PingDiagnose"

_key = None


def b64u(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def vapid_key():
    global _key
    if _key is None:
        path = os.path.join(data_dir(), "vapid.pem")
        if os.path.exists(path):
            with open(path, "rb") as f:
                _key = serialization.load_pem_private_key(f.read(), None)
        else:
            _key = ec.generate_private_key(ec.SECP256R1())
            with open(path, "wb") as f:
                f.write(_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return _key


def public_key():
    return b64u(vapid_key().public_key().public_bytes(serialization.Encoding.X962,
                                                      serialization.PublicFormat.UncompressedPoint))


def allowed_endpoint(url):
    try:
        u = urlparse(url)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    return u.scheme == "https" and u.port is None and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


def vapid_header(endpoint):
    u = urlparse(endpoint)
    sub = load_config().get("vapid_subject") or DEFAULT_SUBJECT
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": f"{u.scheme}://{u.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": sub}).encode())
    unsigned = f"{head}.{claims}".encode()
    r, s = decode_dss_signature(vapid_key().sign(unsigned, ec.ECDSA(hashes.SHA256())))
    sig = b64u(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={unsigned.decode()}.{sig}, k={public_key()}"


def _hkdf(salt, ikm, info, length):
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()[:length]


def encrypt(payload, p256dh, auth):
    ua_pub = b64u_dec(p256dh)
    secret = b64u_dec(auth)
    peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub)
    eph = ec.generate_private_key(ec.SECP256R1())
    as_pub = eph.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = eph.exchange(ec.ECDH(), peer)
    ikm = _hkdf(secret, shared, b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_pub)) + as_pub + body


def _opener():
    proxy = load_config().get("push_proxy")
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener()


def send_one(sub, data, ttl=86400):
    body = encrypt(json.dumps(data, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"])
    req = urllib.request.Request(sub["endpoint"], data=body, method="POST", headers={
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(ttl),
        "Urgency": "high",
        "Authorization": vapid_header(sub["endpoint"]),
    })
    try:
        with _opener().open(req, timeout=15) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read(200).decode(errors="replace")
    except OSError as e:
        return 0, str(getattr(e, "reason", e))


def explain(status, text, endpoint):
    host = urlparse(endpoint).hostname
    if status in (404, 410):
        return f"{status}: thiết bị đã tắt thông báo hoặc gỡ đăng ký"
    if status in (401, 403):
        return f"{status}: dịch vụ push từ chối chữ ký VAPID ({text.strip()[:120]}). Kiểm tra đồng hồ máy chủ."
    if status == 0:
        return (f"Không kết nối được {host}:443 ({text}). Máy chủ cần ra Internet tới dịch vụ push, "
                "hoặc khai báo push_proxy trong config.json")
    return f"{status}: {text.strip()[:150]}"


def _deliver(sub, data):
    status, text = send_one(sub, data)
    now = int(time.time())
    if 200 <= status < 300:
        db.execute("UPDATE push_subs SET last_ok = ?, last_error = NULL WHERE id = ?", (now, sub["id"]))
        return {"id": sub["id"], "ok": True}
    err = explain(status, text, sub["endpoint"])
    if status in (404, 410):
        db.execute("DELETE FROM push_subs WHERE id = ?", (sub["id"],))
    else:
        db.execute("UPDATE push_subs SET last_error = ?, last_error_at = ? WHERE id = ?", (err, now, sub["id"]))
    log.warning("Gửi push thất bại (%s): %s", sub["user_agent"][:60], err)
    return {"id": sub["id"], "ok": False, "error": err}


def send(data, user_id=None):
    if user_id is None:
        subs = db.query("SELECT * FROM push_subs")
    else:
        subs = db.query("SELECT * FROM push_subs WHERE user_id = ?", (user_id,))
    subs = [s for s in subs if allowed_endpoint(s["endpoint"])]
    if not subs:
        return []
    with ThreadPoolExecutor(max_workers=min(16, len(subs))) as ex:
        return list(ex.map(lambda s: _deliver(s, data), subs))


def notify_events(events):
    msgs = []
    for kind, title in (("down", "⚠ Mất kết nối"), ("up", "✅ Đã phục hồi")):
        group = [e for e in events if e[0] == kind]
        if len(group) <= 3:
            msgs += [{"title": f"{title}: {e[2]}", "body": e[1], "tag": f"pd-ev-{e[3]}", "url": "/"} for e in group]
        else:
            msgs.append({"title": f"{title}: {len(group)} địa chỉ", "tag": f"pd-many-{group[0][3]}", "url": "/",
                         "body": ", ".join(e[2] for e in group[:10]) + ("..." if len(group) > 10 else "")})
    for m in msgs:
        try:
            send(m)
        except Exception:
            log.exception("Lỗi gửi push")


def check_connectivity():
    hosts = {"fcm.googleapis.com", "updates.push.services.mozilla.com", "web.push.apple.com"}
    hosts |= {urlparse(s["endpoint"]).hostname for s in db.query("SELECT endpoint FROM push_subs")}
    out = []
    for host in sorted(h for h in hosts if h):
        t0 = time.time()
        try:
            _opener().open(urllib.request.Request(f"https://{host}/", method="GET"), timeout=8).close()
            ok, err = True, None
        except urllib.error.HTTPError:
            ok, err = True, None
        except OSError as e:
            ok, err = False, str(getattr(e, "reason", e))
        out.append({"host": host, "ok": ok, "ms": int((time.time() - t0) * 1000), "error": err})
    return out
