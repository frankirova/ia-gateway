"""Per-application model listing."""

from __future__ import annotations

from typing import Any, Callable

from conftest import (
    ALPHA_KEY,
    BETA_KEY,
    application_entry,
    gateway_document,
)


def test_models_lists_only_the_application_aliases(
    client: Any, alpha_headers: dict[str, str]
) -> None:
    response = client.get("/v1/models", headers=alpha_headers)

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {"id": "e4b", "object": "model", "created": 0, "owned_by": "homelab"}
        ],
    }
    assert response.headers["X-Request-ID"]


def test_models_scope_is_per_application(
    client_factory: Callable[..., Any],
) -> None:
    client = client_factory(
        gateway_document(
            applications=[
                application_entry("alpha", ALPHA_KEY, models=["e4b"]),
                application_entry("beta", BETA_KEY, models=["e4b", "other"]),
            ]
        )
    )

    alpha = client.get("/v1/models", headers={"Authorization": f"Bearer {ALPHA_KEY}"})
    beta = client.get("/v1/models", headers={"Authorization": f"Bearer {BETA_KEY}"})

    assert [entry["id"] for entry in alpha.json()["data"]] == ["e4b"]
    assert [entry["id"] for entry in beta.json()["data"]] == ["e4b", "other"]
