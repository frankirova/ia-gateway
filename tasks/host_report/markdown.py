"""Markdown report: deterministic facts and model interpretation, kept apart.

``## Facts`` only ever contains counts and restarts computed by the collector.
``## Interpretation`` contains the model summary and findings, or a short note
saying why no interpretation is available. One HTML comment carries
machine-readable metadata; it never contains the application key or a raw
gateway response.
"""

from __future__ import annotations

import json
from typing import Any

from tasks.host_report.hoststate import parse_host_state, render_host_state
from tasks.host_report.probe import render_probes, results_from_document
from tasks.host_report.prompt import Render
from tasks.host_report.remote_bundle import remote_sentence

__all__ = [
    "NOTHING_TO_REPORT",
    "metadata_comment",
    "render_markdown",
]

NOTHING_TO_REPORT = (
    "Nothing to report: no container showed error lines, restarts, a non-zero "
    "exit code or an error/warning cluster in the window, so the model was not "
    "called."
)


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    return str(value)


def _facts(digest: dict[str, Any]) -> list[str]:
    containers = digest.get("containers") or []
    skipped = digest.get("skipped") or []
    truncations = digest.get("truncations") or []
    window = digest.get("window") or {}

    with_errors = sum(1 for entry in containers if int(entry.get("error_lines") or 0) > 0)
    with_restarts = sum(
        1 for entry in containers if int(entry.get("restart_count") or 0) > 0
    )
    with_exits = sum(
        1
        for entry in containers
        if isinstance(entry.get("exit_code"), int) and entry["exit_code"] != 0
    )
    error_lines = sum(int(entry.get("error_lines") or 0) for entry in containers)
    warning_lines = sum(int(entry.get("warning_lines") or 0) for entry in containers)
    total_lines = sum(int(entry.get("total_lines") or 0) for entry in containers)
    restarts = sum(int(entry.get("restart_count") or 0) for entry in containers)

    lines = [
        "## Facts",
        "",
        f"- Window: {window.get('since')} .. {window.get('until')} "
        f"({window.get('duration')})",
        f"- Containers collected: {len(containers)} "
        f"(skipped {len(skipped)}, truncations {len(truncations)})",
        f"- Activity: {with_errors} with errors, {with_restarts} with restarts, "
        f"{with_exits} with a non-zero exit code",
        f"- Lines: {error_lines} error, {warning_lines} warning of "
        f"{total_lines} analysed",
        f"- Restarts: {restarts}; non-zero exits: {with_exits}",
    ]
    unreachable = digest.get("unreachable")
    if isinstance(unreachable, str) and unreachable:
        lines.append(f"- Source: {remote_sentence(unreachable)}")
    lines.extend(
        [
            "",
            "| Container | State | Restarts | Exit | Lines | Errors | Warnings | Clusters |",
        ]
    )
    lines.append(
        "| --- | --- | --- | --- | --- | --- | --- | --- |"
    )
    for entry in containers:
        lines.append(
            f"| {_cell(entry.get('name'))} | {_cell(entry.get('state'))} "
            f"| {int(entry.get('restart_count') or 0)} "
            f"| {_cell(entry.get('exit_code'))} "
            f"| {int(entry.get('total_lines') or 0)} "
            f"| {int(entry.get('error_lines') or 0)} "
            f"| {int(entry.get('warning_lines') or 0)} "
            f"| {len(entry.get('clusters') or [])} |"
        )
    return lines


def _measured(digest: dict[str, Any]) -> list[str]:
    """Host state and endpoints, measured rather than interpreted."""
    lines: list[str] = []
    host_state = digest.get("host_state")
    if isinstance(host_state, dict):
        block = render_host_state(parse_host_state(host_state), limit_chars=800)
        if block:
            lines.extend(["## Host state", ""])
            lines.extend(f"- {line}" for line in block.splitlines())
            lines.append("")
    probes = results_from_document(digest.get("probes"))
    if probes:
        block = render_probes(probes, limit_chars=800)
        if block:
            lines.extend(["## Endpoints", ""])
            lines.extend(f"- {line}" for line in block.splitlines())
            lines.append("")
    return lines


def _interpretation(
    model_status: str,
    model_reason: str | None,
    interpretation: dict[str, Any] | None,
) -> list[str]:
    lines = ["## Interpretation", ""]
    if model_status == "skipped":
        lines.append(NOTHING_TO_REPORT)
        return lines
    if interpretation is None or model_status != "used":
        reason = model_reason or "the model reply was not usable"
        lines.append(f"Interpretation unavailable: {reason}.")
        return lines
    summary = interpretation.get("summary") or ""
    if summary.strip():
        lines.append(summary.strip())
        lines.append("")
    findings = interpretation.get("findings") or []
    if not findings:
        lines.append("No findings.")
        return lines
    for finding in findings:
        lines.append(
            f"- **{finding['container']}** ({finding['severity']}): "
            f"{finding['what']} Why: {finding['why']} Action: {finding['action']}"
        )
    return lines


def _metadata(
    digest: dict[str, Any],
    *,
    model_status: str,
    model_reason: str | None,
    render: Render | None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": 1,
        "host": digest.get("host"),
        "window": digest.get("window"),
        "report": "markdown",
        "skipped": model_status == "skipped",
        "model": {"status": model_status, "reason": model_reason},
    }
    if render is not None:
        document["prompt"] = {
            "budget": render.budget,
            "chars": render.char_count,
            "containers": list(render.container_names),
            "drops": list(render.drops),
        }
    return document


def metadata_comment(metadata: dict[str, Any]) -> str:
    """One-line JSON inside an HTML comment so the report stays readable."""
    return "<!-- host_report: " + json.dumps(
        metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ) + " -->"


def render_markdown(
    digest: dict[str, Any],
    *,
    model_status: str,
    model_reason: str | None = None,
    interpretation: dict[str, Any] | None = None,
    render: Render | None = None,
) -> str:
    """Render the report with facts and interpretation in separate sections."""
    metadata = _metadata(
        digest,
        model_status=model_status,
        model_reason=model_reason,
        render=render,
    )
    lines = [
        f"# Host log report: {digest.get('host') or 'unknown'}",
        "",
        metadata_comment(metadata),
        "",
    ]
    lines.extend(_facts(digest))
    lines.append("")
    measured = _measured(digest)
    if measured:
        lines.extend(measured)
    lines.extend(_interpretation(model_status, model_reason, interpretation))
    return "\n".join(lines) + "\n"
