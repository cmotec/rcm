"""Encryption at rest for secret material (AES-256-GCM).

Ciphertext layout (bytes)::

    0x01 | len(kid) (1 byte) | kid (ascii) | nonce (12) | ciphertext+tag

Additional authenticated data (AAD) binds a ciphertext to its location
(``table.column.row_id``), so a ciphertext copied into another row or column
fails to decrypt. Keys are identified by a key ID so they can be rotated:
new data is written with the active key, and old keys stay readable until
``app.cli rotate-keys`` has re-encrypted everything.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

FORMAT_VERSION = 1


class CryptoError(Exception):
    pass


@dataclass(frozen=True)
class Keyring:
    keys: dict[str, bytes]
    active_kid: str

    @classmethod
    def parse(cls, spec: str, active: str | None = None) -> Keyring:
        keys: dict[str, bytes] = {}
        for part in (p.strip() for p in spec.split(",") if p.strip()):
            if ":" not in part:
                raise CryptoError("ENCRYPTION_KEYS entries must be '<key-id>:<base64 key>'")
            kid, b64 = part.split(":", 1)
            kid = kid.strip()
            if not kid or len(kid) > 64 or not kid.isascii():
                raise CryptoError(f"invalid key id {kid!r}")
            try:
                raw = base64.b64decode(b64.strip(), validate=True)
            except ValueError as exc:
                raise CryptoError(f"key {kid!r} is not valid base64") from exc
            if len(raw) != 32:
                raise CryptoError(f"key {kid!r} must be 32 bytes (got {len(raw)})")
            keys[kid] = raw
        if not keys:
            raise CryptoError("ENCRYPTION_KEYS is empty; generate one with `python -m app.cli generate-key`")
        active_kid = active or next(reversed(keys))
        if active_kid not in keys:
            raise CryptoError(f"ENCRYPTION_ACTIVE_KEY_ID {active_kid!r} is not in ENCRYPTION_KEYS")
        return cls(keys=keys, active_kid=active_kid)

    def derive(self, purpose: str, length: int = 32) -> bytes:
        """Derive a sub-key (e.g. for signing state cookies) from the active key."""
        return HKDF(
            algorithm=hashes.SHA256(), length=length, salt=None, info=purpose.encode()
        ).derive(self.keys[self.active_kid])


def aad_for(table: str, column: str, row_id: object) -> bytes:
    return f"{table}.{column}.{row_id}".encode()


class SecretBox:
    def __init__(self, keyring: Keyring) -> None:
        self.keyring = keyring

    def encrypt(self, plaintext: str | bytes | None, aad: bytes) -> bytes | None:
        if plaintext is None or plaintext == "" or plaintext == b"":
            return None
        data = plaintext.encode("utf-8") if isinstance(plaintext, str) else plaintext
        kid = self.keyring.active_kid
        nonce = os.urandom(12)
        ct = AESGCM(self.keyring.keys[kid]).encrypt(nonce, data, aad)
        kid_b = kid.encode("ascii")
        return bytes([FORMAT_VERSION, len(kid_b)]) + kid_b + nonce + ct

    def decrypt_bytes(self, blob: bytes | None, aad: bytes) -> bytes | None:
        if blob is None:
            return None
        blob = bytes(blob)
        if len(blob) < 2 or blob[0] != FORMAT_VERSION:
            raise CryptoError("unknown ciphertext format")
        kid_len = blob[1]
        kid = blob[2 : 2 + kid_len].decode("ascii")
        key = self.keyring.keys.get(kid)
        if key is None:
            raise CryptoError(f"ciphertext uses unknown key id {kid!r}")
        offset = 2 + kid_len
        nonce, ct = blob[offset : offset + 12], blob[offset + 12 :]
        try:
            return AESGCM(key).decrypt(nonce, ct, aad)
        except Exception as exc:  # cryptography raises InvalidTag
            raise CryptoError("decryption failed (wrong key or tampered data)") from exc

    def decrypt(self, blob: bytes | None, aad: bytes) -> str | None:
        raw = self.decrypt_bytes(blob, aad)
        return None if raw is None else raw.decode("utf-8")

    def key_id_of(self, blob: bytes | None) -> str | None:
        if not blob:
            return None
        blob = bytes(blob)
        return blob[2 : 2 + blob[1]].decode("ascii")

    def needs_rotation(self, blob: bytes | None) -> bool:
        kid = self.key_id_of(blob)
        return kid is not None and kid != self.keyring.active_kid


def generate_key() -> str:
    return base64.b64encode(os.urandom(32)).decode()


def token_hash(token: str) -> str:
    """Hash for bearer-style tokens stored in the DB (high-entropy input, so plain SHA-256 is fine)."""
    return hashlib.sha256(token.encode()).hexdigest()


def fingerprint(value: str) -> str:
    """Non-reversible, keyless fingerprint used for change detection only."""
    return hashlib.sha256(value.encode()).hexdigest()


class Signer:
    """HMAC signer for short-lived values (OIDC state cookie)."""

    def __init__(self, key: bytes) -> None:
        self._key = key

    def sign(self, payload: bytes) -> str:
        mac = hmac.new(self._key, payload, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(payload).decode().rstrip("=") + "." + base64.urlsafe_b64encode(
            mac
        ).decode().rstrip("=")

    def unsign(self, value: str) -> bytes | None:
        try:
            p64, m64 = value.split(".", 1)
            payload = base64.urlsafe_b64decode(p64 + "=" * (-len(p64) % 4))
            mac = base64.urlsafe_b64decode(m64 + "=" * (-len(m64) % 4))
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self._key, payload, hashlib.sha256).digest()
        return payload if hmac.compare_digest(mac, expected) else None


_box: SecretBox | None = None


def init_crypto(spec: str, active: str | None) -> SecretBox:
    global _box
    _box = SecretBox(Keyring.parse(spec, active))
    return _box


def get_box() -> SecretBox:
    if _box is None:
        raise CryptoError("encryption not initialised")
    return _box
