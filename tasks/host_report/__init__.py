"""Deterministic host log collection and the optional report writer.

Collection never calls a language model: it talks to the Docker Engine API,
summarises container logs, redacts obvious secrets and prints a digest. The
writer renders a bounded prompt, makes at most one gateway call and keeps the
deterministic facts separate from the model interpretation.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

__all__ = ["SCHEMA_VERSION"]
