"""Time window: ``--since`` durations and the epoch Docker expects."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

__all__ = [
    "DURATION_PATTERN",
    "InvalidDuration",
    "Window",
    "format_utc",
    "format_utc_micro",
    "parse_duration",
    "window_for",
]

#: Accepted duration shapes: an integer magnitude and one unit, e.g. ``24h``.
DURATION_PATTERN = re.compile(r"^(?P<amount>\d{1,6})(?P<unit>[smhd])$")

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


class InvalidDuration(ValueError):
    """Raised when a ``--since`` value is not a supported duration."""


def parse_duration(text: str) -> int:
    """Return the duration in seconds.

    Only ``<amount><unit>`` with unit ``s``, ``m``, ``h`` or ``d`` is accepted;
    anything else (floats, negative values, compound durations, upper case) is
    rejected so the window stays predictable.
    """
    if not isinstance(text, str):
        raise InvalidDuration(f"invalid duration {text!r}: expected <amount><unit>")
    match = DURATION_PATTERN.match(text)
    if match is None:
        raise InvalidDuration(
            f"invalid duration {text!r}: expected a whole number and one unit "
            "among s, m, h, d (for example 24h or 7d)"
        )
    amount = int(match.group("amount"))
    if amount <= 0:
        raise InvalidDuration(
            f"invalid duration {text!r}: the amount must be greater than zero"
        )
    return amount * _UNIT_SECONDS[match.group("unit")]


@dataclass(frozen=True)
class Window:
    """The collection window, in both epoch seconds and ISO 8601 UTC."""

    since_epoch: int
    until_epoch: int
    duration_seconds: int

    @property
    def since_iso(self) -> str:
        return format_utc(datetime.fromtimestamp(self.since_epoch, timezone.utc))

    @property
    def until_iso(self) -> str:
        return format_utc(datetime.fromtimestamp(self.until_epoch, timezone.utc))


def window_for(duration_seconds: int, *, now_epoch: float | None = None) -> Window:
    """Build the window that ends ``now`` and starts ``duration_seconds`` earlier."""
    if duration_seconds <= 0:
        raise InvalidDuration("the window duration must be greater than zero")
    until_epoch = int(time.time() if now_epoch is None else now_epoch)
    return Window(
        since_epoch=until_epoch - int(duration_seconds),
        until_epoch=until_epoch,
        duration_seconds=int(duration_seconds),
    )


def format_utc(moment: datetime) -> str:
    """Format a timestamp as ISO 8601 in UTC with a ``Z`` suffix."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_utc_micro(moment: datetime) -> str:
    """Format a timestamp as ISO 8601 in UTC, keeping microseconds."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
