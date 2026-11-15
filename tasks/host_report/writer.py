"""Report orchestration: skip threshold, one bounded gateway call, markdown.

The deterministic facts are always reportable. Interpretation is optional: a
missing key file, a failed or timed-out call or an unusable reply produces a
facts-only report instead of losing the report. The prompt budget is verified
before any call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tasks.host_report.gateway import Diagnostics, GatewayError, complete
from tasks.host_report.interpretation import InvalidReply, parse_interpretation
from tasks.host_report.markdown import render_markdown
from tasks.host_report.prompt import Render, render_digest
from tasks.host_report.settings import Settings

__all__ = ["ModelFailure", "Report", "write_report"]

MODEL_USED = "used"
MODEL_SKIPPED = "skipped"
MODEL_UNAVAILABLE = "unavailable"

#: Why the model was skipped when no container qualified. Kept apart so a
#: measured fact is never reported as an absence of news.
NO_ACTIVITY = "no container showed activity"
MEASURED_WITHOUT_ACTIVITY = "measured findings without container activity"


@dataclass(frozen=True)
class Report:
    """One rendered markdown report plus what delivery needs afterwards."""

    markdown: str
    model_status: str = MODEL_SKIPPED
    model_reason: str | None = None
    interpretation: dict[str, Any] | None = None


class ModelFailure(RuntimeError):
    """Strict mode: the interpretation could not be produced."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _interpret(
    render: Render, settings: Settings, diagnostics: Diagnostics | None
) -> tuple[str, str | None, dict[str, Any] | None]:
    """Return ``(status, reason, interpretation)`` for one render."""
    gateway = settings.gateway
    if gateway is None:
        return MODEL_UNAVAILABLE, "the gateway is not configured", None
    try:
        content = complete(gateway, render.messages(), diagnostics=diagnostics)
    except GatewayError as error:
        return MODEL_UNAVAILABLE, error.reason, None
    try:
        interpretation = parse_interpretation(content, render.container_names)
    except InvalidReply as error:
        return MODEL_UNAVAILABLE, f"invalid model reply: {error.reason}", None
    return MODEL_USED, None, interpretation


def _has_measured_news(digest: dict[str, Any]) -> bool:
    """True when the digest carries a fact the collector measured itself.

    An unreachable source, a host finding or a failing endpoint is news even
    when no container showed activity. Malformed entries are ignored, never
    guessed at and never fatal.
    """
    if digest.get("unreachable"):
        return True
    findings = digest.get("host_findings")
    if isinstance(findings, (list, tuple)) and any(
        isinstance(entry, dict) for entry in findings
    ):
        return True
    probes = digest.get("probes")
    if isinstance(probes, (list, tuple)):
        for entry in probes:
            if isinstance(entry, dict) and not entry.get("ok", True):
                return True
    return False


def write_report(
    digest: dict[str, Any],
    settings: Settings,
    *,
    diagnostics: Diagnostics | None = None,
) -> Report:
    """Render the markdown report, calling the gateway at most once.

    Raises :class:`~tasks.host_report.prompt.PromptBudgetExceeded` when the
    reduced render still does not fit, and :class:`ModelFailure` in strict mode
    when no interpretation could be produced.
    """
    render = render_digest(digest, settings.prompt_budget)
    if not render.container_names:
        reason = (
            MEASURED_WITHOUT_ACTIVITY
            if _has_measured_news(digest)
            else NO_ACTIVITY
        )
        return Report(
            markdown=render_markdown(
                digest,
                model_status=MODEL_SKIPPED,
                model_reason=reason,
                render=render,
            ),
            model_status=MODEL_SKIPPED,
            model_reason=reason,
        )

    status, reason, interpretation = _interpret(render, settings, diagnostics)
    if status != MODEL_USED and settings.strict_model:
        raise ModelFailure(reason or "the interpretation is unavailable")
    return Report(
        markdown=render_markdown(
            digest,
            model_status=status,
            model_reason=reason,
            interpretation=interpretation,
            render=render,
        ),
        model_status=status,
        model_reason=reason,
        interpretation=interpretation,
    )
