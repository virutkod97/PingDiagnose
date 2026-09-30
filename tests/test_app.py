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
    ev = client.get("/api/events?after=0").get_json()
    assert [e["type"] for e in ev["events"]] == ["down", "up"]
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


def test_stream(client):
    import json
    import threading
    tok = setup_admin(client)
    client.post("/api/hosts", json={"ip": "10.8.8.8", "name": "s"}, headers={"X-CSRF-Token": tok})
    from pingdiagnose import bus, db
    from pingdiagnose.monitor import process_result
    from pingdiagnose.pinger import PingResult
    r = client.get("/api/stream?after=0", buffered=False)
    assert r.mimetype == "text/event-stream"
    it = (c.decode() if isinstance(c, bytes) else c for c in r.response)
    assert next(it).startswith("retry:")
    assert "event: state" in next(it)

    def fire():
        time.sleep(0.3)
        for _ in range(3):
            process_result(db.query_one("SELECT * FROM hosts"), PingResult(sent=2, received=0), 3)
        bus.publish()
    threading.Thread(target=fire).start()
    msg = next(it)
    while "event: alert" not in msg:
        msg = next(it)
    data = json.loads(msg.split("data: ", 1)[1])
    assert data["type"] == "down" and data["ip"] == "10.8.8.8" and msg.startswith(f"id: {data['id']}")
    r.close()
