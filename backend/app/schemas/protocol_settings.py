"""Per-protocol connection settings (stored in ``connections.settings``)."""

from __future__ import annotations

import re
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.errors import ValidationFailed

CertPolicy = Literal["validate", "fingerprints", "tofu", "ignore"]
HostKeyPolicy = Literal["prompt", "strict", "accept-new"]
_FINGERPRINT = re.compile(r"^(sha1|sha256|sha384|sha512):[0-9A-Fa-f:]+$")


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SshTunnel(_Base):
    via_connection_id: uuid.UUID = Field(description="SSH connection used as tunnel endpoint")


class HostSettings(_Base):
    """A host node only carries shared values (hostname, credentials) for its children."""


class SshSettings(_Base):
    engine: Literal["native", "guacd"] = "native"
    host_key_policy: HostKeyPolicy | None = None
    connect_timeout: int | None = Field(None, ge=1, le=300)
    keepalive_interval: int | None = Field(None, ge=0, le=3600)
    jump_host_connection_id: uuid.UUID | None = None
    term_type: str = Field("xterm-256color", max_length=64, pattern=r"^[A-Za-z0-9._+-]+$")
    command: str | None = Field(None, max_length=4096)
    font_size: int | None = Field(None, ge=6, le=48)
    color_scheme: str | None = Field(None, max_length=64)
    scrollback: int | None = Field(None, ge=100, le=100000)
    auto_reconnect: bool = True
    pinned_host_key: str | None = Field(None, max_length=8192, description="guacd engine only")


class SftpSettings(_Base):
    host_key_policy: HostKeyPolicy | None = None
    connect_timeout: int | None = Field(None, ge=1, le=300)
    jump_host_connection_id: uuid.UUID | None = None
    start_directory: str | None = Field(None, max_length=4096)
    show_hidden: bool = False


class RdpSettings(_Base):
    security: Literal["any", "nla", "nla-ext", "tls", "rdp", "vmconnect"] = "any"
    cert_policy: CertPolicy | None = None
    cert_fingerprints: list[str] = Field(default_factory=list, max_length=20)
    color_depth: Literal[8, 16, 24, 32] | None = None
    resize_method: Literal["display-update", "reconnect", "none"] = "display-update"
    width: int | None = Field(None, ge=320, le=8192)
    height: int | None = Field(None, ge=200, le=8192)
    dpi: int | None = Field(None, ge=48, le=480)
    enable_drive: bool = False
    drive_name: str | None = Field(None, max_length=64)
    disable_download: bool = False
    disable_upload: bool = False
    disable_copy: bool = False
    disable_paste: bool = False
    normalize_clipboard: Literal["preserve", "unix", "windows"] | None = None
    disable_audio: bool = False
    enable_audio_input: bool = False
    enable_printing: bool = False
    console: bool = False
    server_layout: str | None = Field(None, max_length=64)
    timezone: str | None = Field(None, max_length=64)
    initial_program: str | None = Field(None, max_length=1024)
    remote_app: str | None = Field(None, max_length=1024)
    remote_app_dir: str | None = Field(None, max_length=1024)
    remote_app_args: str | None = Field(None, max_length=1024)
    client_name: str | None = Field(None, max_length=15)
    enable_wallpaper: bool = False
    enable_theming: bool = False
    enable_font_smoothing: bool = True
    enable_full_window_drag: bool = False
    enable_desktop_composition: bool = False
    enable_menu_animations: bool = False
    gateway_hostname: str | None = Field(None, max_length=255)
    gateway_port: int | None = Field(None, ge=1, le=65535)
    gateway_domain: str | None = Field(None, max_length=255)
    load_balance_info: str | None = Field(None, max_length=1024)
    preconnection_id: int | None = Field(None, ge=0)
    preconnection_blob: str | None = Field(None, max_length=1024)
    read_only: bool = False
    ssh_tunnel: SshTunnel | None = None
    auto_reconnect: bool = True

    @field_validator("cert_fingerprints")
    @classmethod
    def _fp(cls, v: list[str]) -> list[str]:
        for fp in v:
            if not _FINGERPRINT.match(fp):
                raise ValueError("fingerprints must look like 'sha256:AB:CD:…'")
        return v


class VncSettings(_Base):
    display: int = Field(0, ge=0, le=99)
    color_depth: Literal[8, 16, 24, 32] | None = None
    cursor: Literal["local", "remote"] | None = None
    encodings: str | None = Field(None, max_length=256)
    read_only: bool = False
    swap_red_blue: bool = False
    disable_copy: bool = False
    disable_paste: bool = False
    clipboard_encoding: Literal["ISO8859-1", "UTF-8", "UTF-16", "CP1252"] | None = None
    dest_host: str | None = Field(None, max_length=255, description="VNC repeater destination host")
    dest_port: int | None = Field(None, ge=1, le=65535)
    ssh_tunnel: SshTunnel | None = None
    auto_reconnect: bool = True


class FtpSettings(_Base):
    mode: Literal["passive", "active"] = "passive"
    tls_mode: Literal["explicit", "implicit"] = "explicit"
    tls_verify: bool | None = None
    transfer_type: Literal["binary", "ascii"] = "binary"
    encoding: str = Field("utf-8", max_length=32)
    timeout: int | None = Field(None, ge=1, le=600)
    start_directory: str | None = Field(None, max_length=4096)
    show_hidden: bool = False
    trust_pasv_address: bool = False

    @field_validator("encoding")
    @classmethod
    def _enc(cls, v: str) -> str:
        import codecs

        try:
            codecs.lookup(v)
        except LookupError as exc:
            raise ValueError(f"unknown encoding {v!r}") from exc
        return v


SETTINGS_MODELS: dict[str, type[_Base]] = {
    "host": HostSettings,
    "ssh": SshSettings,
    "sftp": SftpSettings,
    "rdp": RdpSettings,
    "vnc": VncSettings,
    "ftp": FtpSettings,
    "ftps": FtpSettings,
}


def validate_settings(protocol: str, data: dict[str, Any] | None) -> dict[str, Any]:
    """Validate and normalise settings for a protocol (defaults are filled in)."""
    model = SETTINGS_MODELS.get(protocol)
    if model is None:
        raise ValidationFailed(f"unknown protocol {protocol!r}")
    try:
        obj = model.model_validate(data or {})
    except Exception as exc:  # pydantic.ValidationError
        errors = getattr(exc, "errors", lambda: [])()
        raise ValidationFailed(
            "invalid protocol settings",
            extra={"errors": [{"loc": ["settings", *e.get("loc", ())], "msg": e.get("msg")} for e in errors]},
        ) from exc
    return obj.model_dump(mode="json", exclude_none=True)


def parse_settings(protocol: str, data: dict[str, Any] | None) -> Any:
    model = SETTINGS_MODELS.get(protocol, HostSettings)
    return model.model_validate(data or {})


def security_warnings(protocol: str, settings: dict[str, Any], *, rdp_default: str, ftp_verify_default: bool,
                      ssh_default: str) -> list[dict[str, str]]:
    """Human-readable warnings about weak transport security, shown in the UI."""
    w: list[dict[str, str]] = []
    if protocol == "ftp":
        w.append({"code": "unencrypted", "message": "Plain FTP is unencrypted: credentials and data are sent in clear text."})
    elif protocol == "ftps":
        verify = settings.get("tls_verify")
        if (verify if verify is not None else ftp_verify_default) is False:
            w.append({"code": "tls_unverified", "message": "TLS certificate validation is disabled."})
    elif protocol == "vnc":
        if not settings.get("ssh_tunnel"):
            w.append({
                "code": "encryption_not_guaranteed",
                "message": "VNC traffic is unencrypted unless the server negotiates VeNCrypt; use an SSH tunnel for guaranteed encryption.",
            })
    elif protocol == "rdp":
        policy = settings.get("cert_policy") or rdp_default
        if policy == "ignore":
            w.append({"code": "cert_ignored", "message": "Certificate validation is disabled for this RDP connection."})
        elif policy == "tofu":
            w.append({"code": "cert_tofu", "message": "Certificates are accepted automatically on first use."})
    elif protocol in ("ssh", "sftp"):
        policy = settings.get("host_key_policy") or ssh_default
        if policy == "accept-new":
            w.append({"code": "hostkey_auto", "message": "Unknown SSH host keys are accepted automatically."})
    return w
