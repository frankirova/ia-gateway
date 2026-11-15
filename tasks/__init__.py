"""Deterministic host log collection for the scheduled host report job.

Nothing in this package calls a language model: it talks to the Docker Engine
API, summarises container logs, redacts obvious secrets and prints a digest.
"""

from __future__ import annotations

SCHEMA_VERSION = 1

__all__ = ["SCHEMA_VERSION"]
