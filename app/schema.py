"""Request schema validation for the chat completions endpoint.

Validation is written by hand instead of relying on a generated schema so every
rejection uses the gateway error envelope and the exact stable codes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from .config import ApplicationConfig
from .errors import invalid_request, model_not_allowed

ALLOWED_ROLES = frozenset({"system", "user", "assistant"})
ALLOWED_REQUEST_FIELDS = frozenset(
    {"model", "messages", "max_tokens", "stream", "temperature", "reasoning"}
)
ALLOWED_MESSAGE_FIELDS = frozenset({"role", "content"})
DEFAULT_MAX_TOKENS = 256
# Deterministic output by default: identical input must yield identical output.
DEFAULT_TEMPERATURE = 0
DEFAULT_REASONING = False

# Public aliases this gateway knows about; they are task-agnostic by design.
PUBLIC_ALIASES = ("e4b",)


@dataclass(frozen=True)
class ChatRequest:
    model: str
    messages: tuple[dict[str, str], ...]
    max_tokens: int
    temperature: float
    reasoning: bool


def parse_chat_request(payload: dict[str, Any], application: ApplicationConfig) -> ChatRequest:
    """Validate an authenticated chat request body."""
    unknown = sorted(set(payload) - ALLOWED_REQUEST_FIELDS)
    if unknown:
        raise invalid_request(
            f"Unsupported request field: {unknown[0]}.", param=unknown[0]
        )

    if "model" not in payload:
        raise invalid_request("'model' is required.", param="model")
    model = payload["model"]
    if not isinstance(model, str) or not model.strip():
        raise invalid_request("'model' must be a nonempty string.", param="model")

    messages = parse_messages(payload)
    max_tokens = parse_max_tokens(payload.get("max_tokens", DEFAULT_MAX_TOKENS), application)
    temperature = parse_temperature(payload.get("temperature", DEFAULT_TEMPERATURE))
    reasoning = parse_reasoning(payload.get("reasoning", DEFAULT_REASONING))

    if "stream" in payload and payload["stream"] is not False:
        raise invalid_request(
            "Streaming is not supported; 'stream' must be false or omitted.",
            param="stream",
        )

    return ChatRequest(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        reasoning=reasoning,
    )


def parse_messages(payload: dict[str, Any]) -> tuple[dict[str, str], ...]:
    if "messages" not in payload:
        raise invalid_request("'messages' is required.", param="messages")
    raw_messages = payload["messages"]
    if not isinstance(raw_messages, list) or not raw_messages:
        raise invalid_request(
            "'messages' must be a nonempty array.", param="messages"
        )

    messages: list[dict[str, str]] = []
    for index, raw_message in enumerate(raw_messages):
        where = f"messages[{index}]"
        if not isinstance(raw_message, dict):
            raise invalid_request(f"'{where}' must be an object.", param=where)
        unknown = sorted(set(raw_message) - ALLOWED_MESSAGE_FIELDS)
        if unknown:
            raise invalid_request(
                f"Unsupported field in '{where}': {unknown[0]}.", param=where
            )
        role = raw_message.get("role")
        if role not in ALLOWED_ROLES:
            raise invalid_request(
                "Message role must be system, user, or assistant.",
                param=f"{where}.role",
            )
        content = raw_message.get("content")
        if not isinstance(content, str) or not content:
            raise invalid_request(
                "Message content must be a nonempty string.",
                param=f"{where}.content",
            )
        messages.append({"role": role, "content": content})
    return tuple(messages)


def parse_max_tokens(value: Any, application: ApplicationConfig) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise invalid_request(
            "'max_tokens' must be an integer.", param="max_tokens"
        )
    if value < 1 or value > application.max_output_tokens:
        raise invalid_request(
            f"'max_tokens' must be between 1 and {application.max_output_tokens}.",
            param="max_tokens",
        )
    return value


def parse_temperature(value: Any) -> float:
    """Validate a caller-requested sampling temperature in the closed 0..1 range.

    The value is returned unchanged so the backend receives exactly what the
    caller asked for. Python booleans are integers too, so they are rejected
    explicitly: they are not a temperature.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise invalid_request(
            "'temperature' must be a number between 0 and 1.",
            param="temperature",
        )
    if value < 0 or value > 1:
        raise invalid_request(
            "'temperature' must be between 0 and 1.", param="temperature"
        )
    return value


def parse_reasoning(value: Any) -> bool:
    """Validate whether the caller wants the backend's reasoning behaviour."""
    if not isinstance(value, bool):
        raise invalid_request("'reasoning' must be a boolean.", param="reasoning")
    return value


def resolve_upstream_model(
    alias: str,
    application: ApplicationConfig,
    alias_map: dict[str, str],
) -> str:
    """Map a public alias to the configured backend model name."""
    if alias not in alias_map:
        raise invalid_request(f"Unknown model alias: {alias}.", param="model")
    if alias not in application.models:
        raise model_not_allowed()
    return alias_map[alias]


def serialized_messages_length(messages: Sequence[dict[str, str]]) -> int:
    return len(
        json.dumps(list(messages), ensure_ascii=False, separators=(",", ":"))
    )


def enforce_context_budget(
    messages: Sequence[dict[str, str]], character_budget: int
) -> None:
    """Reject oversized inputs using a deliberately conservative character proxy.

    This is not tokenization. It is a cheap upper bound that keeps obviously
    oversized requests away from the backend until a tokenizer-based policy is
    validated against the deployed model.
    """
    if serialized_messages_length(messages) > character_budget:
        raise invalid_request(
            "Request messages exceed the gateway character budget for the "
            "backend context.",
            param="messages",
        )
