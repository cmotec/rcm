"""Personal accounts: a user's own user account and privileged account, kept in their profile.

Only the owner can see (without the password) and use them, when opening a connection with
"My accounts". The password is write-only and encrypted like credential passwords.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m
from app.security.crypto import SecretBox, aad_for, get_box

LABELS = {"user": "your user account", "privileged": "your privileged account"}


async def get_account(db: AsyncSession, user_id: uuid.UUID, kind: str) -> m.PersonalAccount | None:
    return (await db.execute(select(m.PersonalAccount).where(
        m.PersonalAccount.user_id == user_id, m.PersonalAccount.kind == kind))).scalar_one_or_none()


async def list_accounts(db: AsyncSession, user_id: uuid.UUID) -> dict[str, m.PersonalAccount]:
    rows = (await db.execute(select(m.PersonalAccount).where(m.PersonalAccount.user_id == user_id))).scalars()
    return {a.kind: a for a in rows}


def set_password(acc: m.PersonalAccount, password: str | None, box: SecretBox | None = None) -> None:
    acc.password_enc = (box or get_box()).encrypt(password, aad_for("personal_accounts", "password", acc.id)) \
        if password else None


def account_values(acc: m.PersonalAccount, box: SecretBox | None = None) -> dict[str, str]:
    """Username, domain and password for a session (only what is filled in)."""
    values: dict[str, str] = {}
    if acc.username:
        values["username"] = acc.username
    if acc.domain:
        values["domain"] = acc.domain
    if acc.password_enc:
        password = (box or get_box()).decrypt(acc.password_enc, aad_for("personal_accounts", "password", acc.id))
        if password:
            values["password"] = password
    return values


def is_empty(acc: m.PersonalAccount) -> bool:
    return not (acc.username or acc.domain or acc.password_enc)
