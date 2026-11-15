"""Configuration loading is strict and fails closed before serving."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from app.config import ConfigError, load_config
from app.main import create_app

from conftest import application_entry, gateway_document, write_config

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def test_create_app_fails_without_gateway_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GATEWAY_CONFIG", raising=False)

    with pytest.raises(ValueError):
        create_app()


def test_create_app_fails_when_config_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("GATEWAY_CONFIG", str(tmp_path / "absent.json"))

    with pytest.raises(ValueError):
        create_app()


def test_create_app_fails_when_config_is_not_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.json"
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("GATEWAY_CONFIG", str(path))

    with pytest.raises(ValueError):
        create_app()


@pytest.mark.parametrize(
    "digest",
    [
        "not-a-digest",
        "0" * 63,
        "0" * 65,
        "Z" * 64,
        "  " + "0" * 62,
    ],
)
def test_create_app_fails_on_malformed_key_digest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    digest: str,
) -> None:
    document = gateway_document(applications=[application_entry(key_sha256=digest)])
    path = write_config(tmp_path, document)
    monkeypatch.setenv("GATEWAY_CONFIG", str(path))

    with pytest.raises(ValueError):
        create_app()


def test_create_app_fails_on_empty_applications(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = write_config(tmp_path, gateway_document(applications=[]))
    monkeypatch.setenv("GATEWAY_CONFIG", str(path))

    with pytest.raises(ValueError):
        create_app()


@pytest.mark.parametrize(
    "document",
    [
        [],
        {"applications": [application_entry()]},
        {"upstream": {}, "applications": []},
        {"upstream": {}, "applications": [application_entry(), application_entry()]},
        {"upstream": {"base_url": "ftp://backend"}, "applications": [application_entry()]},
        {"upstream": {}, "applications": [application_entry(models=[])]},
        {
            "upstream": {},
            "applications": [application_entry(max_output_tokens=0)],
        },
        {
            "upstream": {},
            "applications": [application_entry(rate_capacity=0)],
        },
        {"upstream": {}, "applications": [application_entry()], "limits": {"context_char_budget": 0}},
        {"upstream": {}, "applications": [application_entry()], "unexpected": True},
    ],
)
def test_invalid_documents_fail_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, document: Any
) -> None:
    path = write_config(tmp_path, document)
    monkeypatch.setenv("GATEWAY_CONFIG", str(path))

    with pytest.raises(ValueError):
        create_app()


def test_upstream_defaults_are_applied(
    configure: Callable[..., Path],
) -> None:
    configure({"upstream": {}, "applications": [application_entry()]})

    config = load_config()

    assert config.upstream.base_url == "http://127.0.0.1:8190"
    assert config.upstream.model == "gemma-4-E4B-it-UD-Q4_K_XL"
    assert config.upstream.connect_timeout == 3
    assert config.upstream.read_timeout == 120
    assert config.upstream.api_key is None


def test_application_defaults_are_applied(configure: Callable[..., Path]) -> None:
    configure({"upstream": {}, "applications": [application_entry()]})

    application = load_config().applications[0]

    assert application.max_output_tokens == 512
    assert application.rate_capacity == 10
    assert application.rate_per_minute == 10


def test_upstream_api_key_is_optional(configure: Callable[..., Path]) -> None:
    configure(
        {
            "upstream": {"api_key": "backend-token-for-tests"},
            "applications": [application_entry()],
        }
    )

    assert load_config().upstream.api_key == "backend-token-for-tests"


def test_example_configuration_is_valid_and_contains_placeholders() -> None:
    config = load_config(REPOSITORY_ROOT / "config" / "gateway.example.json")

    assert config.applications
    for application in config.applications:
        assert len(application.key_sha256) == 64
        assert set(application.key_sha256) == {"0"}


def test_repository_contains_no_committed_gateway_config() -> None:
    assert not (REPOSITORY_ROOT / "config" / "gateway.json").exists()


def test_config_error_is_a_value_error() -> None:
    assert issubclass(ConfigError, ValueError)


def test_config_file_must_be_a_json_object(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "gateway.json"
    path.write_text(json.dumps([1, 2]), encoding="utf-8")
    monkeypatch.setenv("GATEWAY_CONFIG", str(path))

    with pytest.raises(ValueError):
        create_app()
