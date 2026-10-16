"""Automatic import of external secrets: key matching and the provider walk (no database)."""

from __future__ import annotations

import asyncio

from app.services.secrets.base import BrowseItem
from app.services.secrets.discovery import _walk, field_for_key, fields_for_keys


def test_keys_are_matched_to_credential_fields() -> None:
    assert field_for_key("Username") == "username"
    assert field_for_key("user_name") == "username"
    assert field_for_key("LOGIN") == "username"
    assert field_for_key("pwd") == "password"
    assert field_for_key("Kennwort") == "password"
    assert field_for_key("ssh-key") == "private_key"
    assert field_for_key("domain") == "domain"
    assert field_for_key("key passphrase") == "passphrase"
    assert field_for_key("api_url") is None


def test_fields_for_a_secret() -> None:
    assert fields_for_keys(["username", "password", "domain", "notes"]) == {
        "username": "username", "password": "password", "domain": "domain"}
    # the first key for a field wins
    assert fields_for_keys(["user", "username"]) == {"username": "user"}
    # a lone value (here a key without a name) is the password
    assert fields_for_keys([""]) == {"password": ""}
    assert fields_for_keys(["token"]) == {"password": "token"}
    # several unknown keys: nothing to import
    assert fields_for_keys(["url", "notes"]) == {}


class _FakeProvider:
    """KV tree: infrastructure/{web01 (secret), db/ (folder) → db/pg01 (secret)}."""

    tree = {
        "": [BrowseItem("web01", None, False), BrowseItem("db", None, True)],
        "web01": [BrowseItem("web01", "username", False), BrowseItem("web01", "password", False)],
        "db": [BrowseItem("db/pg01", None, False)],
        "db/pg01": [BrowseItem("db/pg01", "", False)],
    }

    async def browse(self, path: str) -> list[BrowseItem]:
        return self.tree.get(path.strip("/"), [])


def test_walk_finds_secrets_in_folders_with_their_keys() -> None:
    found = asyncio.run(_walk(_FakeProvider(), ""))  # type: ignore[arg-type]
    assert sorted(found) == [("db/pg01", [""]), ("web01", ["username", "password"])]
    # starting below the top level
    assert asyncio.run(_walk(_FakeProvider(), "/db/")) == [("db/pg01", [""])]  # type: ignore[arg-type]


def test_secret_name_is_the_username_until_the_secret_has_one() -> None:
    import uuid as _uuid

    from app import models as m
    from app.services.secrets.discovery import USERNAME_FROM_NAME, _username_from_secret_name

    stats: dict[str, int] = {}
    cred = m.Credential(id=_uuid.uuid4(), name="root", username=None, meta={}, version=1)
    # no username key: the secret's name is the username
    asyncio.run(_username_from_secret_name(cred, "root", False, False, stats))
    assert cred.username == "root" and cred.meta[USERNAME_FROM_NAME] and stats["usernames_from_name"] == 1
    # the secret gets a username key: the derived name steps aside, so the sync applies the key without a conflict
    asyncio.run(_username_from_secret_name(cred, "root", True, False, stats))
    assert cred.username is None and USERNAME_FROM_NAME not in cred.meta

    # a username typed by hand is never cleared
    edited = m.Credential(id=_uuid.uuid4(), name="web", username="admin", meta={USERNAME_FROM_NAME: True}, version=1)
    asyncio.run(_username_from_secret_name(edited, "web", True, False, stats))
    assert edited.username == "admin"
    # a credential that already has a username keeps it
    named = m.Credential(id=_uuid.uuid4(), name="db", username="postgres", meta={}, version=1)
    asyncio.run(_username_from_secret_name(named, "db", False, False, stats))
    assert named.username == "postgres" and USERNAME_FROM_NAME not in named.meta
