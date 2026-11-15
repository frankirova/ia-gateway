"""Deterministic collection: list containers, read logs, build one digest."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Sequence

from tasks.host_report.ansi import strip_ansi
from tasks.host_report.clustering import (
    SEVERITY_ERROR,
    SEVERITY_INFO,
    SEVERITY_WARNING,
    SAMPLE_LIMIT,
    ClusterAccumulator,
    classify_severity,
)
from tasks.host_report.docker_api import DockerClient, DockerError
from tasks.host_report.hoststate import (
    HostFinding,
    HostState,
    host_findings,
    parse_host_state,
)
from tasks.host_report.probe import ProbeResult, probe_all, probe_findings
from tasks.host_report.redaction import redact
from tasks.host_report.remote_bundle import (
    BundleDockerClient,
    BundleUnavailable,
    fetch_bundle,
)
from tasks.host_report.settings import ContainerLimits, Settings
from tasks.host_report.timestamps import parse_timestamp, split_timestamp
from tasks.host_report.window import Window, format_utc_micro, window_for

__all__ = [
    "MAX_OVERFETCH_TAIL",
    "build_report",
    "collect",
    "requested_tail",
    "summarise_container",
    "truncation_record",
]

RUNNING_STATE = "running"

#: Hard ceiling on the ``tail`` sent to Docker, whatever ``max_lines`` and the
#: over-fetch factor ask for: a noisy service must not turn into a huge download.
MAX_OVERFETCH_TAIL = 20000

#: Highest severity is retained first when the line cap forces a choice.
_RETENTION_ORDER = (SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO)


def truncation_record(
    container: str | None, kind: str, limit: int, observed: int, message: str
) -> dict[str, Any]:
    """One recorded cap hit; caps are never applied silently."""
    return {
        "container": container,
        "kind": kind,
        "limit": limit,
        "observed": observed,
        "message": message,
    }


def requested_tail(max_lines: int, overfetch: int) -> int:
    """``tail`` sent to Docker: ``max_lines * overfetch``, clamped."""
    return min(max_lines * overfetch, MAX_OVERFETCH_TAIL)


def container_name(entry: dict[str, Any]) -> str:
    """Best-effort display name for a listing or inspect document."""
    name = entry.get("Name")
    if isinstance(name, str) and name.strip():
        return name.lstrip("/")
    names = entry.get("Names")
    if isinstance(names, list) and names:
        first = names[0]
        if isinstance(first, str):
            return first.lstrip("/")
    identifier = entry.get("Id")
    return identifier[:12] if isinstance(identifier, str) else "unknown"


def _listing_sort_key(entry: dict[str, Any]) -> tuple[str, str]:
    return container_name(entry).lower(), str(entry.get("Id", ""))


def _is_ignored(text: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(text) for pattern in patterns)


@dataclass(frozen=True)
class _Candidate:
    """One analysed line, before the line cap is applied."""

    text: str
    seen: datetime | None
    severity: str
    order: int


def _severity_counts(candidates: list[_Candidate]) -> dict[str, int]:
    counts = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 0, SEVERITY_INFO: 0}
    for candidate in candidates:
        counts[candidate.severity] += 1
    return counts


def _retain(
    candidates: list[_Candidate], max_lines: int
) -> tuple[list[_Candidate], dict[str, int]]:
    """Choose which lines survive the cap: errors, then warnings, then info.

    Every error and warning line is kept before any informational line, so a
    cap can never trade a real event for routine chatter. Retained lines are
    returned in stream order, so clustering and ordering stay unchanged when
    the cap was not hit.
    """
    if len(candidates) <= max_lines:
        return candidates, {
            "kept": len(candidates),
            "errors_kept": 0,
            "warnings_kept": 0,
            "errors_dropped": 0,
            "warnings_dropped": 0,
            "info_dropped": 0,
        }

    kept: list[_Candidate] = []
    remaining = max_lines
    for severity in _RETENTION_ORDER:
        if remaining <= 0:
            break
        taken = [c for c in candidates if c.severity == severity][:remaining]
        remaining -= len(taken)
        kept.extend(taken)
    kept.sort(key=lambda candidate: candidate.order)

    before = _severity_counts(candidates)
    after = _severity_counts(kept)
    return kept, {
        "kept": len(kept),
        "errors_kept": after[SEVERITY_ERROR],
        "warnings_kept": after[SEVERITY_WARNING],
        "errors_dropped": before[SEVERITY_ERROR] - after[SEVERITY_ERROR],
        "warnings_dropped": before[SEVERITY_WARNING] - after[SEVERITY_WARNING],
        "info_dropped": before[SEVERITY_INFO] - after[SEVERITY_INFO],
    }


def _summarise_lines(
    lines: list[str],
    settings: Settings,
    *,
    name: str,
    truncated: bool,
    limits: ContainerLimits,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    truncations: list[dict[str, Any]] = []
    ignored_lines = 0
    candidates: list[_Candidate] = []

    for raw_line in lines:
        stamp_text, content = split_timestamp(raw_line)
        content = strip_ansi(content)
        if _is_ignored(content, settings.ignore_patterns):
            ignored_lines += 1
            continue
        safe_line = redact(content)
        candidates.append(
            _Candidate(
                text=safe_line,
                seen=parse_timestamp(stamp_text),
                severity=classify_severity(safe_line),
                order=len(candidates),
            )
        )

    kept, retention = _retain(candidates, limits.max_lines)
    sampled = len(candidates) > limits.max_lines

    accumulator = ClusterAccumulator()
    error_lines = 0
    warning_lines = 0
    for candidate in kept:
        if candidate.severity == SEVERITY_ERROR:
            error_lines += 1
        elif candidate.severity == SEVERITY_WARNING:
            warning_lines += 1
        accumulator.add(candidate.text, candidate.seen, candidate.severity)

    if truncated:
        truncations.append(
            truncation_record(
                name,
                "lines",
                limits.max_lines,
                len(lines) + 1,
                "line cap reached; the rest of the log stream was not read",
            )
        )
    elif sampled:
        truncations.append(
            truncation_record(
                name,
                "lines",
                limits.max_lines,
                len(lines),
                "line cap reached; error and warning lines were kept before "
                "informational ones",
            )
        )
    if sampled:
        sampling = truncation_record(
            name,
            "sampling",
            limits.max_lines,
            len(lines),
            "line cap reached; some informational lines were not analysed",
        )
        sampling.update(
            {
                "lines_read": len(lines),
                "kept": retention["kept"],
                "errors_kept": retention["errors_kept"],
                "warnings_kept": retention["warnings_kept"],
                "errors_dropped": retention["errors_dropped"],
                "warnings_dropped": retention["warnings_dropped"],
                "info_dropped": retention["info_dropped"],
            }
        )
        truncations.append(sampling)

    ranked = accumulator.ranked()
    cluster_total = len(ranked)
    if cluster_total > limits.max_clusters:
        truncations.append(
            truncation_record(
                name,
                "clusters",
                limits.max_clusters,
                cluster_total,
                "cluster cap reached; only the highest-count clusters are kept",
            )
        )
        ranked = ranked[: limits.max_clusters]

    clusters = [
        {
            "template": redact(cluster.template),
            "count": cluster.count,
            "first_seen": (
                format_utc_micro(cluster.first_seen)
                if cluster.first_seen is not None
                else None
            ),
            "last_seen": (
                format_utc_micro(cluster.last_seen)
                if cluster.last_seen is not None
                else None
            ),
            "severity": cluster.severity,
            "sample": redact(cluster.sample[:SAMPLE_LIMIT]),
        }
        for cluster in ranked
    ]

    return (
        {
            "lines_read": len(lines),
            "ignored_lines": ignored_lines,
            "total_lines": len(kept),
            "error_lines": error_lines,
            "warning_lines": warning_lines,
            "clusters": clusters,
        },
        truncations,
    )


def summarise_container(
    client: DockerClient,
    listing_entry: dict[str, Any],
    settings: Settings,
    window: Window,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Summarise one container; raises :class:`DockerError` on any failure."""
    identifier = str(listing_entry.get("Id") or "")
    if not identifier:
        raise DockerError("container listing entry has no id")
    document = client.inspect_container(identifier)

    name = container_name(document) or container_name(listing_entry)
    configuration = document.get("Config")
    image = ""
    if isinstance(configuration, dict):
        image_value = configuration.get("Image")
        if isinstance(image_value, str):
            image = image_value
    if not image:
        listing_image = listing_entry.get("Image")
        image = listing_image if isinstance(listing_image, str) else ""

    state_document = document.get("State")
    state_document = state_document if isinstance(state_document, dict) else {}
    state = state_document.get("Status")
    state = state if isinstance(state, str) and state else "unknown"
    restarts_value = document.get("RestartCount")
    restart_count = int(restarts_value) if isinstance(restarts_value, int) else 0
    exit_code: int | None = None
    if state != RUNNING_STATE:
        raw_exit = state_document.get("ExitCode")
        if isinstance(raw_exit, int):
            exit_code = raw_exit

    limits = settings.limits_for(name)
    tail = requested_tail(limits.max_lines, settings.overfetch)
    lines, truncated = client.read_log_lines(
        identifier,
        since=window.since_epoch,
        tail=tail,
        max_lines=tail,
    )
    counts, truncations = _summarise_lines(
        lines, settings, name=name, truncated=truncated, limits=limits
    )

    summary: dict[str, Any] = {
        "name": name,
        "image": image,
        "state": state,
        "restart_count": restart_count,
        "exit_code": exit_code,
    }
    summary.update(counts)
    return summary, truncations


def build_report(
    settings: Settings,
    window: Window,
    containers: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    truncations: list[dict[str, Any]],
    *,
    host_document: dict[str, Any] | None = None,
    state: HostState | None = None,
    findings: Sequence[Any] = (),
    probes: Sequence[ProbeResult] = (),
    unreachable: str | None = None,
) -> dict[str, Any]:
    """Assemble the digest document printed on stdout.

    The host-state, endpoint and unreachable keys appear only when this run has
    something to say about them, so a local report keeps its exact shape.
    """
    ordered = sorted(containers, key=lambda entry: (-int(entry["error_lines"]), entry["name"]))
    ordered_truncations = sorted(
        truncations,
        key=lambda item: (
            item["container"] or "",
            item["kind"],
            item["limit"],
        ),
    )
    document = {
        "schema_version": 1,
        "host": settings.host,
        "window": {
            "since": window.since_iso,
            "until": window.until_iso,
            "duration": settings.since_label,
        },
        "containers": ordered,
        "skipped": sorted(skipped, key=lambda item: item["container"]),
        "truncations": ordered_truncations,
    }
    if host_document and state is not None and (
        state.cpu_count is not None
        or state.filesystems
        or state.uptime_seconds is not None
        or state.mem_total_kb is not None
    ):
        document["host_state"] = dict(host_document)
    if findings:
        document["host_findings"] = [
            {
                "code": item.code,
                "severity": item.severity,
                "message": item.message,
                "kind": item.kind,
                "values": dict(item.values),
            }
            for item in findings
        ]
    if probes:
        document["probes"] = [
            {
                "name": item.name,
                "url": item.url,
                "ok": item.ok,
                "status": item.status,
                "latency_ms": item.latency_ms,
                "reason": item.reason,
            }
            for item in probes
        ]
    if unreachable:
        document["unreachable"] = unreachable
    return document


def selection_findings(missing: Sequence[str]) -> list[HostFinding]:
    """A configured container that is not running is a finding, never silence.

    The remote host owns the allowlist, so a name it reports as missing is a
    service that stopped, crashed or never came back. Without this, a dead
    service is indistinguishable from a quiet window.
    """
    return [
        HostFinding(
            code="expected_container_missing",
            severity="high",
            message=f"expected container {name!r} is not running",
            kind="service",
            values={"name": name},
        )
        for name in missing
    ]


def _open_source(settings: Settings):
    """Return ``(client, unreachable_reason, host_document, selection_missing)``."""
    if settings.target != "ssh":
        return DockerClient(settings.docker_url, timeout=settings.timeout), None, {}, ()
    try:
        bundle = fetch_bundle(
            ssh_target=settings.ssh_target,
            timeout_seconds=settings.ssh_timeout,
            ssh_binary=settings.ssh_binary,
        )
    except BundleUnavailable as error:
        return None, error.reason, {}, ()
    return (
        BundleDockerClient(bundle),
        None,
        dict(bundle.host),
        bundle.selection_missing,
    )


def collect(settings: Settings, *, now_epoch: float | None = None) -> dict[str, Any]:
    """Collect every running container's digest.

    :class:`DockerError` from the container listing is fatal (raise to the
    caller); any per-container failure is recorded in ``skipped``. When the
    remote source is unreachable the digest still carries the fact, the host
    state it could not read and every endpoint probe, so the report can say so.
    """
    window = window_for(settings.since_seconds, now_epoch=now_epoch)
    client, unreachable, host_document, selection_missing = _open_source(settings)

    state = parse_host_state(host_document)
    findings = list(host_findings(state))
    findings.extend(selection_findings(selection_missing))
    probes = probe_all(settings.endpoints)
    findings.extend(probe_findings(probes))

    if client is None:
        return build_report(
            settings,
            window,
            [],
            [],
            [],
            host_document=host_document,
            state=state,
            findings=findings,
            probes=probes,
            unreachable=unreachable,
        )

    listing = sorted(client.list_containers(), key=_listing_sort_key)

    skipped: list[dict[str, Any]] = []
    truncations: list[dict[str, Any]] = []

    if len(listing) > settings.max_containers:
        dropped = listing[settings.max_containers :]
        truncations.append(
            truncation_record(
                None,
                "containers",
                settings.max_containers,
                len(listing),
                "container cap reached; the remaining containers were not collected",
            )
        )
        for entry in dropped:
            skipped.append(
                {
                    "container": container_name(entry),
                    "reason": (
                        f"container cap of {settings.max_containers} reached"
                    ),
                }
            )
        listing = listing[: settings.max_containers]

    summaries: list[dict[str, Any]] = []
    for entry in listing:
        try:
            summary, container_truncations = summarise_container(
                client, entry, settings, window
            )
        except DockerError as error:
            skipped.append(
                {"container": container_name(entry), "reason": str(error)}
            )
            continue
        summaries.append(summary)
        truncations.extend(container_truncations)

    return build_report(
        settings,
        window,
        summaries,
        skipped,
        truncations,
        host_document=host_document,
        state=state,
        findings=findings,
        probes=probes,
    )
