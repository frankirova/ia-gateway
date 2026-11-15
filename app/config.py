"""Configuration loading for the inference gateway.

Configuration lives in a JSON file referenced by ``GATEWAY_CONFIG``. Loading is
strict and fails closed: a missing, unreadable, or invalid document raises
``ConfigError`` (a ``ValueError``) while the application is being created, so the
process never starts with a partially understood policy.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ENV_VAR = "GATEWAY_CONFIG"

DEFAULT_BASE_URL = "http://127.0.0.1:8190"
DEFAULT_MODEL = "gemma-4-E4B-it-UD-Q4_K_XL"
DEFAULT_CONNECT_TIMEOUT = 3.0
DEFAULT_READ_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT_TOKENS = 512
DEFAULT_RATE_CAPACITY = 10
DEFAULT_RATE_PER_MINUTE = 10.0
DEFAULT_CONTEXT_CHAR_BUDGET = 16000

_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ROOT_KEYS = frozenset({"upstream", "limits", "applications"})
_UPSTREAM_KEYS = frozenset(
    {"base_url", "model", "connect_timeout", "read_timeout", "api_key"}
)
_APPLICATION_KEYS = frozenset(
    {"id", "key_sha256", "models", "max_output_tokens", "rate_capacity", "rate_per_minute"}
)
_LIMIT_KEYS = frozenset({"context_char_budget"})


class ConfigError(ValueError):
    """Raised when gateway configuration is missing or invalid."""


@dataclass(frozen=True)
class UpstreamConfig:
    """Backend the gateway proxies inference to."""

    base_url: str
    model: str
    connect_timeout: float
    read_timeout: float
    api_key: str | None = None

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    @property
    def health_url(self) -> str:
        return f"{self.base_url}/health"


@dataclass(frozen=True)
class ApplicationConfig:
    """One independently authenticated client of the gateway."""

    id: str
    key_sha256: str
    models: tuple[str, ...]
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    rate_capacity: int = DEFAULT_RATE_CAPACITY
    rate_per_minute: float = DEFAULT_RATE_PER_MINUTE


@dataclass(frozen=True)
class LimitsConfig:
    """Provisional operating limits that are safe to tune per deployment."""

    context_char_budget: int = DEFAULT_CONTEXT_CHAR_BUDGET


@dataclass(frozen=True)
class GatewayConfig:
    upstream: UpstreamConfig
    applications: tuple[ApplicationConfig, ...]
    limits: LimitsConfig = LimitsConfig()


def load_config(path: str | os.PathLike[str] | None = None) -> GatewayConfig:
    """Load and validate the gateway configuration.

    ``path`` is only for tests and tooling; the application always reads
    ``GATEWAY_CONFIG``.
    """
    raw_path = os.environ.get(ENV_VAR) if path is None else str(path)
    if not raw_path:
        raise ConfigError(f"{ENV_VAR} is not set")

    try:
        text = Path(raw_path).read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError("configuration file cannot be read") from exc

    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ConfigError("configuration file is not valid JSON") from exc

    if not isinstance(document, dict):
        raise ConfigError("configuration root must be a JSON object")
    _reject_unknown_keys(document, _ROOT_KEYS, "configuration")

    return GatewayConfig(
        upstream=_parse_upstream(document.get("upstream")),
        applications=_parse_applications(document.get("applications")),
        limits=_parse_limits(document.get("limits")),
    )


def _reject_unknown_keys(raw: dict[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ConfigError(f"{where} contains unknown keys: {', '.join(unknown)}")


def _parse_upstream(raw: Any) -> UpstreamConfig:
    if not isinstance(raw, dict):
        raise ConfigError("'upstream' must be a JSON object")
    _reject_unknown_keys(raw, _UPSTREAM_KEYS, "'upstream'")

    base_url = _text(raw.get("base_url", DEFAULT_BASE_URL), "upstream.base_url")
    if not base_url.startswith(("http://", "https://")):
        raise ConfigError("'upstream.base_url' must start with http:// or https://")

    api_key = raw.get("api_key")
    if api_key is not None:
        api_key = _text(api_key, "upstream.api_key")

    return UpstreamConfig(
        base_url=base_url.rstrip("/"),
        model=_text(raw.get("model", DEFAULT_MODEL), "upstream.model"),
        connect_timeout=_positive_number(
            raw.get("connect_timeout", DEFAULT_CONNECT_TIMEOUT),
            "upstream.connect_timeout",
        ),
        read_timeout=_positive_number(
            raw.get("read_timeout", DEFAULT_READ_TIMEOUT), "upstream.read_timeout"
        ),
        api_key=api_key,
    )


def _parse_limits(raw: Any) -> LimitsConfig:
    if raw is None:
        return LimitsConfig()
    if not isinstance(raw, dict):
        raise ConfigError("'limits' must be a JSON object")
    _reject_unknown_keys(raw, _LIMIT_KEYS, "'limits'")
    return LimitsConfig(
        context_char_budget=_positive_int(
            raw.get("context_char_budget", DEFAULT_CONTEXT_CHAR_BUDGET),
            "limits.context_char_budget",
        )
    )


def _parse_applications(raw: Any) -> tuple[ApplicationConfig, ...]:
    if not isinstance(raw, list) or not raw:
        raise ConfigError("'applications' must be a nonempty JSON array")

    applications: list[ApplicationConfig] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        where = f"applications[{index}]"
        if not isinstance(item, dict):
            raise ConfigError(f"'{where}' must be a JSON object")
        _reject_unknown_keys(item, _APPLICATION_KEYS, f"'{where}'")

        application_id = _text(item.get("id"), f"{where}.id")
        if application_id in seen_ids:
            raise ConfigError(f"duplicate application id: {application_id}")
        seen_ids.add(application_id)

        digest = _text(item.get("key_sha256"), f"{where}.key_sha256").lower()
        if not _DIGEST_PATTERN.fullmatch(digest):
            raise ConfigError(
                f"'{where}.key_sha256' must be a lowercase SHA-256 hex digest"
            )

        models = item.get("models")
        if not isinstance(models, list) or not models:
            raise ConfigError(f"'{where}.models' must be a nonempty JSON array")
        parsed_models: list[str] = []
        for model in models:
            alias = _text(model, f"{where}.models")
            if alias in parsed_models:
                raise ConfigError(f"'{where}.models' contains duplicate alias: {alias}")
            parsed_models.append(alias)

        applications.append(
            ApplicationConfig(
                id=application_id,
                key_sha256=digest,
                models=tuple(parsed_models),
                max_output_tokens=_positive_int(
                    item.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS),
                    f"{where}.max_output_tokens",
                ),
                rate_capacity=_positive_int(
                    item.get("rate_capacity", DEFAULT_RATE_CAPACITY),
                    f"{where}.rate_capacity",
                ),
                rate_per_minute=_positive_number(
                    item.get("rate_per_minute", DEFAULT_RATE_PER_MINUTE),
                    f"{where}.rate_per_minute",
                ),
            )
        )
    return tuple(applications)


def _text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"'{where}' must be a nonempty string")
    return value


def _positive_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"'{where}' must be an integer greater than 0")
    return value


def _positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"'{where}' must be a number greater than 0")
    return float(value)
