"""Allow auth_mode 'credential' without a pinned credential (user chooses at connect time)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-12
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain SQL on purpose: op.drop_constraint() would apply the metadata naming convention a second
# time ("ck_connections_ck_connections_credential_ref").
CONSTRAINT = "ck_connections_credential_ref"


def upgrade() -> None:
    op.execute(f"ALTER TABLE connections DROP CONSTRAINT IF EXISTS {CONSTRAINT}")


def downgrade() -> None:
    # Connections that let the user choose fall back to inheriting credentials.
    op.execute("UPDATE connections SET auth_mode = 'inherit' WHERE auth_mode = 'credential' AND credential_id IS NULL")
    op.execute(
        f"ALTER TABLE connections ADD CONSTRAINT {CONSTRAINT} "
        "CHECK (auth_mode <> 'credential' OR credential_id IS NOT NULL)"
    )
