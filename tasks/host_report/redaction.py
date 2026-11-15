"""Redaction of obvious credentials before anything leaves the process.

Redacted text keeps the key (or header name) and replaces only the value with
``[redacted]`` so the digest stays readable and the secret never survives.
"""

from __future__ import annotations

import re

__all__ = ["REDACTED", "redact"]

REDACTED = "[redacted]"

# ``password=…``, ``passwd: …``, ``token=…``, ``secret=…``, ``api_key=…``;
# the key quotes of JSON-ish text are preserved around the masked value.
_SECRET_ASSIGNMENT = re.compile(
    r"""(?ix)
    (?P<prefix>
        (?P<key>\b(?:password|passwd|token|secret|api[_-]?key)\b)
        (?P<key_quote>["']?)
        \s*[:=]\s*
        (?P<value_quote>["']?)
    )
    (?P<value>[^\s"'`,;&]+)
    """
)

_BEARER_HEADER = re.compile(
    r"(?i)(?P<prefix>\bAuthorization\s*:\s*Bearer\s+)(?P<value>[^\s,;]+)"
)

_API_KEY_HEADER = re.compile(r"(?i)(?P<prefix>\bX-Api-Key\s*:\s*)(?P<value>[^\s,;]+)")

# Long blob inside a URL query value, e.g. ``?data=QWxhZGRp...``.
_URL_PARAM_BLOB = re.compile(
    r"(?P<prefix>[?&][A-Za-z0-9_.%-]+=)(?P<value>[A-Za-z0-9+/=%_-]{32,})"
)

# Long blob as the whole value of a ``name: value`` header line.
_HEADER_BLOB = re.compile(
    r"(?m)(?P<prefix>^\s*[A-Za-z0-9_-]+\s*:\s*)(?P<value>[A-Za-z0-9+/=_.-]{32,})\s*$"
)

# Long hex identifiers and tokens (hashes, checksums, hex-encoded keys).
_LONG_HEX_BLOB = re.compile(r"(?<![0-9A-Za-z])(?P<value>[0-9a-fA-F]{32,})(?![0-9A-Za-z])")

# Long base64-ish tokens: 32+ characters, at least one digit. Slashes are
# excluded so ordinary file paths survive, and plain words are left alone.
_LONG_TOKEN_BLOB = re.compile(
    r"(?<![A-Za-z0-9+=_-])(?=[A-Za-z+=_-]*\d)(?P<value>[A-Za-z0-9+=_-]{32,})"
    r"(?![A-Za-z0-9+=_-])"
)

_PATTERNS: tuple[re.Pattern[str], ...] = (
    _BEARER_HEADER,
    _API_KEY_HEADER,
    _SECRET_ASSIGNMENT,
    _URL_PARAM_BLOB,
    _HEADER_BLOB,
    _LONG_HEX_BLOB,
    _LONG_TOKEN_BLOB,
)


def _mask(match: "re.Match[str]") -> str:
    prefix = match.groupdict().get("prefix") or ""
    return f"{prefix}{REDACTED}"


def redact(text: str) -> str:
    """Return ``text`` with credential-looking values masked."""
    if not text:
        return text
    redacted = text
    for pattern in _PATTERNS:
        redacted = pattern.sub(_mask, redacted)
    return redacted
