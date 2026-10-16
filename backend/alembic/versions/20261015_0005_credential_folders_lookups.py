"""Credential folders, credential attributes, connection lookups

* ``credential_folders``: folder tree of the credential store; grants on a folder
  (ACL resource type ``cred_folder``) inherit to the folders and credentials below it.
* ``credentials.folder_id`` and ``credentials.attributes`` (non-secret values such as
  port or description that connections can take over through lookups).
* ``connections.lookups`` (field -> ``{{lookup:<name pattern>:<field>}}``) and
  ``connections.credential_folder_id`` (restricts "choose a credential at connect time").
* ``secret_mappings.field`` may also be ``attr:<name>`` (filled into a credential attribute).

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Plain SQL with explicit names (the metadata naming convention), see 0002.
SECRET_FIELDS = "'username', 'password', 'domain', 'private_key', 'passphrase'"


def upgrade() -> None:
    op.execute("""
        CREATE TABLE credential_folders (
            id uuid NOT NULL,
            name varchar(255) NOT NULL,
            parent_id uuid,
            description text,
            owner_id uuid,
            version integer NOT NULL,
            created_at timestamp with time zone NOT NULL DEFAULT now(),
            updated_at timestamp with time zone NOT NULL DEFAULT now(),
            CONSTRAINT pk_credential_folders PRIMARY KEY (id),
            CONSTRAINT ck_credential_folders_not_own_parent CHECK (parent_id IS NULL OR parent_id <> id),
            CONSTRAINT fk_credential_folders_parent_id_credential_folders FOREIGN KEY (parent_id)
                REFERENCES credential_folders (id) ON DELETE RESTRICT,
            CONSTRAINT fk_credential_folders_owner_id_users FOREIGN KEY (owner_id)
                REFERENCES users (id) ON DELETE SET NULL
        )
    """)
    op.execute("CREATE INDEX ix_credential_folders_parent ON credential_folders (parent_id)")

    op.execute("ALTER TABLE credentials ADD COLUMN folder_id uuid")
    op.execute("ALTER TABLE credentials ADD COLUMN attributes jsonb NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE credentials ADD CONSTRAINT fk_credentials_folder_id_credential_folders "
               "FOREIGN KEY (folder_id) REFERENCES credential_folders (id) ON DELETE RESTRICT")
    op.execute("CREATE INDEX ix_credentials_folder ON credentials (folder_id)")

    op.execute("ALTER TABLE connections ADD COLUMN lookups jsonb NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE connections ADD COLUMN credential_folder_id uuid")
    op.execute("ALTER TABLE connections ADD CONSTRAINT fk_connections_credential_folder_id_credential_folders "
               "FOREIGN KEY (credential_folder_id) REFERENCES credential_folders (id) ON DELETE SET NULL")

    op.execute("ALTER TABLE acl_entries DROP CONSTRAINT IF EXISTS ck_acl_entries_resource_type")
    op.execute("ALTER TABLE acl_entries ADD CONSTRAINT ck_acl_entries_resource_type "
               "CHECK (resource_type IN ('folder', 'connection', 'credential', 'cred_folder'))")

    op.execute("ALTER TABLE secret_mappings ALTER COLUMN field TYPE varchar(64)")
    op.execute("ALTER TABLE secret_mappings DROP CONSTRAINT IF EXISTS ck_secret_mappings_field")
    op.execute("ALTER TABLE secret_mappings ADD CONSTRAINT ck_secret_mappings_field "
               f"CHECK (field IN ({SECRET_FIELDS}) OR field LIKE 'attr:%')")


def downgrade() -> None:
    op.execute("DELETE FROM secret_mappings WHERE field LIKE 'attr:%'")
    op.execute("ALTER TABLE secret_mappings DROP CONSTRAINT IF EXISTS ck_secret_mappings_field")
    op.execute(f"ALTER TABLE secret_mappings ADD CONSTRAINT ck_secret_mappings_field CHECK (field IN ({SECRET_FIELDS}))")
    op.execute("ALTER TABLE secret_mappings ALTER COLUMN field TYPE varchar(16)")

    op.execute("DELETE FROM acl_entries WHERE resource_type = 'cred_folder'")
    op.execute("ALTER TABLE acl_entries DROP CONSTRAINT IF EXISTS ck_acl_entries_resource_type")
    op.execute("ALTER TABLE acl_entries ADD CONSTRAINT ck_acl_entries_resource_type "
               "CHECK (resource_type IN ('folder', 'connection', 'credential'))")

    op.execute("ALTER TABLE connections DROP CONSTRAINT IF EXISTS fk_connections_credential_folder_id_credential_folders")
    op.execute("ALTER TABLE connections DROP COLUMN IF EXISTS credential_folder_id")
    op.execute("ALTER TABLE connections DROP COLUMN IF EXISTS lookups")

    op.execute("DROP INDEX IF EXISTS ix_credentials_folder")
    op.execute("ALTER TABLE credentials DROP CONSTRAINT IF EXISTS fk_credentials_folder_id_credential_folders")
    op.execute("ALTER TABLE credentials DROP COLUMN IF EXISTS attributes")
    op.execute("ALTER TABLE credentials DROP COLUMN IF EXISTS folder_id")

    op.execute("DROP TABLE IF EXISTS credential_folders")
