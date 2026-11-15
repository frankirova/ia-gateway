"""Collector settings: CLI flags, environment fallbacks and defaults."""

from __future__ import annotations

import json
import os
import re
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from tasks.host_report.delivery import (
    CHATS,
    DEFAULT_CHAT,
    DEFAULT_HUB_TIMEOUT_SECONDS,
    HubSettings,
)
from tasks.host_report.docker_api import DEFAULT_DOCKER_URL, parse_docker_url
from tasks.host_report.probe import Endpoint, ProbeConfigError, parse_endpoints
from tasks.host_report.gateway import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    GatewaySettings,
)
from tasks.host_report.prompt import DEFAULT_PROMPT_BUDGET
from tasks.host_report.window import InvalidDuration, parse_duration

__all__ = [
    "ContainerLimits",
    "DEFAULT_IGNORE_PATTERNS",
    "DEFAULT_MAX_CLUSTERS",
    "DEFAULT_MAX_CONTAINERS",
    "DEFAULT_MAX_LINES",
    "DEFAULT_OVERFETCH",
    "DEFAULT_PROMPT_BUDGET",
    "DEFAULT_SINCE",
    "DEFAULT_SSH_TIMEOUT",
    "DEFAULT_TIMEOUT",
    "TARGETS",
    "Settings",
    "SettingsError",
    "build_settings",
]

DEFAULT_SINCE = "24h"
DEFAULT_TIMEOUT = 10.0
#: Ceiling for one SSH bundle read on a remote target.
DEFAULT_SSH_TIMEOUT = 60.0
#: Where the evidence comes from: this machine or a remote host over SSH.
TARGETS = ("local", "ssh")
DEFAULT_MAX_LINES = 2000
DEFAULT_MAX_CLUSTERS = 25
DEFAULT_MAX_CONTAINERS = 50
#: How many times ``max_lines`` the collector asks Docker for, so that error and
#: warning lines later in the stream can be kept when the cap is hit.
DEFAULT_OVERFETCH = 4

#: Patterns applied before clustering. Routine noise only; anything that looks
#: like a real event is kept.
DEFAULT_IGNORE_PATTERNS: tuple[str, ...] = (
    r"^\s*$",
    r"(?i)healthcheck",
    r"(?i)\b(?:GET|HEAD|POST)\s+/(?:health|healthz|live|ready|ping)\b",
)

ENV_DOCKER_URL = "HOST_REPORT_DOCKER_URL"
ENV_TIMEOUT = "HOST_REPORT_TIMEOUT"
ENV_MAX_LINES = "HOST_REPORT_MAX_LINES"
ENV_MAX_CLUSTERS = "HOST_REPORT_MAX_CLUSTERS"
ENV_MAX_CONTAINERS = "HOST_REPORT_MAX_CONTAINERS"
ENV_OVERFETCH = "HOST_REPORT_OVERFETCH"
ENV_CONTAINER_LIMITS = "HOST_REPORT_CONTAINER_LIMITS"
ENV_IGNORE = "HOST_REPORT_IGNORE"
ENV_SINCE = "HOST_REPORT_SINCE"
ENV_HOST = "HOST_REPORT_HOST"
ENV_PROMPT_BUDGET = "HOST_REPORT_PROMPT_BUDGET"
ENV_GATEWAY_URL = "HOST_REPORT_GATEWAY_URL"
ENV_GATEWAY_KEY_FILE = "HOST_REPORT_GATEWAY_KEY_FILE"
ENV_GATEWAY_CA_BUNDLE = "HOST_REPORT_GATEWAY_CA_BUNDLE"
ENV_GATEWAY_MODEL = "HOST_REPORT_GATEWAY_MODEL"
ENV_MAX_TOKENS = "HOST_REPORT_MAX_TOKENS"
ENV_GATEWAY_TIMEOUT = "HOST_REPORT_TIMEOUT_SECONDS"
ENV_NOTIFY = "HOST_REPORT_NOTIFY"
ENV_HUB_URL = "HOST_REPORT_HUB_URL"
ENV_HUB_TOKEN_FILE = "HOST_REPORT_HUB_TOKEN_FILE"
ENV_HUB_CA_BUNDLE = "HOST_REPORT_HUB_CA_BUNDLE"
ENV_HUB_CHAT = "HOST_REPORT_HUB_CHAT"
ENV_HUB_TIMEOUT = "HOST_REPORT_HUB_TIMEOUT_SECONDS"
ENV_NOTIFY_KEY = "HOST_REPORT_NOTIFY_KEY"
ENV_TARGET = "HOST_REPORT_TARGET"
ENV_SSH_TARGET = "HOST_REPORT_SSH_TARGET"
ENV_SSH_TIMEOUT = "HOST_REPORT_SSH_TIMEOUT"
ENV_SSH_BINARY = "HOST_REPORT_SSH_BINARY"
ENV_ENDPOINTS = "HOST_REPORT_ENDPOINTS"

NOTIFY_MODES = ("none", "hub")


class SettingsError(ValueError):
    """Raised for an unusable CLI value or environment variable."""


@dataclass(frozen=True)
class ContainerLimits:
    """The caps that apply to one container."""

    max_lines: int = DEFAULT_MAX_LINES
    max_clusters: int = DEFAULT_MAX_CLUSTERS


@dataclass(frozen=True)
class Settings:
    """Everything the collector needs for one run."""

    docker_url: str = DEFAULT_DOCKER_URL
    timeout: float = DEFAULT_TIMEOUT
    since_label: str = DEFAULT_SINCE
    since_seconds: int = 86400
    max_lines: int = DEFAULT_MAX_LINES
    max_clusters: int = DEFAULT_MAX_CLUSTERS
    max_containers: int = DEFAULT_MAX_CONTAINERS
    overfetch: int = DEFAULT_OVERFETCH
    container_limits: Mapping[str, ContainerLimits] = field(default_factory=dict)
    ignore_patterns: tuple[re.Pattern[str], ...] = ()
    ignore_labels: tuple[str, ...] = ()
    host: str = ""
    target: str = "local"
    ssh_target: str = ""
    ssh_timeout: float = DEFAULT_SSH_TIMEOUT
    ssh_binary: str = "ssh"
    endpoints: tuple[Endpoint, ...] = ()
    output_format: str = "json"
    report_format: str = "none"
    strict_model: bool = False
    prompt_budget: int = DEFAULT_PROMPT_BUDGET
    gateway: GatewaySettings | None = None
    hub: HubSettings | None = None

    def limits_for(self, container: str) -> ContainerLimits:
        """Caps for one container: its override, else the global defaults."""
        override = self.container_limits.get(container)
        if override is not None:
            return override
        return ContainerLimits(
            max_lines=self.max_lines, max_clusters=self.max_clusters
        )


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    raw = env.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _resolve_positive_int(
    explicit: int | None, env: Mapping[str, str], name: str, default: int
) -> int:
    if explicit is not None:
        return explicit
    raw = _env_value(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a whole number, got {raw!r}") from None
    if value < 1:
        raise SettingsError(f"{name} must be at least 1, got {value}")
    return value


def _container_limit_value(
    entry: Mapping[str, Any], key: str, container: str, default: int
) -> int:
    """One cap inside a per-container override; absent keys keep the default."""
    if key not in entry:
        return default
    value = entry[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise SettingsError(
            f"{ENV_CONTAINER_LIMITS}[{container!r}].{key} must be a whole "
            f"number, got {value!r}"
        )
    if value < 1:
        raise SettingsError(
            f"{ENV_CONTAINER_LIMITS}[{container!r}].{key} must be at least 1, "
            f"got {value}"
        )
    return value


def _resolve_container_limits(
    env: Mapping[str, str], *, max_lines: int, max_clusters: int
) -> dict[str, ContainerLimits]:
    """Parse ``HOST_REPORT_CONTAINER_LIMITS`` into per-container caps.

    The value is a JSON object mapping a container name to an object with the
    optional keys ``max_lines`` and ``max_clusters``. Every problem is a
    configuration error; a name absent from the run is simply unused.
    """
    raw = _env_value(env, ENV_CONTAINER_LIMITS)
    if raw is None:
        return {}
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise SettingsError(
            f"{ENV_CONTAINER_LIMITS} must be a JSON object, got invalid JSON: "
            f"{error.msg} at column {error.colno}"
        ) from None
    if not isinstance(document, dict):
        raise SettingsError(
            f"{ENV_CONTAINER_LIMITS} must be a JSON object mapping container "
            f"names to limits, got {type(document).__name__}"
        )
    allowed = {"max_lines", "max_clusters"}
    limits: dict[str, ContainerLimits] = {}
    for name, entry in document.items():
        container = name.strip() if isinstance(name, str) else ""
        if not container:
            raise SettingsError(
                f"{ENV_CONTAINER_LIMITS} contains an empty container name"
            )
        if not isinstance(entry, dict):
            raise SettingsError(
                f"{ENV_CONTAINER_LIMITS}[{container!r}] must be a JSON object, "
                f"got {type(entry).__name__}"
            )
        unknown = sorted(set(entry) - allowed)
        if unknown:
            raise SettingsError(
                f"{ENV_CONTAINER_LIMITS}[{container!r}] has unknown keys: "
                f"{', '.join(unknown)}; allowed keys are max_lines and "
                f"max_clusters"
            )
        limits[container] = ContainerLimits(
            max_lines=_container_limit_value(
                entry, "max_lines", container, max_lines
            ),
            max_clusters=_container_limit_value(
                entry, "max_clusters", container, max_clusters
            ),
        )
    return limits


def _resolve_timeout(explicit: float | None, env: Mapping[str, str]) -> float:
    if explicit is not None:
        return explicit
    raw = _env_value(env, ENV_TIMEOUT)
    if raw is None:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{ENV_TIMEOUT} must be a number, got {raw!r}") from None
    if value <= 0:
        raise SettingsError(f"{ENV_TIMEOUT} must be greater than zero, got {value}")
    return value


def _resolve_positive_float(
    env: Mapping[str, str], name: str, default: float
) -> float:
    raw = _env_value(env, name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(f"{name} must be a number, got {raw!r}") from None
    if value <= 0:
        raise SettingsError(f"{name} must be greater than zero, got {value}")
    return value


def _resolve_since(explicit: str | None, env: Mapping[str, str]) -> tuple[str, int]:
    label = explicit if explicit is not None else _env_value(env, ENV_SINCE)
    if label is None:
        label = DEFAULT_SINCE
    try:
        return label, parse_duration(label)
    except InvalidDuration as error:
        raise SettingsError(f"invalid --since value: {error}") from None


def _resolve_ignore_patterns(
    extra: Sequence[str] | None, env: Mapping[str, str]
) -> tuple[tuple[re.Pattern[str], ...], tuple[str, ...]]:
    labels = list(DEFAULT_IGNORE_PATTERNS)
    from_env = _env_value(env, ENV_IGNORE)
    if from_env is not None:
        labels.extend(part.strip() for part in from_env.split(",") if part.strip())
    if extra:
        labels.extend(extra)
    compiled: list[re.Pattern[str]] = []
    for label in labels:
        try:
            compiled.append(re.compile(label))
        except re.error as error:
            raise SettingsError(
                f"invalid --ignore pattern {label!r}: {error}"
            ) from None
    return tuple(compiled), tuple(labels)


def _resolve_docker_url(explicit: str | None, env: Mapping[str, str]) -> str:
    candidate = explicit if explicit is not None else _env_value(env, ENV_DOCKER_URL)
    if candidate is None:
        candidate = DEFAULT_DOCKER_URL
    try:
        parse_docker_url(candidate)
    except ValueError as error:
        raise SettingsError(f"invalid docker URL {candidate!r}: {error}") from None
    return candidate


def _gateway_url(raw: str) -> str:
    parsed = urlsplit(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SettingsError(
            f"{ENV_GATEWAY_URL} must be an http:// or https:// URL, got {raw!r}"
        )
    return raw


def _resolve_gateway_timeout(env: Mapping[str, str]) -> float:
    raw = _env_value(env, ENV_GATEWAY_TIMEOUT)
    if raw is None:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(
            f"{ENV_GATEWAY_TIMEOUT} must be a number, got {raw!r}"
        ) from None
    if value <= 0:
        raise SettingsError(
            f"{ENV_GATEWAY_TIMEOUT} must be greater than zero, got {value}"
        )
    return value


def _resolve_gateway(env: Mapping[str, str]) -> GatewaySettings:
    """Resolve the gateway settings, validating TLS configuration eagerly."""
    base_url = _env_value(env, ENV_GATEWAY_URL)
    ca_bundle = _env_value(env, ENV_GATEWAY_CA_BUNDLE)
    if base_url is not None:
        base_url = _gateway_url(base_url)
        if urlsplit(base_url).scheme == "https":
            if ca_bundle is None:
                raise SettingsError(
                    f"{ENV_GATEWAY_CA_BUNDLE} is required for an https "
                    f"{ENV_GATEWAY_URL}; TLS verification is never disabled"
                )
    if ca_bundle is not None and not Path(ca_bundle).is_file():
        raise SettingsError(
            f"{ENV_GATEWAY_CA_BUNDLE} points to a file that does not exist: {ca_bundle}"
        )
    model = _env_value(env, ENV_GATEWAY_MODEL) or DEFAULT_MODEL
    return GatewaySettings(
        base_url=base_url,
        key_file=_env_value(env, ENV_GATEWAY_KEY_FILE),
        ca_bundle=ca_bundle,
        model=model,
        max_tokens=_resolve_positive_int(
            None, env, ENV_MAX_TOKENS, DEFAULT_MAX_TOKENS
        ),
        timeout_seconds=_resolve_gateway_timeout(env),
    )


def _resolve_notify_mode(explicit: str | None, env: Mapping[str, str]) -> str:
    """The delivery mode: CLI flag, then environment, then ``none``."""
    mode = explicit or _env_value(env, ENV_NOTIFY) or "none"
    mode = mode.strip().lower()
    if mode not in NOTIFY_MODES:
        allowed = " or ".join(NOTIFY_MODES)
        raise SettingsError(f"{ENV_NOTIFY} must be {allowed}, got {mode!r}")
    return mode


def _hub_url(raw: str) -> str:
    parts = urlsplit(raw)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise SettingsError(
            f"{ENV_HUB_URL} must be an http:// or https:// URL, got {raw!r}"
        )
    return raw


def _resolve_hub(
    explicit: str | None, env: Mapping[str, str], report_format: str
) -> HubSettings | None:
    """Resolve delivery settings, or ``None`` when nothing is to be sent."""
    if _resolve_notify_mode(explicit, env) == "none":
        return None
    if report_format != "markdown":
        raise SettingsError(
            f"--notify hub requires --report markdown, because the message has no "
            f"facts without the report"
        )
    raw_url = _env_value(env, ENV_HUB_URL)
    if raw_url is None:
        raise SettingsError(f"{ENV_HUB_URL} is required for --notify hub")
    base_url = _hub_url(raw_url)
    ca_bundle = _env_value(env, ENV_HUB_CA_BUNDLE)
    if urlsplit(base_url).scheme.lower() == "https":
        if ca_bundle is None:
            raise SettingsError(
                f"{ENV_HUB_CA_BUNDLE} is required for an https {ENV_HUB_URL}; "
                f"TLS verification is never disabled"
            )
    if ca_bundle is not None and not Path(ca_bundle).is_file():
        raise SettingsError(
            f"{ENV_HUB_CA_BUNDLE} points to a file that does not exist: {ca_bundle}"
        )
    token_file = _env_value(env, ENV_HUB_TOKEN_FILE)
    if token_file is None:
        raise SettingsError(f"{ENV_HUB_TOKEN_FILE} is required for --notify hub")
    chat = (_env_value(env, ENV_HUB_CHAT) or DEFAULT_CHAT).lower()
    if chat not in CHATS:
        allowed = " or ".join(CHATS)
        raise SettingsError(f"{ENV_HUB_CHAT} must be {allowed}, got {chat!r}")
    return HubSettings(
        base_url=base_url,
        token_file=token_file,
        ca_bundle=ca_bundle,
        chat=chat,
        timeout_seconds=_resolve_hub_timeout(env),
        deduplication_key=_env_value(env, ENV_NOTIFY_KEY),
    )


def _resolve_hub_timeout(env: Mapping[str, str]) -> float:
    raw = _env_value(env, ENV_HUB_TIMEOUT)
    if raw is None:
        return DEFAULT_HUB_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except ValueError:
        raise SettingsError(
            f"{ENV_HUB_TIMEOUT} must be a number, got {raw!r}"
        ) from None
    if value <= 0:
        raise SettingsError(
            f"{ENV_HUB_TIMEOUT} must be greater than zero, got {value}"
        )
    return value


def build_settings(
    options: Any, env: Mapping[str, str] | None = None
) -> Settings:
    """Merge CLI options, environment variables and defaults into settings.

    Precedence is CLI flag, then environment variable, then default.
    """
    environment = os.environ if env is None else env
    since_label, since_seconds = _resolve_since(
        getattr(options, "since", None), environment
    )
    ignore_patterns, ignore_labels = _resolve_ignore_patterns(
        getattr(options, "ignore", None), environment
    )
    host = _env_value(environment, ENV_HOST) or socket.gethostname()
    target = (_env_value(environment, ENV_TARGET) or "local").strip().lower()
    if target not in TARGETS:
        allowed = " or ".join(TARGETS)
        raise SettingsError(f"{ENV_TARGET} must be {allowed}, got {target!r}")
    ssh_target = (_env_value(environment, ENV_SSH_TARGET) or "").strip()
    if target == "ssh" and not ssh_target:
        raise SettingsError(
            f"{ENV_SSH_TARGET} is required when {ENV_TARGET}=ssh"
        )
    ssh_timeout = _resolve_positive_float(
        environment, ENV_SSH_TIMEOUT, DEFAULT_SSH_TIMEOUT
    )
    try:
        endpoints = parse_endpoints(_env_value(environment, ENV_ENDPOINTS))
    except ProbeConfigError as error:
        raise SettingsError(f"{ENV_ENDPOINTS}: {error}") from error
    output_format = getattr(options, "format", None) or "json"
    report_format = getattr(options, "report", None) or "none"
    prompt_budget = (
        _resolve_positive_int(
            getattr(options, "prompt_budget", None),
            environment,
            ENV_PROMPT_BUDGET,
            DEFAULT_PROMPT_BUDGET,
        )
        if report_format == "markdown"
        else DEFAULT_PROMPT_BUDGET
    )
    gateway = _resolve_gateway(environment) if report_format == "markdown" else None
    hub = _resolve_hub(
        getattr(options, "notify", None), environment, report_format
    )
    max_lines = _resolve_positive_int(
        getattr(options, "max_lines", None),
        environment,
        ENV_MAX_LINES,
        DEFAULT_MAX_LINES,
    )
    max_clusters = _resolve_positive_int(
        getattr(options, "max_clusters", None),
        environment,
        ENV_MAX_CLUSTERS,
        DEFAULT_MAX_CLUSTERS,
    )
    return Settings(
        docker_url=_resolve_docker_url(getattr(options, "docker_url", None), environment),
        timeout=_resolve_timeout(getattr(options, "timeout", None), environment),
        since_label=since_label,
        since_seconds=since_seconds,
        max_lines=max_lines,
        max_clusters=max_clusters,
        max_containers=_resolve_positive_int(
            getattr(options, "max_containers", None),
            environment,
            ENV_MAX_CONTAINERS,
            DEFAULT_MAX_CONTAINERS,
        ),
        overfetch=_resolve_positive_int(
            None, environment, ENV_OVERFETCH, DEFAULT_OVERFETCH
        ),
        container_limits=_resolve_container_limits(
            environment, max_lines=max_lines, max_clusters=max_clusters
        ),
        ignore_patterns=ignore_patterns,
        ignore_labels=ignore_labels,
        host=host,
        target=target,
        ssh_target=ssh_target,
        ssh_timeout=ssh_timeout,
        ssh_binary=(_env_value(environment, ENV_SSH_BINARY) or "ssh").strip(),
        endpoints=endpoints,
        output_format=output_format,
        report_format=report_format,
        strict_model=bool(getattr(options, "strict_model", False)),
        prompt_budget=prompt_budget,
        gateway=gateway,
        hub=hub,
    )
