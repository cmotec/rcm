"""Encrypt credential attributes; add write-only secret attributes

* ``credentials.attributes`` (plain JSONB) becomes ``attributes_enc`` (AES-256-GCM, encrypted with
  the application's ENCRYPTION_KEYS; existing values are converted here).
* ``credentials.secret_attributes_enc``: write-only values (like the password) for lookups.
* ``secret_mappings.field`` may also be ``secret:<name>``.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-16
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SECRET_FIELDS = "'username', 'password', 'domain', 'private_key', 'passphrase'"


def _box():  # noqa: ANN202
    from app.config import get_settings
    from app.security.crypto import init_crypto

    s = get_settings()
    return init_crypto(s.ENCRYPTION_KEYS, s.ENCRYPTION_ACTIVE_KEY_ID)


def upgrade() -> None:
    from app.security.crypto import aad_for

    op.execute("ALTER TABLE credentials ADD COLUMN IF NOT EXISTS attributes_enc bytea")
    op.execute("ALTER TABLE credentials ADD COLUMN IF NOT EXISTS secret_attributes_enc bytea")
    conn = op.get_bind()
    rows = conn.execute(text(
        "SELECT id, attributes::text FROM credentials WHERE attributes IS NOT NULL AND attributes::text <> '{}'"
    )).all()
    if rows:
        box = _box()
        for cid, raw in rows:
            values = json.loads(raw) if raw else {}
            if not values:
                continue
            blob = box.encrypt(json.dumps({str(k): str(v) for k, v in values.items()}, sort_keys=True),
                               aad_for("credentials", "attributes", cid))
            conn.execute(text("UPDATE credentials SET attributes_enc = :b WHERE id = :id"), {"b": blob, "id": cid})
    op.execute("ALTER TABLE credentials DROP COLUMN IF EXISTS attributes")

    op.execute("ALTER TABLE secret_mappings DROP CONSTRAINT IF EXISTS ck_secret_mappings_field")
    op.execute("ALTER TABLE secret_mappings ADD CONSTRAINT ck_secret_mappings_field "
               f"CHECK (field IN ({SECRET_FIELDS}) OR field LIKE 'attr:%' OR field LIKE 'secret:%')")


def downgrade() -> None:
    from app.security.crypto import aad_for

    op.execute("DELETE FROM secret_mappings WHERE field LIKE 'secret:%'")
    op.execute("ALTER TABLE secret_mappings DROP CONSTRAINT IF EXISTS ck_secret_mappings_field")
    op.execute("ALTER TABLE secret_mappings ADD CONSTRAINT ck_secret_mappings_field "
               f"CHECK (field IN ({SECRET_FIELDS}) OR field LIKE 'attr:%')")

    op.execute("ALTER TABLE credentials ADD COLUMN IF NOT EXISTS attributes jsonb NOT NULL DEFAULT '{}'")
    conn = op.get_bind()
    rows = conn.execute(text("SELECT id, attributes_enc FROM credentials WHERE attributes_enc IS NOT NULL")).all()
    if rows:
        box = _box()
        for cid, blob in rows:
            raw = box.decrypt(bytes(blob), aad_for("credentials", "attributes", cid))
            conn.execute(text("UPDATE credentials SET attributes = CAST(:v AS jsonb) WHERE id = :id"),
                         {"v": raw or "{}", "id": cid})
    # secret attributes have no plain-text place to go back to
    op.execute("ALTER TABLE credentials DROP COLUMN IF EXISTS secret_attributes_enc")
    op.execute("ALTER TABLE credentials DROP COLUMN IF EXISTS attributes_enc")
