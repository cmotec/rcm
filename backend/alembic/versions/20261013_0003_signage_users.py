"""Signage users: an account limited to one connection, shown fullscreen

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain SQL with explicit names (the metadata naming convention), see 0002.
FK = "fk_users_signage_connection_id_connections"


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_signage boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS signage_connection_id uuid")
    op.execute(
        f"ALTER TABLE users ADD CONSTRAINT {FK} FOREIGN KEY (signage_connection_id) "
        "REFERENCES connections (id) ON DELETE SET NULL"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE users DROP CONSTRAINT IF EXISTS {FK}")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS signage_connection_id")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS is_signage")
