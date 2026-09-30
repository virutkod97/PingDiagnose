import datetime
import ipaddress
import logging
import os
import socket

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .config import data_dir

log = logging.getLogger("pingdiagnose.certs")

CA_CRT = "ca.crt"
CA_KEY = "ca.key"
SRV_CRT = "server.crt"
SRV_KEY = "server.key"


def _path(name):
    return os.path.join(data_dir(), name)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _write_key(path, key):
    with open(path, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))


def _write_cert(path, cert):
    with open(path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))


def _load(name):
    with open(_path(name), "rb") as f:
        return f.read()


def local_names():
    names = {"localhost", socket.gethostname()}
    try:
        names.add(socket.getfqdn())
    except OSError:
        pass
    ips = {"127.0.0.1"}
    for host in (socket.gethostname(), socket.getfqdn()):
        try:
            for info in socket.getaddrinfo(host, None, socket.AF_INET):
                ips.add(info[4][0])
        except OSError:
            pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return {n for n in names if n}, ips


def ensure_ca():
    if os.path.exists(_path(CA_CRT)) and os.path.exists(_path(CA_KEY)):
        return
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, f"PingDiagnose CA ({socket.gethostname()})"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "PingDiagnose"),
    ])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(days=1))
            .not_valid_after(_now() + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(ski, critical=False)
            .sign(key, hashes.SHA256()))
    _write_key(_path(CA_KEY), key)
    _write_cert(_path(CA_CRT), cert)
    log.info("Đã tạo CA nội bộ %s", _path(CA_CRT))


def _server_ok(names, ips):
    try:
        cert = x509.load_pem_x509_certificate(_load(SRV_CRT))
        ca = x509.load_pem_x509_certificate(_load(CA_CRT))
    except (OSError, ValueError):
        return False
    if cert.issuer != ca.subject or cert.not_valid_after_utc - _now() < datetime.timedelta(days=30):
        return False
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    have_dns = {n.lower() for n in san.get_values_for_type(x509.DNSName)}
    have_ip = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
    return {n.lower() for n in names} <= have_dns and ips <= have_ip


def ensure_server_cert(extra_names=()):
    ensure_ca()
    names, ips = local_names()
    for n in extra_names:
        n = n.strip()
        if not n:
            continue
        try:
            ips.add(str(ipaddress.ip_address(n)))
        except ValueError:
            names.add(n)
    if os.path.exists(_path(SRV_KEY)) and _server_ok(names, ips):
        return _path(SRV_CRT), _path(SRV_KEY)
    ca_key = serialization.load_pem_private_key(_load(CA_KEY), None)
    ca = x509.load_pem_x509_certificate(_load(CA_CRT))
    key = ec.generate_private_key(ec.SECP256R1())
    alt = [x509.DNSName(n) for n in sorted(names)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, socket.gethostname())]))
            .issuer_name(ca.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - datetime.timedelta(days=1))
            .not_valid_after(_now() + datetime.timedelta(days=800))
            .add_extension(x509.SubjectAlternativeName(alt), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False, key_cert_sign=False,
                                         crl_sign=False, content_commitment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False),
                           critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    _write_key(_path(SRV_KEY), key)
    with open(_path(SRV_CRT), "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM) + ca.public_bytes(serialization.Encoding.PEM))
    log.info("Đã cấp chứng chỉ HTTPS cho: %s", ", ".join(sorted(names) + sorted(ips)))
    return _path(SRV_CRT), _path(SRV_KEY)


def ca_pem():
    ensure_ca()
    return _load(CA_CRT).decode()


def install_ca_bat():
    lines = [l for l in ca_pem().strip().splitlines()]
    echo = "\r\n".join(f"echo {l}" for l in lines)
    return (
        "@echo off\r\n"
        "set \"F=%TEMP%\\pingdiagnose-ca.crt\"\r\n"
        f"(\r\n{echo}\r\n) > \"%F%\"\r\n"
        "certutil -user -addstore Root \"%F%\"\r\n"
        "if errorlevel 1 (echo Cai chung chi that bai.) else (echo Da cai chung chi. Dong het trinh duyet roi mo lai.)\r\n"
        "del \"%F%\" >nul 2>&1\r\n"
        "pause\r\n"
    )


def import_cert(src, key_file=None, password=None):
    from cryptography.hazmat.primitives.serialization import pkcs12

    with open(src, "rb") as f:
        raw = f.read()
    if key_file:
        with open(key_file, "rb") as f:
            key = serialization.load_pem_private_key(f.read(), password.encode() if password else None)
        chain = x509.load_pem_x509_certificates(raw)
    elif src.lower().endswith((".pfx", ".p12")):
        key, leaf, extra = pkcs12.load_key_and_certificates(raw, password.encode() if password else None)
        chain = [leaf] + list(extra or [])
    else:
        key = serialization.load_pem_private_key(raw, password.encode() if password else None)
        chain = x509.load_pem_x509_certificates(raw)
    pub = serialization.PublicFormat.SubjectPublicKeyInfo
    leaf = next((c for c in chain if c.public_key().public_bytes(serialization.Encoding.DER, pub)
                 == key.public_key().public_bytes(serialization.Encoding.DER, pub)), None)
    if leaf is None:
        raise ValueError("Khoá bí mật không khớp với chứng chỉ")
    chain = [leaf] + [c for c in chain if c is not leaf]
    crt, kf = _path("custom.crt"), _path("custom.key")
    _write_key(kf, key)
    with open(crt, "wb") as f:
        f.write(b"".join(c.public_bytes(serialization.Encoding.PEM) for c in chain))
    return crt, kf, leaf


def cert_info(path):
    leaf = x509.load_pem_x509_certificates(open(path, "rb").read())[0]
    try:
        san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = san.get_values_for_type(x509.DNSName) + [str(i) for i in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    cn = leaf.issuer.get_attributes_for_oid(NameOID.COMMON_NAME)
    return {
        "issuer": cn[0].value if cn else leaf.issuer.rfc4514_string(),
        "names": names,
        "expires": leaf.not_valid_after_utc.astimezone().strftime("%d/%m/%Y"),
    }
