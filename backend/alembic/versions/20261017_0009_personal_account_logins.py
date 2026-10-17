"""Connections and folders can log in with the connecting user's own account

* ``connections.personal_account`` / ``folders.personal_account`` (auth_mode ``credential``):
  ``any`` (the user picks one of their personal accounts), ``user`` or ``privileged``.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CHOICES = "'any', 'user', 'privileged'"


# Plain SQL with explicit names (the metadata naming convention), see 0002.
def upgrade() -> None:
    for table in ("connections", "folders"):
        op.execute(f"ALTER TABLE {table} ADD COLUMN personal_account varchar(16)")
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT ck_{table}_personal_account "
                   f"CHECK (personal_account IS NULL OR personal_account IN ({CHOICES}))")


def downgrade() -> None:
    for table in ("connections", "folders"):
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS ck_{table}_personal_account")
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS personal_account")
