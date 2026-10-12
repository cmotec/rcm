"""Mapping of connection settings to guacd connection parameters."""

from __future__ import annotations

import uuid
from typing import Any

from app.config import Settings
from app.services.sessions.launch import LaunchConfig


def _b(value: Any) -> str:
    return "true" if value else ""


def _s(value: Any) -> str:
    return "" if value is None else str(value)


def _recording(params: dict[str, str], settings: Settings, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    if settings.RECORDING_ENABLED:
        params["recording-path"] = f"{settings.RECORDING_PATH.rstrip('/')}/{user_id}"
        params["recording-name"] = str(session_id)
        params["create-recording-path"] = "true"


def rdp_params(
    launch: LaunchConfig, settings: Settings, *, user_id: uuid.UUID, session_id: uuid.UUID,
    client_timezone: str | None, host: str, port: int,
) -> dict[str, str]:
    s = launch.settings
    policy = launch.extra.get("cert_policy", "validate")
    p: dict[str, str] = {
        "hostname": host,
        "port": str(port),
        "username": _s(launch.username),
        "password": _s(launch.password),
        "domain": _s(launch.domain),
        "security": s.security,
        "ignore-cert": _b(policy == "ignore"),
        "cert-tofu": _b(policy == "tofu"),
        "cert-fingerprints": ",".join(s.cert_fingerprints) if policy == "fingerprints" else "",
        "resize-method": "" if s.resize_method == "none" else s.resize_method,
        "color-depth": _s(s.color_depth),
        "disable-audio": _b(s.disable_audio),
        "enable-audio-input": _b(s.enable_audio_input),
        "enable-printing": _b(s.enable_printing),
        "disable-copy": _b(s.disable_copy),
        "disable-paste": _b(s.disable_paste),
        "normalize-clipboard": _s(s.normalize_clipboard),
        "console": _b(s.console),
        "server-layout": _s(s.server_layout),
        "timezone": _s(s.timezone or client_timezone),
        "initial-program": _s(s.initial_program),
        "client-name": _s(s.client_name),
        "enable-wallpaper": _b(s.enable_wallpaper),
        "enable-theming": _b(s.enable_theming),
        "enable-font-smoothing": _b(s.enable_font_smoothing),
        "enable-full-window-drag": _b(s.enable_full_window_drag),
        "enable-desktop-composition": _b(s.enable_desktop_composition),
        "enable-menu-animations": _b(s.enable_menu_animations),
        "load-balance-info": _s(s.load_balance_info),
        "preconnection-id": _s(s.preconnection_id),
        "preconnection-blob": _s(s.preconnection_blob),
        "read-only": _b(s.read_only),
    }
    if s.remote_app:
        app = s.remote_app if s.remote_app.startswith("||") else "||" + s.remote_app
        p.update({"remote-app": app, "remote-app-dir": _s(s.remote_app_dir), "remote-app-args": _s(s.remote_app_args)})
    if s.gateway_hostname:
        p.update(
            {
                "gateway-hostname": s.gateway_hostname,
                "gateway-port": _s(s.gateway_port or 443),
                "gateway-domain": _s(s.gateway_domain or launch.domain),
                "gateway-username": _s(launch.username),
                "gateway-password": _s(launch.password),
            }
        )
    if s.enable_drive and settings.RDP_DRIVE_ENABLED:
        p.update(
            {
                "enable-drive": "true",
                "drive-name": s.drive_name or "Transfer",
                "drive-path": f"{settings.RDP_DRIVE_ROOT.rstrip('/')}/{user_id}",
                "create-drive-path": "true",
                "disable-download": _b(s.disable_download),
                "disable-upload": _b(s.disable_upload),
            }
        )
    _recording(p, settings, user_id, session_id)
    return p


def vnc_params(
    launch: LaunchConfig, settings: Settings, *, user_id: uuid.UUID, session_id: uuid.UUID, host: str, port: int,
) -> dict[str, str]:
    s = launch.settings
    p = {
        "hostname": host,
        "port": str(port),
        "username": _s(launch.username),
        "password": _s(launch.password),
        "color-depth": _s(s.color_depth),
        "cursor": _s(s.cursor),
        "encodings": _s(s.encodings),
        "read-only": _b(s.read_only),
        "swap-red-blue": _b(s.swap_red_blue),
        "disable-copy": _b(s.disable_copy),
        "disable-paste": _b(s.disable_paste),
        "clipboard-encoding": _s(s.clipboard_encoding),
        "dest-host": _s(s.dest_host),
        "dest-port": _s(s.dest_port),
        "autoretry": "2",
    }
    _recording(p, settings, user_id, session_id)
    return p


def ssh_params(
    launch: LaunchConfig, settings: Settings, *, user_id: uuid.UUID, session_id: uuid.UUID,
    client_timezone: str | None, host: str, port: int, host_key: str | None,
) -> dict[str, str]:
    s = launch.settings
    p = {
        "hostname": host,
        "port": str(port),
        "username": _s(launch.username),
        "password": _s(launch.password),
        "private-key": _s(launch.private_key),
        "passphrase": _s(launch.passphrase),
        "host-key": _s(host_key),
        "font-size": _s(s.font_size),
        "color-scheme": _s(s.color_scheme),
        "command": _s(s.command),
        "terminal-type": _s(s.term_type),
        "scrollback": _s(s.scrollback),
        "server-alive-interval": _s(s.keepalive_interval or settings.SSH_KEEPALIVE_SECONDS),
        "timezone": _s(client_timezone),
    }
    _recording(p, settings, user_id, session_id)
    return p


def client_settings(launch: LaunchConfig) -> dict[str, Any]:
    """Non-secret settings the browser needs to drive the session UI."""
    s = launch.settings
    proto = launch.protocol
    if proto == "rdp":
        return {
            "resize_method": s.resize_method,
            "fixed_width": s.width,
            "fixed_height": s.height,
            "dpi": s.dpi,
            "read_only": s.read_only,
            "disable_copy": s.disable_copy,
            "disable_paste": s.disable_paste,
            "drive_enabled": bool(s.enable_drive),
            "disable_upload": s.disable_upload,
            "disable_download": s.disable_download,
            "auto_reconnect": s.auto_reconnect,
            "server_resize": s.resize_method != "none",
        }
    if proto == "vnc":
        return {
            "read_only": s.read_only,
            "disable_copy": s.disable_copy,
            "disable_paste": s.disable_paste,
            "auto_reconnect": s.auto_reconnect,
            "server_resize": True,
        }
    if proto == "ssh":
        return {
            "engine": s.engine,
            "font_size": s.font_size,
            "color_scheme": s.color_scheme,
            "scrollback": s.scrollback,
            "auto_reconnect": s.auto_reconnect,
            "server_resize": True,
        }
    if proto in ("sftp", "ftp", "ftps"):
        return {
            "start_directory": getattr(s, "start_directory", None),
            "show_hidden": getattr(s, "show_hidden", False),
            "transfer_type": getattr(s, "transfer_type", "binary"),
            "supports_chmod": proto == "sftp",
        }
    return {}
