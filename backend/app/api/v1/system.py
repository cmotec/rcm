"""Health checks and system information."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from app import db as dbmod
from app.api.deps import Principal, get_principal, settings_dep
from app.config import Settings
from app.services.sessions.manager import get_manager

VERSION = "0.1.0"
health_router = APIRouter(tags=["health"])
router = APIRouter(prefix="/system", tags=["system"])


async def _guacd_reachable(settings: Settings) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(settings.GUACD_HOST, settings.GUACD_PORT), 2)
        writer.close()
        return True
    except (TimeoutError, OSError):
        return False


@health_router.get("/healthz", summary="Liveness probe")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@health_router.get("/readyz", summary="Readiness probe (database and guacd)")
async def readyz(settings: Settings = Depends(settings_dep)) -> JSONResponse:
    db_ok = await dbmod.ping()
    guacd_ok = await _guacd_reachable(settings)
    status = 200 if db_ok else 503
    return JSONResponse({"status": "ok" if db_ok else "degraded", "database": db_ok, "guacd": guacd_ok},
                        status_code=status)


@router.get("/info", summary="Version, enabled features and live counters")
async def info(principal: Principal = Depends(get_principal), settings: Settings = Depends(settings_dep)) -> dict:
    manager = get_manager()
    return {
        "version": VERSION,
        "app_display_name": settings.APP_DISPLAY_NAME,
        "features": {
            "oidc": settings.OIDC_ENABLED,
            "local_auth": settings.LOCAL_AUTH_ENABLED,
            "api_tokens": settings.API_TOKENS_ENABLED,
            "rdm_import": settings.FEATURE_RDM_IMPORT,
            "secret_sync": settings.SECRET_SYNC_ENABLED,
            "infisical": settings.INFISICAL_ENABLED,
            "openbao": settings.OPENBAO_ENABLED,
            "rdp_drive": settings.RDP_DRIVE_ENABLED,
            "ssh_guacd_engine": settings.SSH_GUACD_ENGINE_ENABLED,
            "recording": settings.RECORDING_ENABLED,
        },
        "limits": {
            "sessions_per_user": settings.SESSION_MAX_PER_USER,
            "upload_max_mb": settings.FILE_UPLOAD_MAX_MB,
            "reconnect_grace_seconds": settings.SESSION_RECONNECT_GRACE_SECONDS,
        },
        "default_search_domains": settings.search_domains,
        "sessions_active": len(manager.sessions) if principal.access.is_admin else None,
    }
