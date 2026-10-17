"""Remote sessions end to end: guacd tunnel (fake guacd), native SSH, SFTP and FTP (in-process servers)."""

from __future__ import annotations

import json
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app.services.sessions.guacd_protocol import Parser, encode
from tests.conftest import FTP_PASSWORD, FTP_USER, SSH_PASSWORD, SSH_USER, UserClient


def _instructions(text: str) -> list[list[str]]:
    return Parser().feed(text)


def _create(user: UserClient, body: dict) -> dict:
    r = user.post("/api/v1/connections", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- guacd (RDP)


def test_rdp_session_through_guacd_tunnel(admin: UserClient, make_user, fake_guacd) -> None:  # noqa: ANN001
    fake_guacd.reset()
    conn = _create(admin, {"name": "Win", "protocol": "rdp", "hostname": "127.0.0.1", "port": 3389,
                           "auth_mode": "direct", "username": "Administrator", "password": "rdp-pw",
                           "domain": "CORP", "settings": {"security": "nla", "cert_policy": "tofu"}})
    s = admin.post("/api/v1/sessions", json={"connection_id": conn["id"]}).json()
    assert s["kind"] == "guac" and s["ticket"] and s["tunnel_path"].endswith("/tunnel")
    assert [w["code"] for w in s["warnings"]] == ["cert_tofu"]
    assert "rdp-pw" not in json.dumps(s)

    url = f"{s['tunnel_path']}?ticket={s['ticket']}&width=1280&height=720&dpi=144&tz=Europe/Vienna"
    with admin.websocket(url, subprotocols=["guacamole"]) as ws:
        first = _instructions(ws.receive_text())
        assert first[0] == ["", s["id"]]  # internal "tunnel open" with the session UUID
        sync = _instructions(ws.receive_text())
        assert sync[0][0] == "sync"
        ws.send_text(encode("", "ping", "123"))
        assert _instructions(ws.receive_text()) == [["", "ping", "123"]]
        ws.send_text(encode("key", "65307", "1"))
        assert _instructions(ws.receive_text())[0][0] == "clipboard"
        state = admin.get(f"/api/v1/sessions/{s['id']}").json()["state"]
        assert state == "connected"

    received = {i[0]: i for i in fake_guacd.received}
    assert received["select"] == ["select", "rdp"]
    assert received["size"] == ["size", "1280", "720", "144"]
    assert received["timezone"] == ["timezone", "Europe/Vienna"]
    connect = received["connect"]
    values = dict(zip(fake_guacd.args, connect[2:], strict=True))
    assert connect[1] == "VERSION_1_5_0"
    assert values["hostname"] == "127.0.0.1" and values["port"] == "3389"
    assert values["username"] == "Administrator" and values["password"] == "rdp-pw" and values["domain"] == "CORP"
    assert values["security"] == "nla" and values["cert-tofu"] == "true" and values["ignore-cert"] == ""
    assert ["key", "65307", "1"] in fake_guacd.received
    assert not any(i[0] == "" for i in fake_guacd.received)  # internal opcodes never reach guacd

    # the ticket is single-use
    with pytest.raises(WebSocketDisconnect), admin.websocket(url, subprotocols=["guacamole"]) as ws:
        ws.receive_text()
    # reconnect issues a fresh ticket for the same session
    time.sleep(0.2)
    r = admin.post(f"/api/v1/sessions/{s['id']}/reconnect")
    assert r.status_code == 200, r.text
    again = r.json()
    assert again["ticket"] and again["ticket"] != s["ticket"] and again["state"] == "disconnected"

    # other users can neither see nor attach to the session
    other = make_user(["user"])
    assert other.get(f"/api/v1/sessions/{s['id']}").status_code == 404
    with pytest.raises(WebSocketDisconnect), other.websocket(
            f"{s['tunnel_path']}?ticket={again['ticket']}", subprotocols=["guacamole"]) as ws:
        ws.receive_text()
    assert admin.delete(f"/api/v1/sessions/{s['id']}").status_code == 204
    assert admin.get(f"/api/v1/sessions/{s['id']}").status_code == 404


def test_guac_tunnel_rejects_forbidden_opcodes(admin: UserClient, fake_guacd) -> None:  # noqa: ANN001
    fake_guacd.reset()
    conn = _create(admin, {"name": "VNC", "protocol": "vnc", "hostname": "127.0.0.1", "auth_mode": "direct",
                           "password": "vnc-pw"})
    s = admin.post("/api/v1/sessions", json={"connection_id": conn["id"]}).json()
    with admin.websocket(f"{s['tunnel_path']}?ticket={s['ticket']}", subprotocols=["guacamole"]) as ws:
        ws.receive_text()
        ws.receive_text()
        ws.send_text(encode("select", "ssh"))
        messages = []
        with pytest.raises(WebSocketDisconnect):
            while True:
                messages.append(ws.receive_text())
        assert any(i[0] == "error" for m in messages for i in _instructions(m))
    assert ["select", "ssh"] not in fake_guacd.received[1:]
    assert fake_guacd.received[0] == ["select", "vnc"]
    connect = next(i for i in fake_guacd.received if i[0] == "connect")
    assert "5900" in connect  # VNC default port 5900 + display 0


def test_guacd_error_is_reported(admin: UserClient, fake_guacd) -> None:  # noqa: ANN001
    fake_guacd.reset()
    fake_guacd.fail_with = "Upstream unreachable"
    conn = _create(admin, {"name": "Broken", "protocol": "rdp", "hostname": "127.0.0.1"})
    s = admin.post("/api/v1/sessions", json={"connection_id": conn["id"]}).json()
    with admin.websocket(f"{s['tunnel_path']}?ticket={s['ticket']}", subprotocols=["guacamole"]) as ws:
        msg = _instructions(ws.receive_text())
        assert msg[0][0] == "error" and "Upstream unreachable" in msg[0][1]
    time.sleep(0.2)
    got = admin.get(f"/api/v1/sessions/{s['id']}").json()
    assert got["state"] == "error" and "Upstream unreachable" in got["error"]
    fake_guacd.reset()


def test_ws_requires_same_origin(admin: UserClient) -> None:
    conn = _create(admin, {"name": "Origin", "protocol": "rdp", "hostname": "127.0.0.1"})
    s = admin.post("/api/v1/sessions", json={"connection_id": conn["id"]}).json()
    with pytest.raises(WebSocketDisconnect), admin.websocket(
            f"{s['tunnel_path']}?ticket={s['ticket']}", subprotocols=["guacamole"],
            headers={"origin": "https://evil.example"}) as ws:
        ws.receive_text()


# --------------------------------------------------------------------------- native SSH


@pytest.fixture(scope="module")
def ssh_conn(admin: UserClient, ssh_server) -> dict:  # noqa: ANN001
    host = _create(admin, {"name": "Linux box", "protocol": "host", "hostname": "127.0.0.1",
                           "auth_mode": "direct", "username": SSH_USER, "password": SSH_PASSWORD})
    ssh = _create(admin, {"name": "Linux SSH", "protocol": "ssh", "parent_id": host["id"], "port": ssh_server.port})
    return {"host": host, "ssh": ssh}


def test_ssh_host_key_confirmation_and_terminal(admin: UserClient, ssh_conn: dict, ssh_server) -> None:  # noqa: ANN001
    cid = ssh_conn["ssh"]["id"]
    r = admin.post("/api/v1/sessions", json={"connection_id": cid})
    assert r.status_code == 409 and r.json()["code"] == "hostkey_unknown"
    hk = r.json()["host_key"]
    assert hk["fingerprint"] == ssh_server.host_key_fingerprint and hk["port"] == ssh_server.port
    wrong = admin.post("/api/v1/sessions", json={"connection_id": cid, "accept_host_keys": [
        {"hostname": hk["hostname"], "port": hk["port"], "fingerprint": "SHA256:not-the-key"}]})
    assert wrong.status_code == 409
    r = admin.post("/api/v1/sessions", json={"connection_id": cid, "accept_host_keys": [
        {"hostname": hk["hostname"], "port": hk["port"], "fingerprint": hk["fingerprint"]}]})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["kind"] == "terminal" and s["auth_sources"]["password"]["kind"] == "connection"
    known = admin.get("/api/v1/known-hosts", params={"q": "127.0.0.1"}).json()
    assert any(k["fingerprint"] == hk["fingerprint"] for k in known["items"])

    with admin.websocket(f"{s['tunnel_path']}?ticket={s['ticket']}&cols=100&rows=30") as ws:
        assert json.loads(ws.receive_text()) == {"type": "status", "state": "connecting"}
        assert json.loads(ws.receive_text()) == {"type": "status", "state": "connected"}
        out = b""
        while b"size 100x30" not in out:
            out += ws.receive_bytes()
        assert f"welcome {SSH_USER}".encode() in out
        ws.send_bytes(b"hello world\n")
        while b"echo:hello world" not in out:
            out += ws.receive_bytes()
        ws.send_text(json.dumps({"type": "resize", "cols": 132, "rows": 40}))
        while b"resized 132x40" not in out:
            out += ws.receive_bytes()
        ws.send_bytes(b"exit\n")
        tail = []
        while True:
            msg = ws.receive()
            if msg["type"] == "websocket.close":
                break
            tail.append(msg)
        assert any(m.get("text") and json.loads(m["text"]).get("type") == "exit" for m in tail)
    assert "hello world" in ssh_server.commands
    # `exit` ends the session on the server too: nothing left to reconnect to
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and admin.post(f"/api/v1/sessions/{s['id']}/reconnect").status_code != 404:
        time.sleep(0.1)
    assert admin.post(f"/api/v1/sessions/{s['id']}/reconnect").status_code == 404


def test_ssh_wrong_password_reports_error(admin: UserClient, ssh_conn: dict, ssh_server) -> None:  # noqa: ANN001
    bad = _create(admin, {"name": "Bad pw", "protocol": "ssh", "hostname": "127.0.0.1", "port": ssh_server.port,
                          "auth_mode": "direct", "username": SSH_USER, "password": "nope", "inherit_auth": False})
    s = admin.post("/api/v1/sessions", json={"connection_id": bad["id"]}).json()  # host key already trusted
    with admin.websocket(f"{s['tunnel_path']}?ticket={s['ticket']}") as ws:
        assert json.loads(ws.receive_text())["state"] == "connecting"
        err = json.loads(ws.receive_text())
        assert err["type"] == "error" and err["code"] == "remote_auth_failed"


def test_interactive_credentials(admin: UserClient, ssh_server) -> None:  # noqa: ANN001
    conn = _create(admin, {"name": "Ask", "protocol": "ssh", "hostname": "127.0.0.1", "port": ssh_server.port,
                           "auth_mode": "interactive", "username": SSH_USER})
    r = admin.post("/api/v1/sessions", json={"connection_id": conn["id"]})
    assert r.status_code == 428 and r.json()["code"] == "credentials_required" and r.json()["username"] == SSH_USER
    r = admin.post("/api/v1/sessions", json={"connection_id": conn["id"], "credentials": {"password": SSH_PASSWORD}})
    assert r.status_code == 201
    s = r.json()
    assert s["auth_sources"]["password"]["kind"] == "interactive"
    with admin.websocket(f"{s['tunnel_path']}?ticket={s['ticket']}") as ws:
        ws.receive_text()
        assert json.loads(ws.receive_text())["state"] == "connected"
    # interactive secrets are never stored
    got = admin.get(f"/api/v1/connections/{conn['id']}").json()
    assert got["password_set"] is False


def test_personal_account(admin: UserClient, ssh_server) -> None:  # noqa: ANN001
    conn = _create(admin, {"name": "Mine", "protocol": "ssh", "hostname": "127.0.0.1", "port": ssh_server.port,
                           "auth_mode": "interactive"})
    body = {"connection_id": conn["id"], "auth_override": {"mode": "account", "account": "privileged"}}
    r = admin.post("/api/v1/sessions", json=body)
    assert r.status_code == 422 and "not set up" in r.text

    r = admin.put("/api/v1/me/accounts/privileged", json={"username": SSH_USER, "password": SSH_PASSWORD})
    assert r.status_code == 200 and r.json()["has_password"] is True and "password" not in r.json()
    accounts = {a["kind"]: a for a in admin.get("/api/v1/me/accounts").json()}
    assert accounts["privileged"]["username"] == SSH_USER and accounts["user"]["username"] is None
    # the password stays when only the username is changed
    r = admin.put("/api/v1/me/accounts/privileged", json={"username": SSH_USER})
    assert r.json()["has_password"] is True

    r = admin.post("/api/v1/sessions", json=body)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["auth_sources"]["password"]["kind"] == "account"
    with admin.websocket(f"{s['tunnel_path']}?ticket={s['ticket']}") as ws:
        ws.receive_text()
        assert json.loads(ws.receive_text())["state"] == "connected"

    assert admin.delete("/api/v1/me/accounts/privileged").status_code == 204
    assert all(a["username"] is None for a in admin.get("/api/v1/me/accounts").json())


# --------------------------------------------------------------------------- SFTP


def test_sftp_inherits_from_ssh_parent_and_manages_files(admin: UserClient, ssh_conn: dict, ssh_server) -> None:  # noqa: ANN001
    sftp = _create(admin, {"name": "Linux SFTP", "protocol": "sftp", "parent_id": ssh_conn["host"]["id"],
                           "port": ssh_server.port})
    r = admin.post("/api/v1/sessions", json={"connection_id": sftp["id"]})
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["kind"] == "files" and s["state"] == "connected" and s["ticket"] is None
    base = f"/api/v1/sessions/{s['id']}/fs"
    assert admin.get(f"{base}/home").json()["path"] == "/"
    names = {e["name"]: e for e in admin.get(f"{base}/list", params={"path": "/"}).json()["entries"]}
    assert names["hello.txt"]["type"] == "file" and names["docs"]["type"] == "dir"
    assert names["hello.txt"]["permissions"].startswith("-")

    payload = bytes(range(256)) * 4096  # 1 MiB
    r = admin.put(f"{base}/upload", params={"path": "/docs/data.bin"}, content=payload,
                  headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 201 and r.json()["bytes"] == len(payload)
    assert (ssh_server.root / "docs" / "data.bin").read_bytes() == payload
    r = admin.put(f"{base}/upload", params={"path": "/docs/data.bin"}, content=b"x")
    assert r.status_code == 409  # no overwrite without consent
    r = admin.get(f"{base}/download", params={"path": "/docs/data.bin"})
    assert r.status_code == 200 and r.content == payload and r.headers["content-length"] == str(len(payload))

    assert admin.post(f"{base}/mkdir", json={"path": "/docs/sub"}).status_code == 204
    assert admin.post(f"{base}/rename", json={"source": "/docs/data.bin", "target": "/docs/sub/moved.bin"}).status_code == 204
    assert admin.post(f"{base}/chmod", json={"path": "/docs/sub/moved.bin", "mode": 0o600}).status_code == 204
    st = admin.get(f"{base}/stat", params={"path": "/docs/sub/moved.bin"}).json()
    assert st["size"] == len(payload)
    assert admin.post(f"{base}/delete", json={"path": "/docs/sub"}).status_code in (400, 409)  # not empty
    assert admin.post(f"{base}/delete", json={"path": "/docs/sub", "recursive": True}).status_code == 204
    assert not (ssh_server.root / "docs" / "sub").exists()
    assert admin.get(f"{base}/list", params={"path": "/nope"}).status_code == 404
    transfers = admin.get(f"/api/v1/sessions/{s['id']}/transfers").json()
    assert {t["direction"] for t in transfers} == {"upload", "download"}
    assert admin.delete(f"/api/v1/sessions/{s['id']}").status_code == 204


# --------------------------------------------------------------------------- FTP


def test_ftp_file_manager(admin: UserClient, ftp_server) -> None:  # noqa: ANN001
    conn = _create(admin, {"name": "FTP", "protocol": "ftp", "hostname": "127.0.0.1", "port": ftp_server.port,
                           "auth_mode": "direct", "username": FTP_USER, "password": FTP_PASSWORD,
                           "settings": {"mode": "passive"}})
    s = admin.post("/api/v1/sessions", json={"connection_id": conn["id"]}).json()
    assert s["kind"] == "files" and [w["code"] for w in s["warnings"]] == ["unencrypted"]
    base = f"/api/v1/sessions/{s['id']}/fs"
    assert admin.get(f"{base}/home").json()["path"] == "/"
    names = {e["name"] for e in admin.get(f"{base}/list", params={"path": "/"}).json()["entries"]}
    assert "readme.txt" in names
    payload = b"0123456789" * 100_000
    r = admin.put(f"{base}/upload", params={"path": "/up.bin"}, content=payload)
    assert r.status_code == 201, r.text
    assert (ftp_server.root / "up.bin").read_bytes() == payload
    r = admin.get(f"{base}/download", params={"path": "/up.bin"})
    assert r.content == payload
    assert admin.post(f"{base}/mkdir", json={"path": "/dir1"}).status_code == 204
    assert admin.post(f"{base}/rename", json={"source": "/up.bin", "target": "/dir1/up2.bin"}).status_code == 204
    assert admin.post(f"{base}/chmod", json={"path": "/dir1/up2.bin", "mode": 0o644}).status_code == 501
    assert admin.post(f"{base}/delete", json={"path": "/dir1", "recursive": True}).status_code == 204
    assert not (ftp_server.root / "dir1").exists()
    assert admin.get(f"{base}/download", params={"path": "/missing.txt"}).status_code == 404
    # session keeps working after an error
    assert "readme.txt" in {e["name"] for e in admin.get(f"{base}/list", params={"path": "/"}).json()["entries"]}
    assert admin.delete(f"/api/v1/sessions/{s['id']}").status_code == 204


def test_wrong_ftp_password_fails_cleanly(admin: UserClient, ftp_server) -> None:  # noqa: ANN001
    conn = _create(admin, {"name": "FTP bad", "protocol": "ftp", "hostname": "127.0.0.1", "port": ftp_server.port,
                           "auth_mode": "direct", "username": FTP_USER, "password": "wrong"})
    r = admin.post("/api/v1/sessions", json={"connection_id": conn["id"]})
    assert r.status_code in (400, 401, 502) and "530" in r.json()["detail"]


def test_session_listing_and_disconnect_all(admin: UserClient, ftp_server) -> None:  # noqa: ANN001
    conn = _create(admin, {"name": "FTP list", "protocol": "ftp", "hostname": "127.0.0.1", "port": ftp_server.port,
                           "auth_mode": "direct", "username": FTP_USER, "password": FTP_PASSWORD})
    ids = {admin.post("/api/v1/sessions", json={"connection_id": conn["id"]}).json()["id"] for _ in range(2)}
    listed = {s["id"] for s in admin.get("/api/v1/sessions").json()}
    assert ids <= listed  # two independent sessions to the same connection
    assert admin.post("/api/v1/sessions/disconnect-all").json()["closed"] >= 2
    assert admin.get("/api/v1/sessions").json() == []
