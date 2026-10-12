"""Import from Devolutions Remote Desktop Manager exports.

Supported inputs
----------------
* ``.rdm`` / ``.xml`` — RDM's XML export (``<Connection>`` elements, optionally
  nested in ``<SubConnections>``). Root element names differ between RDM
  versions, so the parser looks for ``Connection`` elements anywhere.
* ``.csv`` — RDM's CSV export, mapped by column header.

Rules
-----
* Nothing is dropped silently. Every non-empty value the importer does not
  understand goes into the item's ``unsupported`` list. Unsupported entry
  types are listed.
* Encrypted secrets (``SafePassword``, ``Password`` with encrypted content,
  etc.) are **not** decrypted. The item is marked
  ``credential_status = "encrypted_not_migrated"``, and the report explains
  the manual migration.
* Sub-connections linked with ``<ParentID>`` (e.g. an "SFTP" entry with
  ``HostSourceMode=Inherited``) become child connections that inherit the
  parent's hostname.
* Credential settings: ``CredentialConnectionID`` and
  ``CredentialConnectionSavedPath`` (``Group\\Entry``) reference a credential
  entry. The "prompt on connection" placeholder (``--- Bei Verbindung abfragen ---``
  and its translations) maps to "choose a credential at connect time", and
  ``AlwaysAskForPassword`` maps to interactive authentication.
* Labels: the tree shows ``hostname (description)``, so a name like
  ``host01 (Mail)`` puts "Mail" into the description, and an FQDN is split into
  hostname and domain (``host01`` + ``example.org``).
* RDM client bookkeeping (owner/resource texts, offline/temporary-access
  security, proxy types without a proxy, …) is ignored rather than reported.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import re
import uuid
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any
from xml.etree.ElementTree import Element  # noqa: S405 - type only; parsing uses defusedxml

from defusedxml import ElementTree as SafeET

MAX_UNSUPPORTED_PER_ITEM = 200

TYPE_MAP = {
    "rdpconfigured": "rdp", "rdp": "rdp", "rdpfilename": "rdp",
    "sshshell": "ssh", "ssh": "ssh", "sshterminal": "ssh",
    "vnc": "vnc", "appleremotedesktop": "vnc",
    "sftp": "sftp", "ftp": "ftp", "ftpnative": "ftp", "ftps": "ftps", "filetransfer": "ftp",
}
FOLDER_TYPES = {"group", "folder"}
CREDENTIAL_TYPES = {"credential", "credentials"}
SENSITIVE = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|private.?key|api.?key|key.?data|^pin(code)?$|safe|protected|encrypted)",
    re.IGNORECASE,
)
# Leaf tags that are bookkeeping, not settings.
NOISE = {
    "ID", "Stamp", "CreatedBy", "CreationDateTime", "ModifiedBy", "ModifiedDateTime", "UpdateDateTime",
    "MetaInformation", "SortPriority", "OpenEmbedded", "ConnectionType", "Name", "Group", "Url", "Description",
    "Keywords", "Tags", "CredentialConnectionID", "ParentID", "ImageName", "Image", "DataEntryConnectionType",
    "AutoUpdateOnOpen", "Events", "GroupTab", "ShowInTrayIcon", "ConnectionSubType", "TabPage",
    "AppVersion", "Status", "HostSourceMode", "CredentialConnectionGroup", "CredentialConnectionSavedPath",
}
# Subtrees that are RDM-only (offline cache, temporary access, …) and have no meaning here.
NOISE_SUBTREES = {"Security", "GroupDetails"}
# Leaf tags that are RDM client bookkeeping wherever they appear.
NOISE_LEAVES = {
    "OwnerText", "ResourceText", "AllowMultipleConnections", "PrivateKeyPromptForPassPhrase", "ShowLocalFiles",
    "KeyboardHook", "AlwaysAskForPassword",
}
PROXY_HOST_TAGS = ("ProxyHost", "ProxyHostname", "ProxyServer")
# CredentialConnectionSavedPath placeholders: "--- Prompt on connection ---" and translations.
PLACEHOLDER = re.compile(r"^\s*(?:-{2,}|<)(.*?)(?:-{2,}|>)\s*$")
PROMPT_WORDS = ("abfragen", "prompt", "ask", "demander", "preguntar", "chiedi", "vragen")
INHERIT_WORDS = ("inherit", "geerbt", "vererbt", "hérit", "herit")
NONE_WORDS = ("none", "kein", "aucun", "ningun")
# "host01 (Mail server)" / "host01(Mail server)"
LABEL_IN_NAME = re.compile(r"^\s*([^\s()]+)\s*\((.+)\)\s*$")


@dataclass
class ImportItem:
    source_id: str
    kind: str  # folder | connection | credential | unsupported
    name: str
    entry_type: str
    protocol: str | None = None
    host: str | None = None  # as exported (FQDN or IP)
    hostname: str | None = None  # host without its domain (what the connection stores)
    search_domain: str | None = None
    port: int | None = None
    folder_path: list[str] = field(default_factory=list)
    parent_source_id: str | None = None
    credential_source_id: str | None = None
    # reference | choose (pick at connect) | prompt (type at connect) | inherit | None (no setting)
    credential_mode: str | None = None
    credential_path: str | None = None  # "Group\Entry" from CredentialConnectionSavedPath
    credential_folder: bool = False  # RDM group holding credential entries (not a connection folder)
    username: str | None = None
    domain: str | None = None
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    settings: dict[str, Any] = field(default_factory=dict)
    # none | username_only | encrypted_not_migrated | reference | reference_missing | choose_at_connect
    # | prompt_at_connect | inherited
    credential_status: str = "none"
    unsupported: list[dict[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    duplicate_of: str | None = None
    duplicate_reason: str | None = None


@dataclass
class ParsedImport:
    format: str
    items: list[ImportItem]
    errors: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        by_kind: dict[str, int] = {}
        unsupported_types: dict[str, int] = {}
        for i in self.items:
            by_kind[i.kind] = by_kind.get(i.kind, 0) + 1
            if i.kind == "unsupported":
                unsupported_types[i.entry_type] = unsupported_types.get(i.entry_type, 0) + 1
        return {
            "total": len(self.items),
            "by_kind": by_kind,
            "unsupported_types": unsupported_types,
            "encrypted_credentials": sum(1 for i in self.items if i.credential_status == "encrypted_not_migrated"),
            "items_with_unsupported_settings": sum(1 for i in self.items if i.unsupported),
            "duplicates": sum(1 for i in self.items if i.duplicate_of),
            "errors": self.errors,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"format": self.format, "items": [asdict(i) for i in self.items], "summary": self.summary()}


# --------------------------------------------------------------------------- helpers


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(el: Element, *names: str) -> str | None:
    for child in el:
        if _local(child.tag) in names and child.text and child.text.strip():
            return child.text.strip()
    return None


def _find_text(el: Element, *names: str, skip: tuple[str, ...] = ("SubConnections",)) -> tuple[str | None, str | None]:
    """Depth-first search for the first non-empty element with one of ``names`` (returns value, path)."""
    stack: list[tuple[Element, str]] = [(el, "")]
    while stack:
        node, path = stack.pop(0)
        for child in node:
            tag = _local(child.tag)
            if tag in skip:
                continue
            p = f"{path}/{tag}" if path else tag
            if tag in names and child.text and child.text.strip():
                return child.text.strip(), p
            stack.append((child, p))
    return None, None


def _leaves(el: Element, prefix: str = "") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for child in el:
        tag = _local(child.tag)
        if tag in ("SubConnections",):
            continue
        p = f"{prefix}/{tag}" if prefix else tag
        if len(child):
            out.extend(_leaves(child, p))
        elif child.text and child.text.strip():
            out.append((p, child.text.strip()))
        for attr, val in child.attrib.items():
            if val.strip():
                out.append((f"{p}@{attr}", val.strip()))
    return out


def _bool(v: str | None) -> bool | None:
    if v is None:
        return None
    return v.strip().lower() in ("true", "1", "yes", "on")


def split_host_port(value: str) -> tuple[str, int | None]:
    v = value.strip()
    for prefix in ("rdp://", "ssh://", "vnc://", "ftp://", "ftps://", "sftp://", "telnet://"):
        if v.lower().startswith(prefix):
            v = v[len(prefix):]
    v = v.split("/", 1)[0]
    if "@" in v:
        v = v.rsplit("@", 1)[1]
    if v.startswith("["):  # [ipv6]:port
        host, _, rest = v[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
        return host, int(port) if port.isdigit() else None
    if v.count(":") == 1:
        host, port = v.split(":")
        return host, int(port) if port.isdigit() else None
    return v, None


def _tags(value: str | None) -> list[str]:
    if not value:
        return []
    parts = re.findall(r'"([^"]+)"|(\S+)', value.replace(",", " ").replace(";", " "))
    out: list[str] = []
    for quoted, plain in parts:
        t = (quoted or plain).strip()[:64]
        if t and t not in out:
            out.append(t)
    return out[:50]


def _redact(path: str, value: str) -> str:
    return "[redacted]" if SENSITIVE.search(path.rsplit("/", 1)[-1]) else value[:300]


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


def split_fqdn(host: str) -> tuple[str, str | None]:
    """``web01.example.org`` → (``web01``, ``example.org``). IPs and single labels are left alone."""
    h = host.strip()
    if _is_ip(h) or h.endswith(".") or "." not in h:
        return h, None
    name, _, domain = h.partition(".")
    if not name or not domain:
        return h, None
    return name, domain.lower()


def _label_from_name(name: str, host: str | None, short: str | None) -> str | None:
    """What the RDM name adds to the hostname (the tree shows ``hostname (description)``)."""
    if not host:
        return None
    known = {host.lower(), (short or "").lower()}
    m = LABEL_IN_NAME.match(name)
    if m and m.group(1).lower() in known:
        return m.group(2).strip() or None
    if name.strip().lower() in known:
        return None
    return name.strip() or None


def _credential_placeholder(value: str) -> str | None:
    """Classify a CredentialConnectionSavedPath placeholder: choose | inherit | none | unknown (None = a path)."""
    m = PLACEHOLDER.match(value)
    if not m:
        return None
    word = m.group(1).strip().lower()
    if any(w in word for w in PROMPT_WORDS):
        return "choose"
    if any(w in word for w in INHERIT_WORDS):
        return "inherit"
    if any(w in word for w in NONE_WORDS):
        return "none"
    return "unknown"


def _path_key(segments: list[str]) -> str:
    return "\\".join(s.strip().lower() for s in segments if s.strip())


# --------------------------------------------------------------------------- XML


def _protocol_settings(protocol: str, el: Element, consumed: set[str], item: ImportItem) -> dict[str, Any]:
    s: dict[str, Any] = {}

    def take(*names: str) -> str | None:
        val, path = _find_text(el, *names)
        if path:
            consumed.add(path)
        return val

    if protocol == "rdp":
        auth_level = take("AuthentificationLevel", "AuthenticationLevel")
        if auth_level is not None:
            mapping = {"0": "ignore", "1": "validate", "2": "tofu",
                       "connectdontwarnme": "ignore", "dontconnect": "validate", "warnme": "tofu"}
            policy = mapping.get(auth_level.strip().lower())
            if policy:
                s["cert_policy"] = policy
                if policy != "validate":
                    item.warnings.append(f"RDP server authentication level '{auth_level}' mapped to cert_policy '{policy}'")
        if _bool(take("UseCredSSP", "EnableCredSSPSupport", "NetworkLevelAuthentication")):
            s["security"] = "nla"
        if _bool(take("Console", "AdminMode", "ConnectToConsole")):
            s["console"] = True
        app = take("RemoteApplicationProgram", "RemoteAppProgram")
        if app:
            s["remote_app"] = app
            cmd = take("RemoteApplicationCmdLine", "RemoteAppCmdLine")
            if cmd:
                s["remote_app_args"] = cmd
        if _bool(take("UsesHardDrives", "RedirectDrives", "Drives")):
            s["enable_drive"] = True
        if (take("SoundHook") or "").lower() == "donotplay":
            s["disable_audio"] = True
        prog = take("AlternateShell", "StartProgram", "ShellWorkingDirectory")
        if prog:
            s["initial_program"] = prog
        gw = take("GatewayHostname", "RDPGatewayHostname", "GatewayHost")
        if gw:
            s["gateway_hostname"] = gw
        if _bool(take("DisableWallpaper")) is False:
            s["enable_wallpaper"] = True
    elif protocol == "vnc":
        disp = take("Display", "DisplayNumber")
        if disp and disp.isdigit():
            s["display"] = int(disp)
        if _bool(take("ViewOnly", "ReadOnly")):
            s["read_only"] = True
    elif protocol in ("ftp", "ftps"):
        passive = take("Passive", "PassiveMode", "UsePassiveMode")
        if passive is not None:
            s["mode"] = "passive" if _bool(passive) else "active"
        enc = (take("Encryption", "SecureMode", "FtpSecure", "SecurityMode") or "").lower()
        if "implicit" in enc:
            item.protocol = "ftps"
            s["tls_mode"] = "implicit"
        elif "explicit" in enc or enc in ("tls", "ssl"):
            item.protocol = "ftps"
            s["tls_mode"] = "explicit"
        start = take("RemoteDirectory", "InitialDirectory", "RemoteFolder", "Directory")
        if start and start != "/":
            s["start_directory"] = start
    elif protocol in ("ssh", "sftp"):
        keyfile = take("PrivateKeyPath", "PrivateKeyFile", "PrivateKeyFileName")
        if keyfile:
            item.warnings.append(f"private key file '{keyfile}' refers to the RDM client machine; "
                                 "import the key into a credential entry manually")
        start = take("RemoteDirectory", "InitialDirectory", "Directory")
        if start and start != "/" and protocol == "sftp":
            s["start_directory"] = start
    return s


def _credential_setting(el: Element, item: ImportItem) -> None:
    """How the entry gets its credentials (the reference by path is resolved after parsing)."""
    if item.credential_source_id:
        item.credential_mode, item.credential_status = "reference", "reference"
        return
    saved = _child_text(el, "CredentialConnectionSavedPath")
    if _bool(_find_text(el, "AlwaysAskForPassword")[0]):
        item.credential_mode, item.credential_status = "prompt", "prompt_at_connect"
    elif saved:
        kind = _credential_placeholder(saved)
        if kind is None:
            item.credential_mode, item.credential_path = "reference", saved
            item.credential_status = "reference"
        elif kind == "choose":
            item.credential_mode, item.credential_status = "choose", "choose_at_connect"
        elif kind == "inherit":
            item.credential_mode, item.credential_status = "inherit", "inherited"
        elif kind == "unknown":
            item.warnings.append(f"credential setting '{saved}' was not understood; set authentication manually")


def _parse_xml_connection(el: Element, parent_id: str | None, items: list[ImportItem]) -> None:
    entry_type = _child_text(el, "ConnectionType") or "Unknown"
    etype = entry_type.lower()
    name = _child_text(el, "Name") or "(unnamed)"
    source_id = _child_text(el, "ID") or str(uuid.uuid4())
    group = _child_text(el, "Group") or ""
    path = [p for p in re.split(r"[\\/]", group) if p.strip()]
    consumed: set[str] = set()

    if etype in FOLDER_TYPES:
        if not path or path[-1] != name:
            path = [*path, name]
        group_type, _ = _find_text(el, "GroupType")
        cred_folder = (group_type or "").lower() == "credentials"
        items.append(ImportItem(source_id=source_id, kind="folder", name=name,
                                entry_type=f"{entry_type} (credentials, not created)" if cred_folder else entry_type,
                                folder_path=path, description=_child_text(el, "Description"),
                                credential_folder=cred_folder))
        return

    item = ImportItem(source_id=source_id, kind="connection", name=name, entry_type=entry_type, folder_path=path,
                      parent_source_id=parent_id or _child_text(el, "ParentID"),
                      description=_child_text(el, "Description"),
                      tags=_tags(_child_text(el, "Keywords", "Tags")),
                      credential_source_id=_child_text(el, "CredentialConnectionID"))

    if etype in CREDENTIAL_TYPES:
        item.kind = "credential"
    elif etype in TYPE_MAP:
        item.protocol = TYPE_MAP[etype]
    else:
        item.kind = "unsupported"
        item.warnings.append(f"entry type '{entry_type}' is not supported and will not be imported")

    url = _child_text(el, "Url")
    host, hp = (split_host_port(url) if url else (None, None))
    if not host:
        h, p = _find_text(el, "Host", "HostName", "Hostname", "Server", "ComputerName")
        if p:
            consumed.add(p)
            host, hp2 = split_host_port(h or "")
            hp = hp or hp2
    item.host = host or None
    if item.host:
        item.hostname, item.search_domain = split_fqdn(item.host)
        if item.kind == "connection":
            label = _label_from_name(name, item.host, item.hostname)
            if label and label != (item.description or "").split("\n", 1)[0].strip():
                item.description = f"{label}\n\n{item.description}" if item.description else label
    port_s, port_path = _find_text(el, "Port", "HostPort")
    if port_path:
        consumed.add(port_path)
    port = int(port_s) if port_s and port_s.isdigit() else hp
    item.port = port if port and 0 < port < 65536 else None

    u, up = _find_text(el, "UserName", "Username", "User", "Login")
    if up:
        consumed.add(up)
    d, dp = _find_text(el, "Domain", "UserDomain")
    if dp:
        consumed.add(dp)
    item.username, item.domain = u, d
    pw, pp = _find_text(el, "SafePassword", "Password", "EncryptedPassword", "ProtectedPassword", "PasswordItem")
    if pp:
        consumed.add(pp)
    if item.kind != "credential":
        _credential_setting(el, item)
    if pw:
        item.credential_status = "encrypted_not_migrated"
        item.warnings.append("password is encrypted in the RDM export and was not migrated")
    elif item.credential_status == "none" and u:
        item.credential_status = "username_only"

    if item.kind == "connection" and item.protocol:
        item.settings = _protocol_settings(item.protocol, el, consumed, item)

    has_proxy_host = _find_text(el, *PROXY_HOST_TAGS)[0] is not None
    for path_, value in _leaves(el):
        top = path_.split("/", 1)[0].split("@", 1)[0]
        leaf = path_.rsplit("/", 1)[-1].split("@", 1)[0]
        if path_ in consumed or (top in NOISE and "/" not in path_) or top in NOISE_SUBTREES:
            continue
        if leaf in NOISE_LEAVES or (leaf == "ProxyType" and not has_proxy_host):
            continue
        if value.lower() in ("false", "0", "none", "default", "00000000-0000-0000-0000-000000000000"):
            continue
        if len(item.unsupported) >= MAX_UNSUPPORTED_PER_ITEM:
            item.unsupported.append({"setting": "…", "value": "further settings omitted from the report"})
            break
        item.unsupported.append({"setting": path_, "value": _redact(path_, value)})
    items.append(item)

    for child in el:
        if _local(child.tag) == "SubConnections":
            for sub in child:
                if _local(sub.tag) == "Connection":
                    _parse_xml_connection(sub, source_id, items)


def parse_xml(data: bytes) -> ParsedImport:
    try:
        root = SafeET.fromstring(data)
    except Exception as exc:  # defusedxml raises several types (ParseError, DTDForbidden, …)
        return ParsedImport(format="rdm-xml", items=[], errors=[f"invalid XML: {exc}"])
    items: list[ImportItem] = []

    def walk(node: Element) -> None:
        for child in node:
            tag = _local(child.tag)
            if tag == "Connection":
                _parse_xml_connection(child, None, items)
            elif tag != "SubConnections":
                walk(child)

    if _local(root.tag) == "Connection":
        _parse_xml_connection(root, None, items)
    else:
        walk(root)
    _link_credentials(items)
    return ParsedImport(format="rdm-xml", items=_tree_order(items),
                        errors=[] if items else ["no <Connection> entries found in the file"])


def _link_credentials(items: list[ImportItem]) -> None:
    """Resolve "Group\\Entry" credential paths, and let children inherit when they match their parent."""
    by_path: dict[str, ImportItem] = {}
    by_name: dict[str, list[ImportItem]] = defaultdict(list)
    for c in items:
        if c.kind == "credential":
            by_path.setdefault(_path_key([*c.folder_path, c.name]), c)
            by_name[c.name.strip().lower()].append(c)
    for it in items:
        if it.credential_mode != "reference" or it.credential_source_id or not it.credential_path:
            continue
        segments = [s for s in re.split(r"[\\/]", it.credential_path) if s.strip()]
        hit = by_path.get(_path_key(segments))
        if hit is None and segments and len(by_name.get(segments[-1].strip().lower(), [])) == 1:
            hit = by_name[segments[-1].strip().lower()][0]
        if hit is not None:
            it.credential_source_id = hit.source_id
        else:
            it.credential_status = "reference_missing"
            it.warnings.append(f"credential entry '{it.credential_path}' is not part of this export")

    by_id = {i.source_id: i for i in items}
    for it in items:
        parent = by_id.get(it.parent_source_id or "")
        if parent is None or parent.kind != "connection" or it.credential_mode is None:
            continue
        same = (it.credential_mode == parent.credential_mode
                and it.credential_source_id == parent.credential_source_id and not it.username)
        if same:  # e.g. the "SFTP" child prompts like its SSH parent: inherit (if the parent is imported too)
            it.credential_status = "inherited"


def _tree_order(items: list[ImportItem]) -> list[ImportItem]:
    """Folders, credentials, then connections by folder and name, each parent followed by its children."""
    rank = {"folder": 0, "credential": 1}
    ids = {i.source_id for i in items}
    children: dict[str, list[ImportItem]] = defaultdict(list)
    roots: list[ImportItem] = []
    for i in items:
        if i.parent_source_id in ids and i.parent_source_id != i.source_id:
            children[i.parent_source_id or ""].append(i)
        else:
            roots.append(i)

    def key(i: ImportItem) -> tuple[int, list[str], str]:
        return rank.get(i.kind, 2), [p.lower() for p in i.folder_path], i.name.lower()

    out: list[ImportItem] = []
    seen: set[str] = set()

    def emit(i: ImportItem) -> None:
        if i.source_id in seen:
            return
        seen.add(i.source_id)
        out.append(i)
        for c in sorted(children.get(i.source_id, []), key=key):
            emit(c)

    for r in sorted(roots, key=key):
        emit(r)
    out.extend(i for i in items if i.source_id not in seen)  # parent cycles: keep everything
    return out


# --------------------------------------------------------------------------- CSV

CSV_COLUMNS = {
    "name": ("name", "entry name", "connection name"),
    "type": ("connectiontype", "connection type", "type", "entry type"),
    "group": ("group", "folder", "path", "group path"),
    "host": ("host", "url", "hostname", "computer name", "server", "address"),
    "port": ("port",),
    "username": ("username", "user name", "user", "login"),
    "domain": ("domain",),
    "password": ("password", "safepassword"),
    "description": ("description", "notes"),
    "tags": ("tags", "keywords"),
    "id": ("id", "entry id"),
}


def parse_csv(data: bytes) -> ParsedImport:
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    if not reader.fieldnames:
        return ParsedImport(format="rdm-csv", items=[], errors=["CSV has no header row"])
    lookup = {h.strip().lower(): h for h in reader.fieldnames if h}
    colmap: dict[str, str] = {}
    for key, aliases in CSV_COLUMNS.items():
        for a in aliases:
            if a in lookup:
                colmap[key] = lookup[a]
                break
    items: list[ImportItem] = []
    errors: list[str] = []
    if "name" not in colmap:
        errors.append("no 'Name' column found")
    mapped_cols = set(colmap.values())
    for n, row in enumerate(reader, start=2):
        name = (row.get(colmap.get("name", ""), "") or "").strip()
        if not name:
            continue
        etype = (row.get(colmap.get("type", ""), "") or "").strip() or "RDPConfigured"
        group = row.get(colmap.get("group", ""), "") or ""
        item = ImportItem(source_id=(row.get(colmap.get("id", ""), "") or "").strip() or f"csv-row-{n}",
                          kind="connection", name=name, entry_type=etype,
                          folder_path=[p for p in re.split(r"[\\/]", group) if p.strip()],
                          description=(row.get(colmap.get("description", ""), "") or None),
                          tags=_tags(row.get(colmap.get("tags", ""), "")))
        low = etype.lower()
        if low in FOLDER_TYPES:
            item.kind = "folder"
            if not item.folder_path or item.folder_path[-1] != name:
                item.folder_path.append(name)
        elif low in CREDENTIAL_TYPES:
            item.kind = "credential"
        elif low in TYPE_MAP:
            item.protocol = TYPE_MAP[low]
        else:
            item.kind = "unsupported"
            item.warnings.append(f"entry type '{etype}' is not supported and will not be imported")
        host_raw = (row.get(colmap.get("host", ""), "") or "").strip()
        if host_raw:
            item.host, item.port = split_host_port(host_raw)
        port_raw = (row.get(colmap.get("port", ""), "") or "").strip()
        if port_raw.isdigit() and 0 < int(port_raw) < 65536:
            item.port = int(port_raw)
        item.username = (row.get(colmap.get("username", ""), "") or "").strip() or None
        item.domain = (row.get(colmap.get("domain", ""), "") or "").strip() or None
        if (row.get(colmap.get("password", ""), "") or "").strip():
            item.credential_status = "encrypted_not_migrated"
            item.warnings.append("a password column is present; passwords are never imported from CSV — "
                                 "store them in the credential store or an external secret manager")
        elif item.username:
            item.credential_status = "username_only"
        for col, val in row.items():
            if col and col not in mapped_cols and val and str(val).strip():
                item.unsupported.append({"setting": col, "value": _redact(col, str(val).strip())})
        items.append(item)
    return ParsedImport(format="rdm-csv", items=items, errors=errors)


def parse(filename: str | None, data: bytes) -> ParsedImport:
    name = (filename or "").lower()
    head = data[:200].lstrip()
    if name.endswith(".csv") or not (head.startswith(b"<") or head.startswith(b"\xef\xbb\xbf<")):
        return parse_csv(data)
    return parse_xml(data)
