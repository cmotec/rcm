# guacd integration

We use guacd, the native Apache Guacamole proxy daemon, as a protocol gateway
for RDP and VNC, and optionally for SSH. The Guacamole web application (the
Java servlet, `guacamole-client` WAR, its auth extensions and its UI) is
**not** used. The Python backend implements the parts of that web application
we need: the handshake, the WebSocket tunnel, and authorisation.

Browser side, we use the `guacamole-common-js` library from npm only for
three things: its protocol parser, its display/canvas renderer, and its
keyboard, mouse and touch input mapping. All UI around it is our own.

## 1. Components and trust boundaries

```
 Browser ──WSS──► web (nginx) ──WS──► api ──TCP 4822──► guacd ──RDP/VNC──► target
   untrusted           TLS end        trusted          trusted, private       untrusted
```

* **guacd has no published port.** It is attached only to the private Compose
  network `gateway`, which it shares with `api` alone. `web` is not on that
  network. Anyone who can reach guacd can make it connect anywhere with any
  credentials, so this isolation is required.
* **Only `api` speaks the handshake.** The browser never sends `select` or
  `connect`, and never sees hostnames, ports, usernames or passwords for
  guacd-backed sessions. The backend builds those parameters from the stored
  connection, after authorising the user.
* Optional: guacd ↔ api over TLS (`GUACD_TLS=true`, guacd started with
  `-C/-K`). This is useful when guacd runs on a different host.

## 2. Wire format

A Guacamole instruction is a list of elements. Each element is `LENGTH.VALUE`.
`LENGTH` is the number of Unicode code points in `VALUE`. Elements are
separated by `,` and the instruction ends with `;`. The first element is the
opcode.

```
4.size,4.1024,3.768,2.96;
```

`backend/app/services/sessions/guacd_protocol.py` contains an incremental
parser that works in code points (Python `str`) and an encoder. The parser is
used for:

* the handshake;
* splitting guacd output on instruction boundaries, so every WebSocket
  message sent to the browser holds whole instructions. `guacamole-common-js`
  needs this;
* checking browser→guacd traffic against an allow-list.

## 3. Handshake (backend ↔ guacd)

```
api → guacd   select      "rdp"                                (or "vnc", "ssh")
guacd → api   args        "VERSION_1_5_0", "hostname", "port", "username", …
api → guacd   size        <width>, <height>, <dpi>
api → guacd   audio       "audio/L16", …        (when audio is enabled)
api → guacd   video                              (none)
api → guacd   image       "image/png", "image/jpeg", "image/webp"
api → guacd   timezone    <IANA tz from browser, validated>
api → guacd   name        <app username>         (for guacd logs only)
api → guacd   connect     "VERSION_1_5_0", <value for each arg in order>
guacd → api   ready       "$<connection-uuid>"
```

* The `args` list from guacd decides which values we send. A parameter we
  configured that this guacd version does not advertise (for example
  `cert-tofu` on an older guacd) is left out and logged as a warning, never an
  error. This keeps the backend compatible across guacd releases.
* Parameter mapping (connection settings → guacd args) lives in
  `services/sessions/guacd_params.py`. There is one builder per protocol, and
  each has unit tests.
* If guacd answers with `error` instead of `ready`, the status code is mapped
  to an HTTP-like reason and sent to the browser as the WebSocket close reason.
  `guacamole-common-js` turns it into a `Guacamole.Status`.
* Timeouts: the TCP connect to guacd is limited by
  `GUACD_CONNECT_TIMEOUT_SECONDS` and the handshake by
  `GUACD_HANDSHAKE_TIMEOUT_SECONDS`.

## 4. Tunnel (browser ↔ backend)

`guacamole-common-js`'s `Guacamole.WebSocketTunnel` is pointed at
`/api/v1/sessions/{session_id}/tunnel`. The client calls
`tunnel.connect("ticket=…&width=…&height=…&dpi=…&tz=…")`, which opens
`wss://…/tunnel?ticket=…` with the `guacamole` sub-protocol.

On upgrade the backend:

1. checks the `Origin` header against `APP_URL` (cross-site WebSocket
   hijacking protection; cookies are sent on WS upgrades);
2. authenticates the user from the session cookie or a bearer token;
3. consumes the **one-time ticket**. It is random (256 bit), valid for
   `SESSION_TICKET_TTL_SECONDS` (default 30 s), and bound to *both* the session
   ID and the user ID. A ticket leaked through logs or browser history cannot
   be replayed, and is useless to any other user;
4. checks that the session belongs to this user and is not already attached
   (one active tunnel per session, so a second tab cannot take over a live
   desktop);
5. connects to guacd and performs the handshake above;
6. sends the internal instruction `0.,36.<session-uuid>;`. The client library
   needs it to move the tunnel to `OPEN`;
7. relays in both directions until one side closes.

### guacd → browser
Bytes are decoded as UTF-8 incrementally, split on instruction boundaries, and
sent as WebSocket text frames, many instructions per frame.

### browser → guacd
Each frame is parsed. Then:

* the internal opcode (`""`) is handled by the backend. `ping` is echoed back
  so the client's receive timeout stays fresh. Nothing internal reaches guacd.
* allowed opcodes are forwarded: `key`, `mouse`, `touch`, `size`,
  `clipboard`, `blob`, `end`, `ack`, `file`, `pipe`, `put`, `get`, `audio`,
  `argv`, `sync`, `nop`, `disconnect`.
* everything else (`select`, `connect`, `args`, …) is dropped and logged,
  and the tunnel is closed. After `ready` guacd would ignore these anyway.
  Dropping them is defence in depth.
* `file`, `pipe`, `put` (uploads to RDP drive redirection) are only forwarded
  if the connection has drive redirection enabled and uploads are not disabled.
  Downloads are blocked by guacd itself through the `disable-download`
  parameter.
* For view-only connections, `key`, `mouse`, `touch` and `clipboard` are dropped
  as well (in addition to guacd's `read-only` parameter).

### Resizing, scaling and high-DPI
* The browser measures its container, multiplies by `devicePixelRatio`, and
  sends `size`. For RDP this needs `resize-method=display-update` (the
  default) or `reconnect`. The server then changes the **remote resolution**.
* The canvas is scaled with `display.scale(1 / devicePixelRatio)`, or
  "fit to window" when the remote side cannot resize. This is **visual scaling**
  only. The UI shows which of the two is active ("Remote resolution 2560×1440
  · scaled 100 %").
* The initial `size` in the handshake uses the same calculation, and `dpi` =
  `96 × devicePixelRatio`, so Windows applies the right scale factor.
* Resize events are debounced (200 ms) and ignored below an 8 px change, so
  dragging a split divider does not flood the server with resolution changes.

## 5. Session ownership

* Every session has exactly one owner (the user who created it). Only the
  owner can attach a tunnel, request a reconnect ticket or use file
  operations.
* Administrators with `sessions.manage` can list all sessions and terminate
  them, but cannot attach to them. Shadowing is not supported. If it is
  needed later, it should be a separately audited, read-only `select $uuid`
  join.
* All session events are audited: create, attach, detach, reconnect, close,
  and denied attempts.

## 6. Reconnection
guacd connections cannot be resumed after the TCP stream breaks. "Reconnect"
starts a new guacd connection with the same resolved parameters (including
interactive credentials held in memory). Windows RDP itself reattaches to the
existing user session, so the user sees the same desktop. The frontend
reconnects by itself, with backoff (1 s, 2 s, 5 s, 10 s, 20 s, then it gives
up and shows a *Reconnect* button), on network errors.
It does not reconnect after guacd reported an authentication or permission
error, or after the user disconnected.

## 7. Certificates

### RDP
`rdp_cert_policy` per connection, falling back to `RDP_CERT_POLICY_DEFAULT`:

| Policy | guacd parameters | UI |
|---|---|---|
| `validate` (default) | `ignore-cert=false` | — |
| `fingerprints` | `cert-fingerprints=<configured list>` | shows the pinned fingerprints |
| `tofu` (automatic acceptance on first use) | `cert-tofu=true` | warning badge |
| `ignore` | `ignore-cert=true` | red warning badge |

`RDP_CERT_ALLOW_INSECURE=false` turns off `tofu` and `ignore` globally. The
API then rejects them. `validate` needs the CA to be trusted *inside the guacd
container* (FreeRDP uses its own store). [deployment.md](deployment.md) shows
how to mount a CA bundle.

### VNC
guacd does not expose certificate settings for VeNCrypt. libvncclient
negotiates TLS when the server offers it, without verifying the certificate.
So VNC sessions are shown as **"Encryption not guaranteed"** unless they run
over an SSH tunnel (`settings.ssh_tunnel`). In that case the backend opens a
local forward and points guacd at it. A plain VNC connection without a tunnel
is always marked as unencrypted in the tree and in the window title bar.

## 8. SSH tunnelling for guacd protocols

If a connection has `settings.ssh_tunnel.via_connection_id` set, the backend:

1. opens an asyncssh connection to the referenced SSH connection (with that
   connection's own resolved credentials and host-key policy);
2. starts a local forward on the backend's address in the `gateway` network,
   on a random port, to `effective_host:effective_port`. It accepts only
   connections from guacd's address;
3. gives guacd `hostname=<api gateway address>` and `port=<forward port>`;
4. closes the forward when the session ends.

## 9. Things guacd does not do for us

| Need | Handled by |
|---|---|
| Authentication, authorisation, connection storage | backend |
| Jump hosts / SSH tunnels | backend (asyncssh) |
| SFTP / FTP file managers | backend (asyncssh, ftplib) |
| Host-key TOFU for SSH | backend (native SSH engine) |
| Session ownership, tickets, audit | backend |
| DNS search domains | backend resolves before the handshake |
