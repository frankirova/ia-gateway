"""S2/S7 acceptance: a new application is added by configuration only.

The gateway must stay task-agnostic. These tests prove the two properties a
reviewer cannot see from the configuration-change test alone: a second
application reuses the same alias with its own credential, and the service
source carries no knowledge of any particular task.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

FIRST_KEY = "first-application-key-00000000000000000000000000000000000000"
SECOND_KEY = "second-application-key-0000000000000000000000000000000000000"
UPSTREAM_BASE_URL = "http://upstream.test:8190"
UPSTREAM_MODEL = "test-model"
TASK_VOCABULARY = (
    "whatsapp",
    "telegram",
    "journald",
    "n8n",
    "log_analysis",
    "daily_report",
    "backup",
)


def entry(application_id: str, key: str) -> dict:
    return {
        "id": application_id,
        "key_sha256": hashlib.sha256(key.encode("ascii")).hexdigest(),
        "models": ["e4b"],
    }


def document() -> dict:
    return {
        "upstream": {"base_url": UPSTREAM_BASE_URL, "model": UPSTREAM_MODEL},
        "applications": [entry("logs", FIRST_KEY), entry("alerts", SECOND_KEY)],
    }


def call(client, key: str):
    return client.post(
        "/v1/chat/completions",
        json={"model": "e4b", "messages": [{"role": "user", "content": "Summarize."}]},
        headers={"Authorization": f"Bearer {key}"},
    )


def test_two_applications_share_one_alias_with_their_own_credentials(
    client_factory, upstream
):
    client = client_factory(document())

    first, second = call(client, FIRST_KEY), call(client, SECOND_KEY)

    assert (first.status_code, second.status_code) == (200, 200)
    assert first.json()["model"] == second.json()["model"] == "e4b"
    assert upstream.call_count == 2
    assert {body["model"] for body in upstream.bodies} == {UPSTREAM_MODEL}


def test_each_application_sees_only_its_own_aliases(client_factory):
    scoped = document()
    scoped["applications"][1]["models"] = ["other"]

    client = client_factory(scoped)

    assert [item["id"] for item in client.get(
        "/v1/models", headers={"Authorization": f"Bearer {FIRST_KEY}"}
    ).json()["data"]] == ["e4b"]
    assert [item["id"] for item in client.get(
        "/v1/models", headers={"Authorization": f"Bearer {SECOND_KEY}"}
    ).json()["data"]] == ["other"]
    assert call(client, SECOND_KEY).status_code == 403


def test_gateway_source_carries_no_task_specific_knowledge():
    source_root = Path(__file__).resolve().parents[1] / "app"

    offenders = [
        f"{path.name}: {word}"
        for path in sorted(source_root.rglob("*.py"))
        for word in TASK_VOCABULARY
        if word in path.read_text(encoding="utf-8").lower()
    ]

    assert offenders == []
