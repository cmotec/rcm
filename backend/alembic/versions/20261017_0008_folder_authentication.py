"""Folder authentication: connections that inherit their login take it from their folder

* ``folders.auth_mode`` (inherit | direct | credential | interactive, like connections),
  ``username``, ``domain``, ``password_enc`` (AES-256-GCM), ``credential_id`` (pinned credential) and
  ``credential_folder_id`` (credential mode without a pinned credential: offer only this folder).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Plain SQL with explicit names (the metadata naming convention), see 0002.
def upgrade() -> None:
    op.execute("ALTER TABLE folders ADD COLUMN auth_mode varchar(16) NOT NULL DEFAULT 'inherit'")
    op.execute("ALTER TABLE folders ADD COLUMN username varchar(255)")
    op.execute("ALTER TABLE folders ADD COLUMN domain varchar(255)")
    op.execute("ALTER TABLE folders ADD COLUMN password_enc bytea")
    op.execute("ALTER TABLE folders ADD COLUMN credential_id uuid")
    op.execute("ALTER TABLE folders ADD COLUMN credential_folder_id uuid")
    op.execute("ALTER TABLE folders ADD CONSTRAINT ck_folders_auth_mode "
               "CHECK (auth_mode IN ('inherit', 'direct', 'credential', 'interactive'))")
    op.execute("ALTER TABLE folders ADD CONSTRAINT fk_folders_credential_id_credentials "
               "FOREIGN KEY (credential_id) REFERENCES credentials (id) ON DELETE RESTRICT")
    op.execute("ALTER TABLE folders ADD CONSTRAINT fk_folders_credential_folder_id_credential_folders "
               "FOREIGN KEY (credential_folder_id) REFERENCES credential_folders (id) ON DELETE SET NULL")
    op.execute("CREATE INDEX ix_folders_credential ON folders (credential_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_folders_credential")
    op.execute("ALTER TABLE folders DROP CONSTRAINT IF EXISTS fk_folders_credential_folder_id_credential_folders")
    op.execute("ALTER TABLE folders DROP CONSTRAINT IF EXISTS fk_folders_credential_id_credentials")
    op.execute("ALTER TABLE folders DROP CONSTRAINT IF EXISTS ck_folders_auth_mode")
    for column in ("credential_folder_id", "credential_id", "password_enc", "domain", "username", "auth_mode"):
        op.execute(f"ALTER TABLE folders DROP COLUMN IF EXISTS {column}")
