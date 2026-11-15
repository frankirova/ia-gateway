"""Docker ``timestamps=1`` prefixes."""

from __future__ import annotations

import re
from datetime import datetime, timezone

__all__ = ["parse_timestamp", "split_timestamp"]

_TIMESTAMP_PREFIX = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2}))"
    r"(?P<separator>$|\s)"
)

_TIMESTAMP_PARTS = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?"
    r"(?P<offset>Z|[+-]\d{2}:\d{2})$"
)


def split_timestamp(line: str) -> tuple[str | None, str]:
    """Split the leading RFC 3339 timestamp off a log line.

    Returns ``(timestamp_text, remainder)``. When the line carries no
    timestamp, ``timestamp_text`` is ``None`` and the line is returned as is.
    """
    match = _TIMESTAMP_PREFIX.match(line)
    if match is None:
        return None, line
    return match.group("stamp"), line[match.end() :]


def parse_timestamp(text: str | None) -> datetime | None:
    """Parse a Docker timestamp into an aware UTC datetime, or ``None``."""
    if not text:
        return None
    match = _TIMESTAMP_PARTS.match(text)
    if match is None:
        return None
    fraction = (match.group("fraction") or "0").ljust(6, "0")[:6]
    offset = "+00:00" if match.group("offset").upper() == "Z" else match.group("offset")
    try:
        parsed = datetime.fromisoformat(
            f"{match.group('base')}.{fraction}{offset}"
        )
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc)
