"""Operational commands: ``python -m app.cli <command>``.

  generate-key                 print a new random encryption key (base64, 32 bytes)
  migrate                      apply database migrations
  create-admin USER            create a local administrator (asks for the password)
  grant-role USER ROLE         assign a role to an existing user (e.g. the first OIDC admin)
  rotate-keys                  re-encrypt all secrets with ENCRYPTION_ACTIVE_KEY_ID
  openapi [FILE]               write the OpenAPI document (default: stdout)
  check-config                 validate configuration and connectivity
  ftp-test HOST [options]      diagnose an FTP/FTPS connection step by step (see --help)
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys

from sqlalchemy import func, select


def _settings():  # noqa: ANN202
    from app.config import get_settings

    return get_settings()


async def _with_db(fn) -> int:  # noqa: ANN001
    from app.db import dispose_engine, init_engine, session_scope
    from app.security.crypto import init_crypto

    s = _settings()
    init_crypto(s.ENCRYPTION_KEYS, s.ENCRYPTION_ACTIVE_KEY_ID)
    init_engine(s)
    try:
        async with session_scope() as db:
            return await fn(db)
    finally:
        await dispose_engine()


def cmd_generate_key(_: argparse.Namespace) -> int:
    from app.security.crypto import generate_key

    print(generate_key())
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    from app.main import run_migrations

    run_migrations(_settings())
    print("migrations applied")
    return 0


def cmd_create_admin(args: argparse.Namespace) -> int:
    from app import models as m
    from app.security.passwords import hash_password, validate_password_strength
    from app.services.authz import ensure_builtin_roles

    password = args.password or getpass.getpass("Password: ")
    if not args.password and getpass.getpass("Repeat: ") != password:
        print("passwords do not match", file=sys.stderr)
        return 1
    try:
        validate_password_strength(password)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1

    async def run(db) -> int:  # noqa: ANN001
        roles = await ensure_builtin_roles(db)
        exists = (await db.execute(select(m.User).where(func.lower(m.User.username) == args.username.lower()))).scalar_one_or_none()
        if exists is not None:
            print(f"user {args.username!r} already exists", file=sys.stderr)
            return 1
        user = m.User(username=args.username, is_local=True, password_hash=hash_password(password),
                      display_name="Local administrator")
        db.add(user)
        await db.flush()
        db.add(m.UserRole(user_id=user.id, role_id=roles["admin"].id, source="manual"))
        print(f"created local administrator {args.username!r} (requires LOCAL_AUTH_ENABLED=true to log in)")
        return 0

    return asyncio.run(_with_db(run))


def cmd_grant_role(args: argparse.Namespace) -> int:
    from app import models as m
    from app.services.authz import ensure_builtin_roles

    async def run(db) -> int:  # noqa: ANN001
        roles = await ensure_builtin_roles(db)
        role = roles.get(args.role) or (await db.execute(select(m.Role).where(m.Role.name == args.role))).scalar_one_or_none()
        if role is None:
            print(f"unknown role {args.role!r}", file=sys.stderr)
            return 1
        user = (await db.execute(select(m.User).where(func.lower(m.User.username) == args.username.lower()))).scalar_one_or_none()
        if user is None:
            print(f"unknown user {args.username!r} (OIDC users appear after their first login)", file=sys.stderr)
            return 1
        if await db.get(m.UserRole, (user.id, role.id)) is None:
            db.add(m.UserRole(user_id=user.id, role_id=role.id, source="manual"))
        print(f"granted {role.name!r} to {user.username!r}")
        return 0

    return asyncio.run(_with_db(run))


def cmd_rotate_keys(_: argparse.Namespace) -> int:
    from app import models as m
    from app.security.crypto import aad_for, get_box

    targets = [
        (m.Connection, "connections", [("password_enc", "password")]),
        (m.Folder, "folders", [("password_enc", "password")]),
        (m.Credential, "credentials", [("password_enc", "password"), ("private_key_enc", "private_key"),
                                       ("passphrase_enc", "passphrase"), ("attributes_enc", "attributes"),
                                       ("secret_attributes_enc", "secret_attributes")]),
        (m.PersonalAccount, "personal_accounts", [("password_enc", "password")]),
        (m.SecretProvider, "secret_providers", [("secret_config_enc", "secret_config")]),
        (m.SecretMapping, "secret_mappings", [("pending_remote_enc", "pending")]),
        (m.WebSession, "web_sessions", [("oidc_id_token_enc", "id_token"), ("oidc_refresh_token_enc", "refresh_token")]),
    ]

    async def run(db) -> int:  # noqa: ANN001
        box = get_box()
        count = 0
        for model, table, cols in targets:
            for row in (await db.execute(select(model))).scalars():
                for attr, column in cols:
                    blob = getattr(row, attr)
                    if blob and box.needs_rotation(blob):
                        aad = aad_for(table, column, row.id)
                        setattr(row, attr, box.encrypt(box.decrypt_bytes(blob, aad), aad))
                        count += 1
        print(f"re-encrypted {count} value(s) with key {box.keyring.active_kid!r}")
        return 0

    return asyncio.run(_with_db(run))


def cmd_openapi(args: argparse.Namespace) -> int:
    from app.main import create_app

    spec = create_app(_settings()).openapi()
    text = json.dumps(spec, indent=2, sort_keys=False)
    if args.file:
        with open(args.file, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    else:
        print(text)
    return 0


def cmd_ftp_test(args: argparse.Namespace) -> int:
    """Run the same FTP/FTPS sequence the file manager uses and print every step."""
    import os
    import time

    from app.errors import AppError
    from app.services.sessions.files.ftp import connect_ftp

    password = os.environ.get("FTP_TEST_PASSWORD") or getpass.getpass(f"Password for {args.user}: ")
    tls = None if args.tls == "none" else args.tls
    port = args.port or (990 if tls == "implicit" else 21)
    print(f"Testing {args.tls} FTP to {args.host}:{port} as {args.user} "
          f"(passive={not args.active}, verify_tls={args.verify}, timeout={args.timeout}s)")
    try:
        ftp = connect_ftp(host=args.host, port=port, username=args.user, password=password, tls=tls,
                          tls_verify=args.verify, ca_bundle=None, passive=not args.active,
                          trust_pasv_address=args.trust_pasv, encoding="utf-8", timeout=args.timeout, log=print)
    except AppError as exc:
        print(f"FAIL {exc.detail}")
        return 1
    try:
        print(f"ok   PWD: {ftp.pwd()}")
        if not args.active:
            host, dport = ftp.makepasv()
            print(f"ok   PASV: server announces data port {dport} on {host} "
                  f"(connecting to {'that address' if args.trust_pasv else args.host})")
        lines: list[str] = []
        start = time.monotonic()
        ftp.retrlines("LIST", lines.append)
        print(f"ok   LIST over the data connection: {len(lines)} entries in {time.monotonic() - start:.1f}s")
        for line in lines[:10]:
            print(f"       {line}")
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic tool
        print(f"FAIL data connection / listing: {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            ftp.quit()
        except Exception:  # noqa: S110
            pass
    print("All steps succeeded.")
    return 0


def cmd_check_config(_: argparse.Namespace) -> int:
    from app import db as dbmod
    from app.security.crypto import init_crypto

    s = _settings()
    ok = True
    try:
        init_crypto(s.ENCRYPTION_KEYS, s.ENCRYPTION_ACTIVE_KEY_ID)
        print("encryption keys: ok")
    except Exception as exc:  # noqa: BLE001
        print(f"encryption keys: FAILED ({exc})")
        ok = False

    async def check() -> None:
        nonlocal ok
        dbmod.init_engine(s)
        db_ok = await dbmod.ping()
        print(f"database {s.DB_HOST}:{s.DB_PORT}/{s.DB_NAME}: {'ok' if db_ok else 'UNREACHABLE'}")
        ok = ok and db_ok
        await dbmod.dispose_engine()
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(s.GUACD_HOST, s.GUACD_PORT), 3)
            w.close()
            print(f"guacd {s.GUACD_HOST}:{s.GUACD_PORT}: ok")
        except (TimeoutError, OSError) as exc:
            print(f"guacd {s.GUACD_HOST}:{s.GUACD_PORT}: UNREACHABLE ({exc})")

    asyncio.run(check())
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate-key").set_defaults(fn=cmd_generate_key)
    sub.add_parser("migrate").set_defaults(fn=cmd_migrate)
    p = sub.add_parser("create-admin")
    p.add_argument("username")
    p.add_argument("--password", help="non-interactive (avoid: ends up in shell history)")
    p.set_defaults(fn=cmd_create_admin)
    p = sub.add_parser("grant-role")
    p.add_argument("username")
    p.add_argument("role")
    p.set_defaults(fn=cmd_grant_role)
    sub.add_parser("rotate-keys").set_defaults(fn=cmd_rotate_keys)
    p = sub.add_parser("openapi")
    p.add_argument("file", nargs="?")
    p.set_defaults(fn=cmd_openapi)
    sub.add_parser("check-config").set_defaults(fn=cmd_check_config)
    p = sub.add_parser("ftp-test", help="diagnose an FTP/FTPS connection step by step")
    p.add_argument("host")
    p.add_argument("--port", type=int, default=0, help="default: 990 for implicit, 21 otherwise")
    p.add_argument("--user", default="anonymous")
    p.add_argument("--tls", choices=["implicit", "explicit", "none"], default="implicit")
    p.add_argument("--verify", action="store_true", help="validate the TLS certificate (default: off)")
    p.add_argument("--active", action="store_true", help="active instead of passive mode")
    p.add_argument("--trust-pasv", action="store_true", help="use the address from the PASV reply")
    p.add_argument("--timeout", type=float, default=15.0)
    p.set_defaults(fn=cmd_ftp_test)
    args = parser.parse_args(argv)
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
