"""Remove the Infisical integration: OpenBao (Vault-compatible) is the only secret provider

Infisical providers are deleted together with their credential mappings and sync history
(ON DELETE CASCADE). The credentials themselves and the values already synchronised stay.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain SQL with the explicit constraint name (the metadata naming convention), see 0002.
CONSTRAINT = "ck_secret_providers_type"


def upgrade() -> None:
    op.execute("DELETE FROM secret_providers WHERE type = 'infisical'")
    op.execute(f"ALTER TABLE secret_providers DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(f"ALTER TABLE secret_providers ADD CONSTRAINT {CONSTRAINT} CHECK (type IN ('openbao'))")


def downgrade() -> None:
    op.execute(f"ALTER TABLE secret_providers DROP CONSTRAINT IF EXISTS {CONSTRAINT}")
    op.execute(f"ALTER TABLE secret_providers ADD CONSTRAINT {CONSTRAINT} CHECK (type IN ('infisical', 'openbao'))")
