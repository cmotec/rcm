"""Personal accounts: every user's own user account and privileged account (profile)

* ``personal_accounts``: username, domain and the encrypted password (AES-256-GCM, write-only) of
  the user's normal and privileged login; only the owner can open connections with them.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Plain SQL with explicit names (the metadata naming convention), see 0002.
def upgrade() -> None:
    op.execute("""
        CREATE TABLE personal_accounts (
            id uuid NOT NULL,
            user_id uuid NOT NULL,
            kind varchar(16) NOT NULL,
            username varchar(255),
            domain varchar(255),
            password_enc bytea,
            created_at timestamp with time zone NOT NULL DEFAULT now(),
            updated_at timestamp with time zone NOT NULL DEFAULT now(),
            CONSTRAINT pk_personal_accounts PRIMARY KEY (id),
            CONSTRAINT ck_personal_accounts_kind CHECK (kind IN ('user', 'privileged')),
            CONSTRAINT uq_personal_accounts_user_kind UNIQUE (user_id, kind),
            CONSTRAINT fk_personal_accounts_user_id_users FOREIGN KEY (user_id)
                REFERENCES users (id) ON DELETE CASCADE
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS personal_accounts")
