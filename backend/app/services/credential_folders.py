"""Credential folder helpers shared by the API and the secret import."""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import models as m


async def ensure_folder_path(db: AsyncSession, parent_id: uuid.UUID | None, names: list[str],
                             owner_id: uuid.UUID | None) -> uuid.UUID | None:
    """Find or create the folders ``names`` below ``parent_id`` (used by the OpenBao import)."""
    current = parent_id
    for name in names:
        name = name.strip()[:255]
        if not name:
            continue
        cond = m.CredentialFolder.parent_id.is_(None) if current is None else m.CredentialFolder.parent_id == current
        existing = (await db.execute(select(m.CredentialFolder.id).where(
            cond, func.lower(m.CredentialFolder.name) == name.lower()))).scalars().first()
        if existing is None:
            folder = m.CredentialFolder(id=uuid.uuid4(), name=name, parent_id=current, owner_id=owner_id)
            db.add(folder)
            await db.flush()
            existing = folder.id
        current = existing
    return current
