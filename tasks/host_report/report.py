"""Rendering the digest as JSON (default) or a short human summary."""

from __future__ import annotations

import json
from typing import Any

__all__ = ["render_json", "render_text"]


def render_json(report: dict[str, Any]) -> str:
    """One JSON document, indented for humans, terminated by a newline."""
    return json.dumps(report, indent=2, ensure_ascii=False) + "\n"


def render_text(report: dict[str, Any]) -> str:
    """Short human summary of the same digest."""
    window = report["window"]
    containers = report["containers"]
    lines = [
        f"Host log report for {report['host']}",
        f"window: {window['since']} .. {window['until']} ({window['duration']})",
        f"containers: {len(containers)} collected, {len(report['skipped'])} skipped, "
        f"{len(report['truncations'])} truncations",
    ]
    for entry in containers:
        lines.append(
            f"- {entry['name']} ({entry['image'] or 'unknown image'}) "
            f"state={entry['state']} restarts={entry['restart_count']} "
            f"exit_code={entry['exit_code']}"
        )
        lines.append(
            f"  lines={entry['total_lines']} (ignored {entry['ignored_lines']} of "
            f"{entry['lines_read']}) errors={entry['error_lines']} "
            f"warnings={entry['warning_lines']} clusters={len(entry['clusters'])}"
        )
        for cluster in entry["clusters"]:
            lines.append(
                f"  x{cluster['count']} {cluster['severity']}: {cluster['template']}"
            )
    for item in report["skipped"]:
        lines.append(f"skipped {item['container']}: {item['reason']}")
    for item in report["truncations"]:
        subject = item["container"] or "host"
        lines.append(
            f"truncated {subject} ({item['kind']}): limit {item['limit']}, "
            f"observed {item['observed']} - {item['message']}"
        )
    return "\n".join(lines) + "\n"
