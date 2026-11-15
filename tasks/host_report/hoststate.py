"""Deterministic host state for a remote host, without a model and without network.

The remote collector already reduced the host to plain numbers (load, uptime,
memory, filesystem usage). This module parses that document, decides which
thresholds are exceeded, and renders a bounded block of facts.

The model never decides whether a disk is full: :func:`host_findings` owns every
severity here, and the report reuses its messages verbatim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

__all__ = [
    "DEFAULT_DISK_CRITICAL",
    "DEFAULT_DISK_WARNING",
    "DEFAULT_LOAD_WARNING_RATIO",
    "DEFAULT_MEM_AVAILABLE_WARNING_PERCENT",
    "Filesystem",
    "HostFinding",
    "HostState",
    "host_findings",
    "host_is_quiet",
    "parse_host_state",
    "render_host_state",
]

DEFAULT_DISK_WARNING = 85
DEFAULT_DISK_CRITICAL = 95
DEFAULT_MEM_AVAILABLE_WARNING_PERCENT = 10
DEFAULT_LOAD_WARNING_RATIO = 1.0

_GIB = 1024**3
_MIB = 1024**2
_TRUNCATION = "…"
_SEVERITY_ORDER = {"critical": 0, "warning": 1}


@dataclass(frozen=True)
class Filesystem:
    """One mount point as the remote host described it."""

    mount: str
    total_bytes: int
    used_bytes: int
    avail_bytes: int
    used_percent: int


@dataclass(frozen=True)
class HostState:
    """The remote host's state; every field may be absent."""

    cpu_count: int | None = None
    load: tuple[float, float, float] | None = None
    uptime_seconds: int | None = None
    mem_total_kb: int | None = None
    mem_available_kb: int | None = None
    swap_total_kb: int | None = None
    swap_free_kb: int | None = None
    filesystems: tuple[Filesystem, ...] = ()


@dataclass(frozen=True)
class HostFinding:
    """A fact the report states verbatim; it is never model text.

    ``kind`` and ``values`` carry the same fact in a form the message can
    translate, so a Spanish message never has to parse English prose back.
    """

    code: str
    severity: str
    message: str
    kind: str = ""
    values: Mapping[str, Any] = field(default_factory=dict)


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _parse_load(value: Any) -> tuple[float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    parsed = [_optional_float(item) for item in value]
    if any(item is None for item in parsed):
        return None
    return (parsed[0], parsed[1], parsed[2])  # type: ignore[return-value]


def _parse_filesystem(entry: Any) -> Filesystem | None:
    if not isinstance(entry, dict):
        return None
    mount = entry.get("mount")
    if not isinstance(mount, str) or not mount:
        return None
    total = _optional_int(entry.get("total_bytes"))
    used = _optional_int(entry.get("used_bytes"))
    avail = _optional_int(entry.get("avail_bytes"))
    percent = _optional_int(entry.get("used_percent"))
    if total is None or percent is None:
        return None
    used = 0 if used is None else max(0, used)
    avail = 0 if avail is None else max(0, avail)
    return Filesystem(
        mount=mount,
        total_bytes=max(0, total),
        used_bytes=used,
        avail_bytes=avail,
        used_percent=percent,
    )


def parse_host_state(document: dict[str, Any] | None) -> HostState:
    """Parse the bundle's ``host`` object; missing or wrong types stay empty."""
    if not isinstance(document, dict):
        return HostState()

    entries = document.get("filesystems")
    filesystems: list[Filesystem] = []
    if isinstance(entries, (list, tuple)):
        for entry in entries:
            parsed = _parse_filesystem(entry)
            if parsed is not None:
                filesystems.append(parsed)

    return HostState(
        cpu_count=_optional_int(document.get("cpu_count")),
        load=_parse_load(document.get("load")),
        uptime_seconds=_optional_int(document.get("uptime_seconds")),
        mem_total_kb=_optional_int(document.get("mem_total_kb")),
        mem_available_kb=_optional_int(document.get("mem_available_kb")),
        swap_total_kb=_optional_int(document.get("swap_total_kb")),
        swap_free_kb=_optional_int(document.get("swap_free_kb")),
        filesystems=tuple(filesystems),
    )


def host_findings(
    state: HostState,
    *,
    disk_warning: int = DEFAULT_DISK_WARNING,
    disk_critical: int = DEFAULT_DISK_CRITICAL,
    mem_available_warning_percent: int = DEFAULT_MEM_AVAILABLE_WARNING_PERCENT,
    load_warning_ratio: float = DEFAULT_LOAD_WARNING_RATIO,
) -> tuple[HostFinding, ...]:
    """Return every exceeded threshold, critical before warning, then by code."""
    findings: list[HostFinding] = []

    for filesystem in state.filesystems:
        if filesystem.used_percent >= disk_critical:
            severity = "critical"
        elif filesystem.used_percent >= disk_warning:
            severity = "warning"
        else:
            continue
        findings.append(
            HostFinding(
                code=f"disk:{filesystem.mount}",
                severity=severity,
                message=f"disk {filesystem.mount} at {filesystem.used_percent}% used",
                kind="disk",
                values={
                    "mount": filesystem.mount,
                    "used_percent": filesystem.used_percent,
                    "avail_bytes": filesystem.avail_bytes,
                    "total_bytes": filesystem.total_bytes,
                },
            )
        )

    total = state.mem_total_kb
    available = state.mem_available_kb
    if total is not None and available is not None and total > 0:
        available_percent = available * 100 // total
        if available_percent < mem_available_warning_percent:
            findings.append(
                HostFinding(
                    code="memory:available",
                    severity="warning",
                    message=f"memory available {available_percent}%",
                    kind="memory",
                    values={"available_percent": available_percent},
                )
            )

    if state.load is not None and state.cpu_count is not None:
        threshold = state.cpu_count * load_warning_ratio
        if threshold > 0 and state.load[0] > threshold:
            findings.append(
                HostFinding(
                    code="load:1m",
                    severity="warning",
                    message=(
                        f"load {state.load[0]:.2f} above {state.cpu_count} cpu"
                    ),
                    kind="load",
                    values={"load1": state.load[0], "cpu_count": state.cpu_count},
                )
            )

    return tuple(
        sorted(findings, key=lambda item: (_SEVERITY_ORDER.get(item.severity, 2), item.code))
    )


def host_is_quiet(state: HostState) -> bool:
    """True when no threshold is exceeded, so the caller may skip the model."""
    return not host_findings(state)


def _gib(kilobytes: int) -> float:
    return kilobytes * 1024 / _GIB


def _gib_from_bytes(value: int) -> float:
    return value / _GIB


def _human_uptime(seconds: int) -> str:
    days, remainder = divmod(max(0, seconds), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_disk(filesystem: Filesystem) -> str:
    size = _gib_from_bytes(filesystem.total_bytes)
    free = _gib_from_bytes(filesystem.avail_bytes)
    if filesystem.avail_bytes < 128 * _MIB:
        free_text = f"{filesystem.avail_bytes / _MIB:.0f} MiB"
    else:
        free_text = f"{free:.1f} GiB"
    return f"disk {filesystem.mount} {filesystem.used_percent}% of {size:.1f} GiB ({free_text} free)"


def render_host_state(state: HostState, *, limit_chars: int) -> str:
    """Render one line per known fact, truncated at ``limit_chars`` characters."""
    if limit_chars <= 0:
        return ""

    lines: list[str] = []

    if state.cpu_count is not None and state.load is not None:
        one, five, fifteen = state.load
        lines.append(f"cpu {state.cpu_count} · load {one:.2f} {five:.2f} {fifteen:.2f}")
    elif state.cpu_count is not None:
        lines.append(f"cpu {state.cpu_count}")
    elif state.load is not None:
        one, five, fifteen = state.load
        lines.append(f"load {one:.2f} {five:.2f} {fifteen:.2f}")

    if state.uptime_seconds is not None:
        lines.append(f"uptime {_human_uptime(state.uptime_seconds)}")

    if state.mem_total_kb is not None and state.mem_total_kb > 0:
        total_gib = _gib(state.mem_total_kb)
        if state.mem_available_kb is not None:
            available_gib = _gib(state.mem_available_kb)
            used_gib = max(0.0, total_gib - available_gib)
            percent = int(state.mem_available_kb * 100 // state.mem_total_kb)
            lines.append(
                f"mem {used_gib:.1f}/{total_gib:.1f} GiB used ({percent}% available)"
            )
        else:
            lines.append(f"mem {total_gib:.1f} GiB total")

    if state.swap_total_kb is not None and state.swap_total_kb > 0:
        free = state.swap_free_kb or 0
        used = _gib(max(0, state.swap_total_kb - free))
        lines.append(f"swap {used:.1f}/{_gib(state.swap_total_kb):.1f} GiB used")

    for filesystem in state.filesystems:
        lines.append(_format_disk(filesystem))

    rendered: list[str] = []
    used_chars = 0
    for line in lines:
        candidate = len(line) + (1 if rendered else 0)
        if used_chars + candidate > limit_chars:
            break
        rendered.append(line)
        used_chars += candidate
    if lines and not rendered:
        # Nothing fits: say so instead of splitting a line in the middle of a value.
        return _TRUNCATION[:limit_chars] if limit_chars else ""
    text = "\n".join(rendered)

    if len(text) > limit_chars:
        text = text[: max(0, limit_chars - 1)] + _TRUNCATION
    return text


def summarise_host(state: HostState) -> dict[str, Any]:
    """The machine-readable host summary the digest carries."""
    document: dict[str, Any] = {
        "cpu_count": state.cpu_count,
        "load": list(state.load) if state.load is not None else None,
        "uptime_seconds": state.uptime_seconds,
    }
    if state.mem_total_kb is not None and state.mem_total_kb > 0:
        document["memory_used_percent"] = (
            int((state.mem_total_kb - (state.mem_available_kb or 0)) * 100 // state.mem_total_kb)
        )
    document["filesystems"] = [
        {
            "mount": item.mount,
            "total_bytes": item.total_bytes,
            "used_bytes": item.used_bytes,
            "avail_bytes": item.avail_bytes,
            "used_percent": item.used_percent,
        }
        for item in state.filesystems
    ]
    return document


#: Kept for symmetry with the probe module's MiB-based rendering.
MIB = _MIB
