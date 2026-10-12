"""Open a remote file system for a file-transfer session."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.services.app_settings import RuntimeSettings
from app.services.sessions.files.base import RemoteFS
from app.services.sessions.files.ftp import FtpFS
from app.services.sessions.files.sftp import SftpFS
from app.services.sessions.launch import LaunchConfig
from app.services.sessions.ssh_native import open_connection_set


async def open_fs(db: AsyncSession, launch: LaunchConfig, settings: Settings, runtime: RuntimeSettings) -> RemoteFS:
    s = launch.settings
    if launch.protocol == "sftp":
        conns = await open_connection_set(db, launch, settings)
        try:
            return await SftpFS.open(conns, s.start_directory)
        except BaseException:
            conns.close()
            raise
    tls = s.tls_mode if launch.protocol == "ftps" else None
    verify = s.tls_verify if s.tls_verify is not None else runtime.ftp_tls_verify_default
    return await FtpFS.open(
        host=launch.host,
        port=launch.port,
        username=launch.username,
        password=launch.password,
        tls=tls,
        tls_verify=verify,
        ca_bundle=settings.TLS_CA_BUNDLE,
        passive=s.mode == "passive",
        trust_pasv_address=s.trust_pasv_address,
        encoding=s.encoding,
        timeout=float(s.timeout or settings.FTP_TIMEOUT_SECONDS),
        transfer_type=s.transfer_type,
        show_hidden=s.show_hidden,
        start_directory=s.start_directory,
    )
