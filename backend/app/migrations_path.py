"""Locate alembic.ini regardless of the working directory."""

from __future__ import annotations

from pathlib import Path


def alembic_config_path() -> str:
    """Source checkout: <repo>/backend/alembic.ini. Container: /srv/alembic.ini (package installed)."""
    candidates = [Path(__file__).resolve().parent.parent / "alembic.ini", Path("/srv/alembic.ini"),
                  Path.cwd() / "alembic.ini"]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise FileNotFoundError("alembic.ini not found")
