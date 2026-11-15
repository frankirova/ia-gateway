"""Strict validation of the model reply.

The model is asked for exactly one JSON object with a fixed shape. Anything
else — invalid JSON, extra keys, unknown containers, oversized strings, too many
findings — is rejected so the caller can fall back to the deterministic facts.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

__all__ = [
    "FIELD_LIMIT",
    "FINDING_KEYS",
    "InvalidReply",
    "MAX_FINDINGS",
    "SEVERITIES",
    "SUMMARY_LIMIT",
    "parse_interpretation",
]

SUMMARY_LIMIT = 300
FIELD_LIMIT = 150
MAX_FINDINGS = 3

#: Normalisation placeholders the renderer uses; a report that quotes them shows
#: the reader a synthetic token instead of a value, so the reply is rejected.
PLACEHOLDER = re.compile(r"<(?:num|ts|str|path|duration|id|hex)>")
SEVERITIES = ("high", "medium", "low")

TOP_LEVEL_KEYS = frozenset({"summary", "findings"})
FINDING_KEYS = frozenset({"container", "severity", "what", "why", "action"})


class InvalidReply(ValueError):
    """The reply does not match the requested shape."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _require_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str):
        raise InvalidReply(f"{label} must be a string")
    if len(value) > limit:
        raise InvalidReply(f"{label} is longer than {limit} characters")
    if PLACEHOLDER.search(value):
        raise InvalidReply(f"{label} quotes a normalisation placeholder")
    return value


def _check_finding(finding: Any, allowed: frozenset[str]) -> dict[str, str]:
    if not isinstance(finding, dict):
        raise InvalidReply("every finding must be an object")
    keys = set(finding)
    if keys != FINDING_KEYS:
        missing = sorted(FINDING_KEYS - keys)
        extra = sorted(keys - FINDING_KEYS)
        detail = f"missing {missing}" if missing else f"unexpected {extra}"
        raise InvalidReply(f"finding keys are wrong: {detail}")
    container = _require_text(finding["container"], "finding container", FIELD_LIMIT)
    if container not in allowed:
        raise InvalidReply("a finding names a container absent from the evidence")
    severity = finding["severity"]
    if severity not in SEVERITIES:
        raise InvalidReply("a finding severity is not high, medium or low")
    return {
        "container": container,
        "severity": severity,
        "what": _require_text(finding["what"], "finding what", FIELD_LIMIT),
        "why": _require_text(finding["why"], "finding why", FIELD_LIMIT),
        "action": _require_text(finding["action"], "finding action", FIELD_LIMIT),
    }


#: A single fenced block wrapping the object. The deployed model reliably
#: answers with ```json even when asked for raw JSON; one fenced block is
#: unambiguous, while prose around it is not and stays rejected.
_FENCED_REPLY = re.compile(r"```(?:json)?[ \t]*\r?\n(.*?)\r?\n```", re.S)


def _unwrap_fence(content: str) -> str:
    """Return the object text, unwrapping exactly one markdown fence."""
    stripped = content.strip()
    if not stripped.startswith("```"):
        return stripped
    match = _FENCED_REPLY.fullmatch(stripped)
    if match is None:
        raise InvalidReply("the reply is not JSON")
    return match.group(1).strip()


def parse_interpretation(
    content: str, allowed_containers: Iterable[str]
) -> dict[str, Any]:
    """Return the validated interpretation or raise :class:`InvalidReply`."""
    try:
        document = json.loads(_unwrap_fence(content))
    except (TypeError, json.JSONDecodeError):
        raise InvalidReply("the reply is not JSON") from None
    if not isinstance(document, dict):
        raise InvalidReply("the reply is not a JSON object")
    keys = set(document)
    if keys != TOP_LEVEL_KEYS:
        missing = sorted(TOP_LEVEL_KEYS - keys)
        extra = sorted(keys - TOP_LEVEL_KEYS)
        detail = f"missing {missing}" if missing else f"unexpected {extra}"
        raise InvalidReply(f"reply keys are wrong: {detail}")

    summary = _require_text(document["summary"], "summary", SUMMARY_LIMIT)
    findings = document["findings"]
    if not isinstance(findings, list):
        raise InvalidReply("findings must be a list")
    if len(findings) > MAX_FINDINGS:
        raise InvalidReply(f"the reply has more than {MAX_FINDINGS} findings")
    allowed = frozenset(str(name) for name in allowed_containers)
    return {
        "summary": summary,
        "findings": [_check_finding(finding, allowed) for finding in findings],
    }
