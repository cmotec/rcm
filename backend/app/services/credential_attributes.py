"""Credential attributes: values connections can take over through lookups.

Both kinds are stored encrypted (AES-256-GCM, bound to credential and column, like passwords):

* ``attributes``: shown to everyone who may view the credential (port, hostname, description, …);
* ``secret_attributes``: write-only like the password. Only sessions (lookups at launch) and the
  audited reveal read them; lists and details show their names, never their values.
"""

from __future__ import annotations

import json

from app import models as m
from app.security.crypto import SecretBox, aad_for, get_box


def _read(cred: m.Credential, column: str, box: SecretBox | None) -> dict[str, str]:
    blob = getattr(cred, f"{column}_enc")
    if not blob:
        return {}
    raw = (box or get_box()).decrypt(blob, aad_for("credentials", column, cred.id))
    data = json.loads(raw) if raw else {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write(cred: m.Credential, column: str, values: dict[str, str], box: SecretBox | None) -> None:
    clean = {k: v for k, v in values.items() if v not in (None, "")}
    setattr(cred, f"{column}_enc",
            (box or get_box()).encrypt(json.dumps(clean, sort_keys=True), aad_for("credentials", column, cred.id))
            if clean else None)


def get_attributes(cred: m.Credential, box: SecretBox | None = None) -> dict[str, str]:
    return _read(cred, "attributes", box)


def set_attributes(cred: m.Credential, values: dict[str, str], box: SecretBox | None = None) -> None:
    _write(cred, "attributes", values, box)


def get_secret_attributes(cred: m.Credential, box: SecretBox | None = None) -> dict[str, str]:
    return _read(cred, "secret_attributes", box)


def set_secret_attributes(cred: m.Credential, values: dict[str, str], box: SecretBox | None = None) -> None:
    _write(cred, "secret_attributes", values, box)


def secret_attribute_names(cred: m.Credential, box: SecretBox | None = None) -> list[str]:
    return sorted(get_secret_attributes(cred, box))
