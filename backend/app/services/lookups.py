"""Lookups: connection fields that take their value from the credential store.

A field holds ``{{lookup:<credential name pattern>:<field>}}`` instead of a value, e.g.
``{{lookup:{hostname}_root:username}}``. When the connection is used:

1. placeholders in the pattern are replaced: ``{hostname}`` (the hostname of the opened
   connection), ``{name}`` (its name), ``{domain}`` (its search domain);
2. ``*`` matches any text; names are compared case-insensitively and must match completely;
3. exactly one credential the user may use must match, otherwise the lookup fails with a clear
   message (nothing is guessed);
4. the value is the credential's ``<field>``: ``username``, ``password``, ``domain``,
   ``private_key``, ``passphrase``, or one of its attributes (``port``, ``hostname``,
   ``description``, ``search_domain`` or any other attribute name).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Connection fields that may hold a lookup.
LOOKUP_FIELDS = (
    "username", "password", "domain", "private_key", "passphrase",
    "hostname", "port", "search_domain", "description",
)
PLACEHOLDERS = ("hostname", "name", "domain")

_LOOKUP = re.compile(r"^\{\{\s*lookup\s*:(?P<pattern>.+):(?P<field>[A-Za-z][A-Za-z0-9_]{0,63})\s*\}\}$")
_PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")


class LookupFailed(Exception):
    """The lookup cannot be resolved (placeholder missing, no match, several matches, no value)."""


@dataclass(frozen=True)
class Lookup:
    pattern: str
    field: str

    @property
    def expression(self) -> str:
        return f"{{{{lookup:{self.pattern}:{self.field}}}}}"


def parse_lookup(text: str | None) -> Lookup | None:
    """The lookup in ``text``, or None if ``text`` is a plain value."""
    if not text:
        return None
    m = _LOOKUP.match(text.strip())
    if not m:
        return None
    return Lookup(pattern=m.group("pattern").strip(), field=m.group("field"))


def is_lookup(text: str | None) -> bool:
    return bool(text) and text.strip().startswith("{{") and "lookup" in text


def validate_lookups(lookups: dict[str, str]) -> dict[str, str]:
    """Check a connection's lookups (field -> expression); raises ValueError with a readable message."""
    out: dict[str, str] = {}
    for field, expr in lookups.items():
        if field not in LOOKUP_FIELDS:
            raise ValueError(f"'{field}' cannot use a lookup (allowed: {', '.join(LOOKUP_FIELDS)})")
        lk = parse_lookup(expr)
        if lk is None:
            raise ValueError(f"{field}: '{expr}' is not a lookup; expected {{{{lookup:<name pattern>:<field>}}}}")
        unknown = sorted({p for p in _PLACEHOLDER.findall(lk.pattern) if p not in PLACEHOLDERS})
        if unknown:
            raise ValueError(f"{field}: unknown placeholder(s) {', '.join('{' + u + '}' for u in unknown)} "
                             f"(available: {', '.join('{' + p + '}' for p in PLACEHOLDERS)})")
        if field == "hostname" and "{hostname}" in lk.pattern:
            raise ValueError("hostname: a hostname lookup cannot use {hostname} itself")
        if field == "search_domain" and "{domain}" in lk.pattern:
            raise ValueError("search_domain: a domain lookup cannot use {domain} itself")
        out[field] = lk.expression
    return out


def substitute(pattern: str, values: dict[str, str | None]) -> str:
    """Replace the placeholders; a placeholder without a value fails the lookup."""
    def repl(m: re.Match[str]) -> str:
        name = m.group(1)
        value = values.get(name)
        if not value:
            raise LookupFailed(f"the placeholder {{{name}}} has no value for this connection")
        return value

    return _PLACEHOLDER.sub(repl, pattern)


def name_matcher(pattern: str) -> re.Pattern[str]:
    """``*`` = any text, everything else literal, case-insensitive, whole name."""
    return re.compile("^" + ".*".join(re.escape(part) for part in pattern.split("*")) + "$", re.IGNORECASE)


def find_credential(pattern: str, candidates: list[tuple[object, str]]) -> object:
    """The id of the single candidate whose name matches ``pattern`` (after substitution)."""
    rx = name_matcher(pattern)
    hits = [(cid, name) for cid, name in candidates if rx.match(name)]
    if not hits:
        raise LookupFailed(f"no credential named '{pattern}' that you may use")
    if len(hits) > 1:
        names = ", ".join(sorted(n for _, n in hits)[:5]) + (" …" if len(hits) > 5 else "")
        raise LookupFailed(f"'{pattern}' matches {len(hits)} credentials ({names}); make the name unique")
    return hits[0][0]
