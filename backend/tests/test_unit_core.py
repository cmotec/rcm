"""Unit tests without database or network."""

from __future__ import annotations

import base64
import os
import uuid

import pytest

from app.logging import REDACTED, redact_mapping, redact_value
from app.security.crypto import CryptoError, Keyring, SecretBox, Signer, aad_for
from app.services.sessions.files.ftp import parse_list_line
from app.services.sessions.guacd_protocol import GuacProtocolError, Parser, encode


def _key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


# --------------------------------------------------------------------------- crypto


def test_encrypt_roundtrip_and_aad_binding() -> None:
    box = SecretBox(Keyring.parse(f"k1:{_key()}"))
    rid = uuid.uuid4()
    blob = box.encrypt("s3cret-ä", aad_for("credentials", "password", rid))
    assert blob is not None and b"s3cret" not in blob
    assert box.decrypt(blob, aad_for("credentials", "password", rid)) == "s3cret-ä"
    with pytest.raises(CryptoError):
        box.decrypt(blob, aad_for("credentials", "password", uuid.uuid4()))  # moved to another row
    with pytest.raises(CryptoError):
        box.decrypt(blob, aad_for("credentials", "passphrase", rid))  # moved to another column


def test_empty_values_are_not_encrypted() -> None:
    box = SecretBox(Keyring.parse(f"k1:{_key()}"))
    assert box.encrypt(None, b"x") is None
    assert box.encrypt("", b"x") is None
    assert box.decrypt(None, b"x") is None


def test_key_rotation() -> None:
    k1, k2 = _key(), _key()
    old = SecretBox(Keyring.parse(f"k1:{k1}"))
    blob = old.encrypt("value", b"aad")
    both = SecretBox(Keyring.parse(f"k1:{k1},k2:{k2}", active="k2"))
    assert both.decrypt(blob, b"aad") == "value"
    assert both.needs_rotation(blob)
    rotated = both.encrypt(both.decrypt(blob, b"aad"), b"aad")
    assert both.key_id_of(rotated) == "k2" and not both.needs_rotation(rotated)
    with pytest.raises(CryptoError):
        SecretBox(Keyring.parse(f"k2:{k2}")).decrypt(blob, b"aad")


@pytest.mark.parametrize("spec", ["", "nokey", "k1:notbase64!!", f"k1:{base64.b64encode(b'short').decode()}"])
def test_invalid_keyring(spec: str) -> None:
    with pytest.raises(CryptoError):
        Keyring.parse(spec)


def test_signer_detects_tampering() -> None:
    s = Signer(os.urandom(32))
    token = s.sign(b'{"state":"abc"}')
    assert s.unsign(token) == b'{"state":"abc"}'
    payload, mac = token.split(".")
    assert s.unsign(payload + "x." + mac) is None
    assert s.unsign("garbage") is None


# --------------------------------------------------------------------------- guacamole protocol


def test_encode_counts_code_points() -> None:
    assert encode("size", 1024, 768) == "4.size,4.1024,3.768;"
    assert encode("clipboard", "ä😀") == "9.clipboard,2.ä😀;"
    assert encode("nop") == "3.nop;"


def test_parser_incremental_and_split_chunks() -> None:
    p = Parser()
    data = encode("sync", "123") + encode("img", "1", "image/png", "0", "0", "0") + encode("blob", "1", "ä;,.")
    out: list[list[str]] = []
    for ch in data:  # worst case: one character per feed
        out.extend(p.feed(ch))
    assert out == [["sync", "123"], ["img", "1", "image/png", "0", "0", "0"], ["blob", "1", "ä;,."]]
    assert p.pending == 0


def test_parser_feed_split_returns_whole_instructions_only() -> None:
    p = Parser()
    full = encode("a", "x") + encode("b", "y")
    chunk, _ = p.feed_split(full + "4.syn", collect=False)
    assert chunk == full
    chunk, instr = p.feed_split("c,1.1;")
    assert chunk == "4.sync,1.1;" and instr == [["sync", "1"]]


@pytest.mark.parametrize("bad", ["x.abc;", "3.abc!", "999999999999999.x"])
def test_parser_rejects_garbage(bad: str) -> None:
    with pytest.raises(GuacProtocolError):
        Parser().feed(bad)


# --------------------------------------------------------------------------- FTP LIST parsing


def test_parse_unix_list_lines() -> None:
    e = parse_list_line("/pub", "drwxr-xr-x    2 ftp      ftp          4096 Jan 15  2024 releases")
    assert e and e.type == "dir" and e.name == "releases" and e.path == "/pub/releases" and e.mode == 0o755
    e = parse_list_line("/", "-rw-r--r--    1 owner    group      123456 Mar  3 12:30 file name.txt")
    assert e and e.type == "file" and e.size == 123456 and e.name == "file name.txt" and e.owner == "owner"
    e = parse_list_line("/", "lrwxrwxrwx    1 root     root            7 Jan  1  2020 latest -> v1.2.3")
    assert e and e.type == "link" and e.name == "latest" and e.link_target == "v1.2.3"
    assert parse_list_line("/", "drwxr-xr-x 2 a b 0 Jan 1 2020 .") is None


def test_parse_dos_list_lines() -> None:
    e = parse_list_line("/", "01-15-24  10:30AM       <DIR>          Folder")
    assert e and e.type == "dir" and e.name == "Folder"
    e = parse_list_line("/", "01-15-24  10:30AM                 1024 data.bin")
    assert e and e.type == "file" and e.size == 1024


# --------------------------------------------------------------------------- transactions


def test_db_transactions_commit_before_the_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: with FastAPI's default "request" scope a yield-dependency exits *after* the
    response is sent, so a client could read before the commit (and a failing commit would go
    unreported). Every route must use get_db with scope="function"."""
    from fastapi.routing import APIRoute, APIWebSocketRoute

    from app.db import get_db
    from app.main import create_app

    monkeypatch.setenv("ENVIRONMENT", "test")
    from app.config import reset_settings_cache

    reset_settings_cache()
    app = create_app()
    offenders: list[str] = []

    def walk(dep, path: str) -> None:  # noqa: ANN001
        if dep.call is get_db and dep.scope != "function":
            offenders.append(path)
        for sub in dep.dependencies:
            walk(sub, path)

    for route in app.routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            walk(route.dependant, route.path)
    assert not offenders, f"get_db without scope='function' in: {sorted(set(offenders))}"


# --------------------------------------------------------------------------- redaction


def test_log_redaction() -> None:
    data = redact_mapping({"password": "p", "token": "t", "credential_id": "c-1", "hostname": "h",
                           "nested": {"private_key": "k", "ok": 1}, "password_set": True})
    assert data["password"] == REDACTED and data["token"] == REDACTED
    assert data["credential_id"] == "c-1" and data["hostname"] == "h" and data["password_set"] is True
    assert data["nested"]["private_key"] == REDACTED and data["nested"]["ok"] == 1
    assert redact_value("GET /x?ticket=abc&w=1") == f"GET /x?ticket={REDACTED}&w=1"
    assert "AAAA" not in redact_value("-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----")
    assert redact_value("Authorization: Bearer pat_abc.def") == f"Authorization: Bearer {REDACTED}"


# --------------------------------------------------------------------------- authorisation


def test_signage_account_only_connects_to_its_connection() -> None:
    from app.services.authz import ROOT_ID, AccessContext, Perm

    screen, other, folder = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    access = AccessContext(user_id=uuid.uuid4(), username="lobby", roles=["admin"], role_ids=set(), group_ids=set(),
                           permissions={Perm.ALL.value}, cap=4, signage=True, signage_connection_id=screen)
    access._acl = {("folder", ROOT_ID): 4}  # roles and ACL grants must not apply
    access._folders, access._connections, access._credentials = {}, {}, {}

    assert not access.is_admin and not access.has(Perm.API_ACCESS) and not access.has(Perm.CONNECTIONS_CREATE)
    assert access.can("connection", screen, "connect") and not access.can("connection", screen, "edit")
    assert not access.can("connection", other, "view")
    assert not access.can("folder", folder, "view") and not access.can("folder", None, "view")
    assert not access.can("credential", uuid.uuid4(), "view")


# --------------------------------------------------------------------------- SSH: private key first, password fallback


def test_unloadable_private_key_falls_back_to_the_password() -> None:
    import asyncssh

    from app.errors import ValidationFailed
    from app.services.sessions.launch import LaunchConfig
    from app.services.sessions.ssh_native import _client_keys

    def launch(private_key: str | None, password: str | None, passphrase: str | None = None) -> LaunchConfig:
        return LaunchConfig(connection_id=uuid.uuid4(), connection_name="x", protocol="ssh", host="h", port=22,
                            original_host="h", host_note=None, username="u", password=password, domain=None,
                            private_key=private_key, passphrase=passphrase, settings=None, auth_sources={},
                            warnings=[])

    key = asyncssh.generate_private_key("ssh-ed25519")
    encrypted = key.export_private_key("openssh", passphrase="right").decode()
    assert _client_keys(launch(None, "pw")) is None
    assert len(_client_keys(launch(encrypted, "pw", "right")) or []) == 1
    # wrong passphrase or garbage: the password is used instead ...
    assert _client_keys(launch(encrypted, "pw", "wrong")) is None
    assert _client_keys(launch("not a key", "pw")) is None
    # ... and without a password it is an error
    with pytest.raises(ValidationFailed):
        _client_keys(launch(encrypted, None, "wrong"))
