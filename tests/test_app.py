import os
import re
import tempfile
import time

import pytest


@pytest.fixture()
def client(monkeypatch):
    d = tempfile.mkdtemp()
    monkeypatch.setenv("PINGDIAGNOSE_DATA", d)
    from pingdiagnose import db
    db._local.__dict__.clear()
    from pingdiagnose.web import create_app
    app = create_app()
    app.config["TESTING"] = True
    c = app.test_client()
    yield c
    db._local.__dict__.clear()


def csrf(c, path="/login"):
    html = c.get(path).get_data(as_text=True)
    return re.search(r'name="csrf-token" content="([^"]+)"|name="csrf_token" value="([^"]+)"', html).group(0).split('"')[-2]


def login(c, user="admin", pw="admin"):
    tok = csrf(c)
    return c.post("/login", data={"username": user, "password": pw, "csrf_token": tok})


def setup_admin(c):
    login(c)
    tok = csrf(c, "/account")
    r = c.post("/api/account/password", json={"old_password": "admin", "new_password": "secret123"},
               headers={"X-CSRF-Token": tok})
    assert r.status_code == 200, r.get_json()
    return tok


def test_login_and_force_change(client):
    assert client.get("/").status_code == 302
    r = login(client, pw="wrong")
    assert "Sai tên" in r.get_data(as_text=True)
    login(client)
    assert client.get("/").headers["Location"].endswith("/account")
    assert client.get("/api/dashboard").status_code == 403


def test_csrf_required(client):
    setup_admin(client)
    assert client.post("/api/hosts", json={"ip": "1.1.1.1"}).status_code == 403


def test_roles_and_hosts(client):
    tok = setup_admin(client)
    h = {"X-CSRF-Token": tok}
    assert client.post("/api/hosts", json={"ip": "127.0.0.1", "name": "lo"}, headers=h).status_code == 200
    assert client.post("/api/hosts", json={"ip": "127.0.0.1"}, headers=h).status_code == 400
    assert client.post("/api/hosts", json={"ip": "-c 5 x"}, headers=h).status_code == 400
    r = client.post("/api/hosts/import", json={"text": "10.0.0.1, A\nbad ip!\n10.0.0.2"}, headers=h).get_json()
    assert r["added"] == 2 and len(r["errors"]) == 1
    assert client.post("/api/users", json={"username": "view", "password": "viewer1", "role": "viewer"},
                       headers=h).status_code == 200
    client.post("/logout", data={"csrf_token": tok})
    login(client, "view", "viewer1")
    tok2 = csrf(client, "/")
    h2 = {"X-CSRF-Token": tok2}
    assert client.get("/api/dashboard").status_code == 200
    assert len(client.get("/api/hosts").get_json()["hosts"]) == 3
    assert client.post("/api/hosts", json={"ip": "8.8.8.8"}, headers=h2).status_code == 403
    assert client.put("/api/settings", json={"ping_count": 3}, headers=h2).status_code == 403
    assert client.get("/users").status_code == 403


def test_alert_after_three_cycles(client):
    tok = setup_admin(client)
    client.post("/api/hosts", json={"ip": "10.9.9.9", "name": "x"}, headers={"X-CSRF-Token": tok})
    from pingdiagnose import db
    from pingdiagnose.monitor import process_result
    from pingdiagnose.pinger import PingResult
    now = int(time.time()) - 3600
    fail = PingResult(sent=2, received=0)
    statuses = []
    for i in range(4):
        host = db.query_one("SELECT * FROM hosts WHERE ip='10.9.9.9'")
        st, ev = process_result(host, fail, 3, now + i * 180)
        statuses.append((st, ev and ev[0]))
    assert statuses == [("warning", None), ("warning", None), ("down", "down"), ("down", None)]
    host = db.query_one("SELECT * FROM hosts WHERE ip='10.9.9.9'")
    st, ev = process_result(host, PingResult(sent=2, received=2, rtts=[1.0, 3.0]), 3, now + 900)
    assert st == "up" and ev[0] == "up"
    ev = client.get("/api/events").get_json()
    assert [e["type"] for e in ev["events"]] == ["up", "down"]
    d = client.get("/api/dashboard").get_json()
    assert d["hosts"][0]["avail_24h"] == 20.0
    rep = client.get("/api/report").get_json()
    assert rep["totals"]["checks"] == 5 and rep["totals"]["avail"] == 20.0
    csv = client.get("/api/report.csv")
    assert csv.status_code == 200 and "10.9.9.9" in csv.get_data(as_text=True)
    for p in ("/", "/hosts", "/reports", "/events", "/users", "/settings", "/account"):
        assert client.get(p).status_code == 200, p


def test_ping_localhost():
    from pingdiagnose.pinger import ping, valid_target
    assert not valid_target("-n 1 1.1.1.1")
    assert not valid_target("1.1.1.1 & calc")
    assert valid_target("192.168.1.1") and valid_target("server-01.local") and valid_target("::1")
    r = ping("127.0.0.1", 2, 1000)
    assert r.sent == 2
    if os.environ.get("REQUIRE_PING"):
        assert r.received == 2, r.to_dict()



def test_pagination(client):
    tok = setup_admin(client)
    text = "\n".join(f"10.1.{i // 250}.{i % 250 + 1}, H{i:03d}" for i in range(45))
    assert client.post("/api/hosts/import", json={"text": text}, headers={"X-CSRF-Token": tok}).get_json()["added"] == 45
    d = client.get("/api/hosts?page=3&size=20").get_json()
    assert d["total"] == 45 and len(d["hosts"]) == 5 and d["hosts"][0]["name"] == "H040"
    d = client.get("/api/hosts?q=H01").get_json()
    assert d["total"] == 10
    d = client.get("/api/dashboard?page=2&size=20").get_json()
    assert d["total"] == 45 and len(d["hosts"]) == 20 and d["counts"]["total"] == 45
    assert client.get("/api/dashboard?status=down").get_json()["total"] == 0
    from pingdiagnose import db
    with db.tx() as conn:
        for i in range(30):
            conn.execute("INSERT INTO events(host_id, ts, type, message) VALUES (1, ?, 'down', 'x')", (int(time.time()) - i,))
    d = client.get("/api/events?page=2&size=20").get_json()
    assert d["total"] == 30 and len(d["events"]) == 10
    r = client.get("/api/report?tpage=3&epage=2&size=20").get_json()
    assert r["table_total"] == 45 and len(r["table"]) == 5 and r["events_total"] == 30 and len(r["events"]) == 10
    assert r["totals"]["down_events"] == 30
    assert len(client.get("/api/hosts/options").get_json()["hosts"]) == 45
    assert client.get("/api/report.csv").get_data(as_text=True).count("H0") >= 45


def test_title_status_filter_and_cert_renew(client):
    tok = setup_admin(client)
    h = {"X-CSRF-Token": tok}
    r = client.put("/api/settings", json={"dashboard_title": "Hệ thống giám sát IED", "interval_seconds": 180}, headers=h)
    assert r.status_code == 200 and r.get_json()["dashboard_title"] == "Hệ thống giám sát IED"
    assert r.get_json()["settings"]["interval_seconds"] == 180
    assert "Hệ thống giám sát IED" in client.get("/").get_data(as_text=True)
    client.post("/api/hosts/import", json={"text": "10.0.0.1, A\n10.0.0.2, B"}, headers=h)
    from pingdiagnose import db
    from pingdiagnose.monitor import process_result
    from pingdiagnose.pinger import PingResult
    now = int(time.time()) - 600
    for i in range(3):
        for ip, ok in (("10.0.0.1", 2), ("10.0.0.2", 0)):
            process_result(db.query_one("SELECT * FROM hosts WHERE ip = ?", (ip,)), PingResult(sent=2, received=ok), 3, now + i * 180)
    up = client.get("/api/report?status=up").get_json()
    down = client.get("/api/report?status=down").get_json()
    assert [x["ip"] for x in up["table"]] == ["10.0.0.1"] and up["totals"]["avail"] == 100.0
    assert [x["ip"] for x in down["table"]] == ["10.0.0.2"] and down["totals"]["avail"] == 0.0
    assert down["events_total"] == 1 and up["events_total"] == 0
    assert "10.0.0.1" not in client.get("/api/report.csv?status=down").get_data(as_text=True)

    from pingdiagnose import certs
    from pingdiagnose.config import data_dir
    certs.ensure_server_cert()
    old = open(os.path.join(data_dir(), "server.crt")).read()
    r = client.post("/api/cert/renew", headers=h).get_json()
    assert r["ca_new"] is False and r["days_left"] > 700
    assert open(os.path.join(data_dir(), "server.crt")).read() != old
    assert client.get("/api/cert").get_json()["days_left"] > 700
