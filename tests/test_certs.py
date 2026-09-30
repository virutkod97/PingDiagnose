import datetime
import os
import tempfile

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID


@pytest.fixture()
def data(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("PINGDIAGNOSE_DATA", d)
    from pingdiagnose import db
    db._local.__dict__.clear()
    yield d
    db._local.__dict__.clear()


def test_server_cert_chain(data):
    from pingdiagnose import certs
    crt, key = certs.ensure_server_cert(["ping.congty.local", "10.1.2.3"])
    leaf, ca = x509.load_pem_x509_certificates(open(crt, "rb").read())
    assert leaf.issuer == ca.subject
    ca.public_key().verify(leaf.signature, leaf.tbs_certificate_bytes, ec.ECDSA(leaf.signature_hash_algorithm))
    info = certs.cert_info(crt)
    assert "ping.congty.local" in info["names"] and "10.1.2.3" in info["names"] and "127.0.0.1" in info["names"]
    mtime = os.path.getmtime(crt)
    certs.ensure_server_cert(["ping.congty.local", "10.1.2.3"])
    assert os.path.getmtime(crt) == mtime
    bat = certs.install_ca_bat()
    assert "certutil -user -addstore Root" in bat and "BEGIN CERTIFICATE" in bat


def make_cert():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "*.congty.vn")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("*.congty.vn")]), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def test_import_pfx_and_pem(data):
    from pingdiagnose import certs
    key, cert = make_cert()
    pfx = os.path.join(data, "c.pfx")
    open(pfx, "wb").write(pkcs12.serialize_key_and_certificates(
        b"x", key, cert, None, serialization.BestAvailableEncryption(b"secret")))
    crt, kf, leaf = certs.import_cert(pfx, password="secret")
    assert leaf.serial_number == 1 and certs.cert_info(crt)["names"] == ["*.congty.vn"]
    pem_c, pem_k = os.path.join(data, "c.pem"), os.path.join(data, "k.pem")
    open(pem_c, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(pem_k, "wb").write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    assert certs.import_cert(pem_c, pem_k)[2].serial_number == 1
    other, _ = make_cert()
    open(pem_k, "wb").write(other.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                serialization.NoEncryption()))
    with pytest.raises(ValueError):
        certs.import_cert(pem_c, pem_k)
