"""Deterministic message clustering.

Lines that differ only by volatile parts (timestamps, ids, durations, numbers,
quoted paths) collapse into one cluster with a normalised template.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

__all__ = [
    "Cluster",
    "ClusterAccumulator",
    "SAMPLE_LIMIT",
    "classify_severity",
    "normalise",
]

#: Maximum length of the sample line kept per cluster.
SAMPLE_LIMIT = 200

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_SEVERITY_RANK = {SEVERITY_INFO: 0, SEVERITY_WARNING: 1, SEVERITY_ERROR: 2}

_ERROR_WORDS = re.compile(
    r"(?i)\b(?:error|errors|failed|failing|failure|fail|fatal|exception|"
    r"traceback|panic|critical)\b"
)
_WARNING_WORDS = re.compile(
    r"(?i)\b(?:warn|warning|warnings|deprecated|degraded)\b"
)

_ISO_TIMESTAMP = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})?\b"
)
_UUID = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_HEX_WITH_PREFIX = re.compile(r"\b0[xX][0-9a-fA-F]+\b")
_HEX_ID = re.compile(r"\b(?=[0-9a-f]*[a-f])[0-9a-f]{8,}\b")
_QUOTED = re.compile(r'"[^"]*"|\'[^\']*\'')
_PATH = re.compile(r"(?<![\w.<])/(?:[\w.\-]+/)+[\w.\-]*")
_DURATION = re.compile(r"\b\d+(?:\.\d+)?(?:ns|us|µs|ms|s|m|h|d)\b")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")

_NORMALISERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_ISO_TIMESTAMP, "<ts>"),
    (_UUID, "<id>"),
    (_HEX_WITH_PREFIX, "<hex>"),
    (_HEX_ID, "<hex>"),
    (_QUOTED, "<str>"),
    (_PATH, "<path>"),
    (_DURATION, "<duration>"),
    (_NUMBER, "<num>"),
)

_WHITESPACE = re.compile(r"\s+")


def classify_severity(text: str) -> str:
    """Classify one line as ``error``, ``warning`` or ``info``."""
    if _ERROR_WORDS.search(text):
        return SEVERITY_ERROR
    if _WARNING_WORDS.search(text):
        return SEVERITY_WARNING
    return SEVERITY_INFO


def normalise(text: str) -> str:
    """Replace volatile parts of a line and collapse whitespace."""
    value = text.strip()
    for pattern, replacement in _NORMALISERS:
        value = pattern.sub(replacement, value)
    return _WHITESPACE.sub(" ", value).strip()


def _max_severity(left: str, right: str) -> str:
    return left if _SEVERITY_RANK[left] >= _SEVERITY_RANK[right] else right


@dataclass
class Cluster:
    """One normalised message template and its occurrences."""

    template: str
    count: int
    first_seen: datetime | None
    last_seen: datetime | None
    severity: str
    sample: str
    order: int


class ClusterAccumulator:
    """Group normalised lines, keeping the first sample and the time span."""

    def __init__(self) -> None:
        self._clusters: dict[str, Cluster] = {}
        self._order = 0

    def add(self, text: str, seen: datetime | None, severity: str) -> Cluster:
        template = normalise(text)
        existing = self._clusters.get(template)
        if existing is None:
            cluster = Cluster(
                template=template,
                count=1,
                first_seen=seen,
                last_seen=seen,
                severity=severity,
                sample=text[:SAMPLE_LIMIT],
                order=self._order,
            )
            self._order += 1
            self._clusters[template] = cluster
            return cluster
        existing.count += 1
        existing.severity = _max_severity(existing.severity, severity)
        if seen is not None:
            if existing.first_seen is None or seen < existing.first_seen:
                existing.first_seen = seen
            if existing.last_seen is None or seen > existing.last_seen:
                existing.last_seen = seen
        return existing

    def ranked(self) -> list[Cluster]:
        """Clusters ordered by descending count, then first appearance."""
        return sorted(self._clusters.values(), key=lambda c: (-c.count, c.order))

    def __len__(self) -> int:
        return len(self._clusters)
