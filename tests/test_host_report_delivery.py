"""Delivery behaviour: compact message, deduplication key, one hub call.

Everything runs in-process against loopback doubles. No real hub, no WhatsApp
session, no network and no token on a command line.
"""

from __future__ import annotations

import pytest

from host_report_fake import (
    FakeContainer,
    FakeHubReply,
    closed_port_url,
    fake_docker,
    fake_gateway,
    fake_hub,
    garbage_hub,
    log_line,
)

SEND_TOKEN = "hub-send-token-000000000000000000000000000000"
MESSAGE_LIMIT = 4096

HOST_REPORT_ENV = (
    "HOST_REPORT_CONTAINER_LIMITS",
    "HOST_REPORT_DOCKER_URL",
    "HOST_REPORT_HOST",
    "HOST_REPORT_IGNORE",
    "HOST_REPORT_MAX_CLUSTERS",
    "HOST_REPORT_MAX_CONTAINERS",
    "HOST_REPORT_MAX_LINES",
    "HOST_REPORT_OVERFETCH",
    "HOST_REPORT_SINCE",
    "HOST_REPORT_TIMEOUT",
    "HOST_REPORT_PROMPT_BUDGET",
    "HOST_REPORT_GATEWAY_URL",
    "HOST_REPORT_GATEWAY_KEY_FILE",
    "HOST_REPORT_GATEWAY_CA_BUNDLE",
    "HOST_REPORT_GATEWAY_MODEL",
    "HOST_REPORT_MAX_TOKENS",
    "HOST_REPORT_TIMEOUT_SECONDS",
    "HOST_REPORT_NOTIFY",
    "HOST_REPORT_HUB_URL",
    "HOST_REPORT_HUB_TOKEN_FILE",
    "HOST_REPORT_HUB_CA_BUNDLE",
    "HOST_REPORT_HUB_CHAT",
    "HOST_REPORT_HUB_TIMEOUT_SECONDS",
    "HOST_REPORT_NOTIFY_KEY",
)

WINDOW = {
    "since": "2026-11-11T21:08:23Z",
    "until": "2026-11-12T21:08:23Z",
    "duration": "24h",
}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch):
    for name in HOST_REPORT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOST_REPORT_HOST", "test-host")
    return None


def digest_with(**overrides) -> dict:
    container = {
        "name": "dockge-dockge-1",
        "state": "running",
        "restart_count": 0,
        "exit_code": None,
        "total_lines": 2000,
        "error_lines": 574,
        "warning_lines": 0,
        "clusters": [],
    }
    document = {
        "schema_version": 1,
        "host": "server",
        "window": dict(WINDOW),
        "containers": [container],
        "skipped": [],
        "truncations": [],
    }
    document.update(overrides)
    return document


def interpretation(summary: str = "One container is failing.", **overrides) -> dict:
    document = {
        "summary": summary,
        "findings": [
            {
                "container": "dockge-dockge-1",
                "severity": "high",
                "what": "Process exited with code 1.",
                "why": "The status lookup cannot reach its service.",
                "action": "Check the missing environment variable.",
            }
        ],
    }
    document.update(overrides)
    return document


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "send.token"
    path.write_text(SEND_TOKEN, encoding="utf-8")
    return path


def hub_settings(token_file, *, base_url=None, **overrides):
    from tasks.host_report.delivery import HubSettings

    values = {
        "base_url": base_url,
        "token_file": str(token_file),
        "chat": "owner",
        "timeout_seconds": 5.0,
    }
    values.update(overrides)
    return HubSettings(**values)


def settings_for(argv: list[str], env: dict[str, str]):
    from tasks.host_report.cli import build_parser
    from tasks.host_report.settings import build_settings

    return build_settings(build_parser().parse_args(argv), env)


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]):
    from tasks.host_report.cli import main

    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# --- the message ---------------------------------------------------------


def test_message_carries_host_totals_summary_and_findings():
    from tasks.host_report.delivery import render_message

    message = render_message(
        digest_with(),
        model_status="used",
        interpretation=interpretation(),
    )

    assert message.splitlines()[0] == "🖥️ Servidor: server · 24h"
    assert "📊 2000 líneas · 574 errores · 0 reinicios" in message
    assert "One container is failing." in message
    assert "1. 🔴 dockge-dockge-1 (alta): Process exited with code 1." in message
    assert "↳ Por qué: The status lookup cannot reach its service." in message
    assert "↳ Acción: Check the missing environment variable." in message


def test_message_says_no_news_when_nothing_to_report():
    from tasks.host_report.delivery import render_message

    message = render_message(
        digest_with(),
        model_status="skipped",
        model_reason="no container showed activity",
    )

    assert message.splitlines()[0] == "🖥️ Servidor: server · 24h"
    assert "Sin novedades" in message
    assert "2000 líneas · 574 errores" in message


def test_message_explains_an_unavailable_interpretation():
    from tasks.host_report.delivery import render_message

    message = render_message(
        digest_with(),
        model_status="unavailable",
        model_reason="gateway request timed out",
    )

    assert "Sin interpretación: gateway request timed out." in message
    assert "574 errores" in message


def test_worst_case_legal_message_fits_a_single_hub_message():
    from tasks.host_report.delivery import render_message

    long = "x" * 150
    worst = {
        "summary": "s" * 300,
        "findings": [
            {
                "container": "c" * 200,
                "severity": "high",
                "what": long,
                "why": long,
                "action": long,
            }
            for _ in range(3)
        ],
    }
    message = render_message(
        digest_with(), model_status="used", interpretation=worst
    )

    assert len(message) <= MESSAGE_LIMIT


# --- the deduplication key ----------------------------------------------


def test_deduplication_key_uses_the_host_and_the_window_minute():
    from tasks.host_report.delivery import deduplication_key

    assert deduplication_key(digest_with()) == (
        "host-report:server:2026-11-12T21:08"
    )


def test_deduplication_key_honours_an_explicit_value():
    from tasks.host_report.delivery import deduplication_key

    assert deduplication_key(digest_with(), explicit="n8n-slot-42") == "n8n-slot-42"


# --- one hub call -------------------------------------------------------


def test_deliver_posts_bearer_token_chat_text_and_deduplication_key(token_file):
    from tasks.host_report.delivery import deliver

    with fake_hub() as hub:
        result = deliver(
            hub_settings(token_file, base_url=hub.base_url),
            "hello",
            "host-report:server:2026-11-12T21:08",
        )

        assert result == "sent"
        assert hub.call_count == 1
        request = hub.last_request
        assert request["method"] == "POST"
        assert request["path"] == "/send"
        assert request["headers"]["authorization"] == f"Bearer {SEND_TOKEN}"
        assert request["body"] == {
            "chat": "owner",
            "text": "hello",
            "deduplication_key": "host-report:server:2026-11-12T21:08",
        }


def test_deliver_reports_a_duplicate_without_resending(token_file):
    from tasks.host_report.delivery import deliver

    with fake_hub(FakeHubReply(body={"status": "duplicate"})) as hub:
        result = deliver(
            hub_settings(token_file, base_url=hub.base_url), "hello", "key-1"
        )

        assert result == "duplicate"
        assert hub.call_count == 1


def test_deliver_fails_on_401_without_leaking_the_token(token_file):
    from tasks.host_report.delivery import DeliveryError, deliver

    with fake_hub(FakeHubReply(status=401, body={"detail": "unauthorized"})) as hub:
        with pytest.raises(DeliveryError) as raised:
            deliver(
                hub_settings(token_file, base_url=hub.base_url), "hello", "key-1"
            )

    assert "401" in raised.value.reason
    assert SEND_TOKEN not in raised.value.reason
    assert "unauthorized" not in raised.value.reason


def test_deliver_does_not_retry_a_502(token_file):
    from tasks.host_report.delivery import DeliveryError, deliver

    with fake_hub(FakeHubReply(status=502, body={"detail": "waha failed"})) as hub:
        with pytest.raises(DeliveryError):
            deliver(
                hub_settings(token_file, base_url=hub.base_url), "hello", "key-1"
            )

        assert hub.call_count == 1


def test_deliver_requires_a_token_file(tmp_path):
    from tasks.host_report.delivery import DeliveryError, deliver

    with pytest.raises(DeliveryError) as raised:
        deliver(
            hub_settings(tmp_path / "absent.token", base_url="http://127.0.0.1:1"),
            "hello",
            "key-1",
        )

    assert "send token file" in raised.value.reason


def test_deliver_reports_a_refused_connection(token_file):
    from tasks.host_report.delivery import DeliveryError, deliver

    with pytest.raises(DeliveryError) as raised:
        deliver(hub_settings(token_file, base_url=closed_port_url()), "hello", "key-1")

    assert "connection failed" in raised.value.reason


def test_deliver_refuses_a_message_the_hub_cannot_take(token_file):
    from tasks.host_report.delivery import MESSAGE_LIMIT, DeliveryError, deliver

    with fake_hub() as hub:
        with pytest.raises(DeliveryError) as raised:
            deliver(
                hub_settings(token_file, base_url=hub.base_url),
                "x" * (MESSAGE_LIMIT + 1),
                "key-1",
            )

        assert "longer than the hub accepts" in raised.value.reason
        assert hub.call_count == 0


# --- settings -----------------------------------------------------------


def test_settings_need_a_markdown_report(tmp_path):
    from tasks.host_report.settings import SettingsError

    env = {
        "HOST_REPORT_HUB_URL": "http://127.0.0.1:8000",
        "HOST_REPORT_HUB_TOKEN_FILE": str(tmp_path / "send.token"),
    }
    with pytest.raises(SettingsError) as raised:
        settings_for(["--notify", "hub"], env)

    assert "--report markdown" in str(raised.value)


def test_settings_need_a_hub_url_and_a_token_file():
    from tasks.host_report.settings import SettingsError

    with pytest.raises(SettingsError) as missing_url:
        settings_for(
            ["--report", "markdown", "--notify", "hub"],
            {"HOST_REPORT_HUB_TOKEN_FILE": "/tmp/send.token"},
        )
    assert "HOST_REPORT_HUB_URL" in str(missing_url.value)

    with pytest.raises(SettingsError) as missing_token:
        settings_for(
            ["--report", "markdown", "--notify", "hub"],
            {"HOST_REPORT_HUB_URL": "http://127.0.0.1:8000"},
        )
    assert "HOST_REPORT_HUB_TOKEN_FILE" in str(missing_token.value)


def test_settings_default_to_no_delivery():
    settings = settings_for([], {})
    assert settings.hub is None


def test_settings_reject_an_https_hub_without_a_ca_bundle(tmp_path):
    from tasks.host_report.settings import SettingsError

    env = {
        "HOST_REPORT_HUB_URL": "https://hub.internal",
        "HOST_REPORT_HUB_TOKEN_FILE": str(tmp_path / "send.token"),
    }
    with pytest.raises(SettingsError) as raised:
        settings_for(["--report", "markdown", "--notify", "hub"], env)

    assert "HOST_REPORT_HUB_CA_BUNDLE" in str(raised.value)


# --- the command line ---------------------------------------------------


def test_cli_sends_one_message_and_logs_the_status(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    container = FakeContainer(
        id="d1" * 32, name="api", log_lines=[log_line("upstream failed: exit 1")]
    )
    token = tmp_path / "send.token"
    token.write_text(SEND_TOKEN, encoding="utf-8")

    with fake_docker(container) as docker, fake_gateway() as gateway, fake_hub() as hub:
        monkeypatch.setenv("HOST_REPORT_GATEWAY_URL", gateway.base_url)
        key_path = tmp_path / "application.key"
        key_path.write_text("gateway-key-0000000000000000000000000000000000")
        monkeypatch.setenv("HOST_REPORT_GATEWAY_KEY_FILE", str(key_path))
        monkeypatch.setenv("HOST_REPORT_HUB_URL", hub.base_url)
        monkeypatch.setenv("HOST_REPORT_HUB_TOKEN_FILE", str(token))

        code, out, err = run_cli(
            [
                "--docker-url",
                docker.base_url,
                "--report",
                "markdown",
                "--notify",
                "hub",
            ],
            capsys,
        )

        assert code == 0, err
        assert out.startswith("# Host log report:")
        assert "host_report: hub status=200" in err
        assert "result=sent" in err
        assert hub.call_count == 1
        body = hub.last_request["body"]
        assert body["chat"] == "owner"
        assert body["deduplication_key"].startswith("host-report:")
        assert "Servidor:" in body["text"]
        assert SEND_TOKEN not in err


def test_cli_exits_5_when_delivery_fails_and_keeps_the_report(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    container = FakeContainer(
        id="d1" * 32, name="api", log_lines=[log_line("upstream failed: exit 1")]
    )
    token = tmp_path / "send.token"
    token.write_text(SEND_TOKEN, encoding="utf-8")
    key_path = tmp_path / "application.key"
    key_path.write_text("gateway-key-0000000000000000000000000000000000")

    with fake_docker(container) as docker, fake_gateway() as gateway:
        monkeypatch.setenv("HOST_REPORT_GATEWAY_URL", gateway.base_url)
        monkeypatch.setenv("HOST_REPORT_GATEWAY_KEY_FILE", str(key_path))
        monkeypatch.setenv("HOST_REPORT_HUB_URL", closed_port_url())
        monkeypatch.setenv("HOST_REPORT_HUB_TOKEN_FILE", str(token))

        code, out, err = run_cli(
            [
                "--docker-url",
                docker.base_url,
                "--report",
                "markdown",
                "--notify",
                "hub",
            ],
            capsys,
        )

    assert code == 5
    assert out.startswith("# Host log report:")
    assert "host_report: delivery failed" in err


def test_deliver_reports_a_transport_failure_that_is_not_an_oserror(token_file):
    """A reply that is not HTTP at all must not escape as a traceback."""
    from tasks.host_report.delivery import DeliveryError, deliver

    with garbage_hub() as url:
        with pytest.raises(DeliveryError) as raised:
            deliver(hub_settings(token_file, base_url=url), "hello", "key-1")

    assert raised.value.reason == "hub request failed"


def test_cli_exits_5_on_a_transport_failure(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    container = FakeContainer(
        id="d1" * 32, name="api", log_lines=[log_line("upstream failed: exit 1")]
    )

    with fake_docker(container) as docker, fake_gateway() as gateway:
        with garbage_hub() as url:
            configure_hub_cli(monkeypatch, tmp_path, gateway, url)

            code, out, err = run_cli(
                [
                    "--docker-url",
                    docker.base_url,
                    "--report",
                    "markdown",
                    "--notify",
                    "hub",
                ],
                capsys,
            )

    assert code == 5
    assert out.startswith("# Host log report:")
    assert "host_report: delivery failed: hub request failed" in err
    assert "Traceback" not in err


def test_cli_refuses_a_redirect_and_the_token_does_not_follow(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    container = FakeContainer(
        id="d1" * 32, name="api", log_lines=[log_line("upstream failed: exit 1")]
    )

    with fake_docker(container) as docker, fake_gateway() as gateway:
        with fake_hub() as target:
            redirect = FakeHubReply(
                status=302, headers={"Location": target.base_url + "/send"}
            )
            with fake_hub(redirect) as hub:
                configure_hub_cli(monkeypatch, tmp_path, gateway, hub.base_url)

                code, out, err = run_cli(
                    [
                        "--docker-url",
                        docker.base_url,
                        "--report",
                        "markdown",
                        "--notify",
                        "hub",
                    ],
                    capsys,
                )

    assert code == 5
    assert "hub returned HTTP 302" in err
    assert hub.call_count == 1
    assert target.call_count == 0
    assert SEND_TOKEN not in err
    assert SEND_TOKEN not in out


def test_cli_401_exits_5_naming_only_the_status(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    container = FakeContainer(
        id="d1" * 32, name="api", log_lines=[log_line("upstream failed: exit 1")]
    )
    marker = "unauthorized-body-marker"

    with fake_docker(container) as docker, fake_gateway() as gateway:
        with fake_hub(FakeHubReply(status=401, body={"detail": marker})) as hub:
            configure_hub_cli(monkeypatch, tmp_path, gateway, hub.base_url)

            code, out, err = run_cli(
                [
                    "--docker-url",
                    docker.base_url,
                    "--report",
                    "markdown",
                    "--notify",
                    "hub",
                ],
                capsys,
            )

    assert code == 5
    assert out.startswith("# Host log report:")
    lines = err.strip().splitlines()
    hub_line = next(
        line for line in lines if line.startswith("host_report: hub status=")
    )
    assert hub_line.startswith("host_report: hub status=401 elapsed=")
    assert lines[lines.index(hub_line) + 1] == (
        "host_report: delivery failed: hub returned HTTP 401"
    )
    assert marker not in err
    assert SEND_TOKEN not in err
    assert hub.call_count == 1


def test_hub_settings_rejections_exit_2_before_collecting(
    monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("HOST_REPORT_HUB_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("HOST_REPORT_HUB_TOKEN_FILE", "/tmp/send.token")

    code, out, err = run_cli(["--notify", "hub"], capsys)

    assert code == 2
    assert out == ""
    assert "requires --report markdown" in err

    monkeypatch.setenv("HOST_REPORT_HUB_URL", "https://hub.internal")
    code, out, err = run_cli(["--report", "markdown", "--notify", "hub"], capsys)

    assert code == 2
    assert out == ""
    assert "TLS verification is never disabled" in err


def test_deduplication_key_ignores_an_empty_explicit_value(tmp_path):
    env = {
        "HOST_REPORT_HUB_URL": "http://127.0.0.1:8000",
        "HOST_REPORT_HUB_TOKEN_FILE": str(tmp_path / "send.token"),
        "HOST_REPORT_NOTIFY_KEY": "",
    }
    settings = settings_for(["--report", "markdown", "--notify", "hub"], env)

    assert settings.hub.deduplication_key is None

    from tasks.host_report.delivery import deduplication_key

    assert deduplication_key(digest_with()) == "host-report:server:2026-11-12T21:08"
    assert deduplication_key(digest_with(host="other")) == (
        "host-report:other:2026-11-12T21:08"
    )


def test_cli_honours_the_scheduler_deduplication_key(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    container = FakeContainer(
        id="d1" * 32, name="api", log_lines=[log_line("upstream failed: exit 1")]
    )

    with fake_docker(container) as docker, fake_gateway() as gateway, fake_hub() as hub:
        configure_hub_cli(monkeypatch, tmp_path, gateway, hub.base_url)
        monkeypatch.setenv("HOST_REPORT_NOTIFY_KEY", "n8n-slot-42")

        code, _, err = run_cli(
            [
                "--docker-url",
                docker.base_url,
                "--report",
                "markdown",
                "--notify",
                "hub",
            ],
            capsys,
        )

    assert code == 0, err
    assert hub.call_count == 1
    assert hub.last_request["body"]["deduplication_key"] == "n8n-slot-42"


def test_no_news_run_still_delivers(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A quiet window reports that it is quiet instead of staying silent."""
    container = FakeContainer(id="d1" * 32, name="api", log_lines=[log_line("hello")])
    token = tmp_path / "send.token"
    token.write_text(SEND_TOKEN, encoding="utf-8")

    with fake_docker(container) as docker, fake_hub() as hub:
        monkeypatch.setenv("HOST_REPORT_HUB_URL", hub.base_url)
        monkeypatch.setenv("HOST_REPORT_HUB_TOKEN_FILE", str(token))

        code, out, err = run_cli(
            [
                "--docker-url",
                docker.base_url,
                "--report",
                "markdown",
                "--notify",
                "hub",
            ],
            capsys,
        )

    assert code == 0, err
    assert "Nothing to report" in out
    assert hub.call_count == 1
    assert "Sin novedades" in hub.last_request["body"]["text"]


def configure_hub_cli(monkeypatch, tmp_path, gateway, hub_url) -> None:
    """Point the CLI at the fake gateway and a hub URL, fresh files included."""
    key_path = tmp_path / "gateway.key"
    key_path.write_text("gateway-key-0000000000000000000000000000000000")
    token = tmp_path / "send.token"
    token.write_text(SEND_TOKEN, encoding="utf-8")
    monkeypatch.setenv("HOST_REPORT_GATEWAY_URL", gateway.base_url)
    monkeypatch.setenv("HOST_REPORT_GATEWAY_KEY_FILE", str(key_path))
    monkeypatch.setenv("HOST_REPORT_HUB_URL", hub_url)
    monkeypatch.setenv("HOST_REPORT_HUB_TOKEN_FILE", str(token))


# --- measured facts in the message -------------------------------------- #


def host_finding(kind: str, message: str, **values) -> dict:
    return {
        "code": f"{kind or 'mystery'}:test",
        "severity": "warning",
        "message": message,
        "kind": kind,
        "values": values,
    }


KIND_LINES = {
    "disk": (
        host_finding("disk", "disk / at 92% used", mount="/", used_percent=92),
        "💾 Disco / al 92%",
    ),
    "memory": (
        host_finding(
            "memory", "memory available 4%", available_percent=4
        ),
        "🧠 Memoria disponible 4%",
    ),
    "load": (
        host_finding("load", "load 3.20 above 2 cpu", load1=3.2, cpu_count=2),
        "⚙️ Carga 3.20 con 2 CPU",
    ),
    "endpoint-down": (
        host_finding(
            "endpoint",
            "endpoint api refused",
            name="api",
            reason="refused",
            status=None,
        ),
        "🔌 api no responde (refused)",
    ),
    "endpoint-status": (
        host_finding(
            "endpoint",
            "endpoint api status_mismatch 502",
            name="api",
            reason="status_mismatch",
            status=502,
        ),
        "🔌 api respondió 502",
    ),
    "unknown-kind": (
        host_finding("swap", "swap almost exhausted"),
        "⚠️ swap almost exhausted",
    ),
    "no-kind": (host_finding("", "something odd"), "⚠️ something odd"),
}


@pytest.mark.parametrize(
    "entry,expected", KIND_LINES.values(), ids=KIND_LINES
)
def test_every_measured_kind_renders_a_spanish_line(entry, expected):
    from tasks.host_report.delivery import render_message

    message = render_message(
        digest_with(host_findings=[entry]),
        model_status="used",
        interpretation=interpretation(),
    )

    lines = message.splitlines()
    assert lines[1] == "📊 2000 líneas · 574 errores · 0 reinicios"
    assert lines[2] == expected
    assert lines[3] == ""
    assert "One container is failing." in message


UNREACHABLE_LINES = {
    "ssh-timeout": (
        "ssh_timeout",
        "⚠️ No pude relevar la VPS: no respondió por SSH",
    ),
    "ssh-failed": (
        "ssh_failed",
        "⚠️ No pude relevar la VPS: falló la conexión SSH",
    ),
    "docker-unavailable": (
        "docker_unavailable",
        "⚠️ No pude relevar la VPS: no pude leer Docker",
    ),
    "invalid-bundle": (
        "invalid_bundle",
        "⚠️ No pude relevar la VPS: respuesta inválida",
    ),
    "too-large": (
        "too_large",
        "⚠️ No pude relevar la VPS: la respuesta fue demasiado grande",
    ),
}


@pytest.mark.parametrize(
    "code,expected", UNREACHABLE_LINES.values(), ids=UNREACHABLE_LINES
)
def test_unreachable_reason_renders_spanish_and_never_the_code(code, expected):
    from tasks.host_report.delivery import render_message

    message = render_message(
        digest_with(unreachable=code),
        model_status="skipped",
        model_reason="measured findings without container activity",
    )

    assert message.splitlines()[2] == expected
    assert code not in message


def test_four_measured_findings_render_only_three_lines():
    from tasks.host_report.delivery import render_message

    findings = [
        host_finding("disk", "disk / at 92% used", mount="/", used_percent=92),
        host_finding("memory", "memory available 4%", available_percent=4),
        host_finding("load", "load 3.20 above 2 cpu", load1=3.2, cpu_count=2),
        host_finding(
            "disk", "disk /var at 97% used", mount="/var", used_percent=97
        ),
    ]

    message = render_message(
        digest_with(host_findings=findings),
        model_status="used",
        interpretation=interpretation(),
    )

    lines = message.splitlines()
    assert lines[2:5] == [
        "💾 Disco / al 92%",
        "🧠 Memoria disponible 4%",
        "⚙️ Carga 3.20 con 2 CPU",
    ]
    assert "/var" not in message


def test_unreachable_and_findings_share_the_three_line_budget():
    from tasks.host_report.delivery import render_message

    findings = [
        host_finding("disk", "disk / at 92% used", mount="/", used_percent=92),
        host_finding("memory", "memory available 4%", available_percent=4),
        host_finding("load", "load 3.20 above 2 cpu", load1=3.2, cpu_count=2),
    ]

    message = render_message(
        digest_with(unreachable="ssh_timeout", host_findings=findings),
        model_status="used",
        interpretation=interpretation(),
    )

    lines = message.splitlines()
    assert lines[2:5] == [
        "⚠️ No pude relevar la VPS: no respondió por SSH",
        "💾 Disco / al 92%",
        "🧠 Memoria disponible 4%",
    ]
    assert "Carga 3.20" not in message


def test_failing_probes_render_after_the_host_findings():
    from tasks.host_report.delivery import render_message

    probes = [
        {
            "name": "web",
            "url": "https://web.internal/",
            "ok": False,
            "status": None,
            "latency_ms": None,
            "reason": "timeout",
        },
        {
            "name": "api",
            "url": "https://api.internal/",
            "ok": True,
            "status": 200,
            "latency_ms": 12,
            "reason": "ok",
        },
    ]

    message = render_message(
        digest_with(
            probes=probes,
            host_findings=[
                host_finding(
                    "disk", "disk / at 92% used", mount="/", used_percent=92
                )
            ],
        ),
        model_status="used",
        interpretation=interpretation(),
    )

    lines = message.splitlines()
    assert lines[2:4] == ["💾 Disco / al 92%", "🔌 web no responde (timeout)"]
    assert "api" not in message.splitlines()[3]


def test_a_failing_endpoint_is_not_rendered_twice():
    """The collector emits the same failure as a finding and as a probe."""
    from tasks.host_report.delivery import render_message

    entry = host_finding(
        "endpoint",
        "endpoint api refused",
        name="api",
        reason="refused",
        status=None,
    )
    probe = {
        "name": "api",
        "url": "https://api.internal/",
        "ok": False,
        "status": None,
        "latency_ms": None,
        "reason": "refused",
    }

    message = render_message(
        digest_with(host_findings=[entry], probes=[probe]),
        model_status="used",
        interpretation=interpretation(),
    )

    assert message.count("🔌 api no responde (refused)") == 1


def test_worst_case_legal_message_with_measured_lines_fits_the_limit():
    from tasks.host_report.delivery import render_message

    long = "x" * 150
    worst = {
        "summary": "s" * 300,
        "findings": [
            {
                "container": "c" * 150,
                "severity": "high",
                "what": long,
                "why": long,
                "action": long,
            }
            for _ in range(3)
        ],
    }
    findings = [
        host_finding("disk", "disk /mnt/x at 92% used", mount="/mnt/x", used_percent=92),
        host_finding("memory", "memory available 4%", available_percent=4),
        host_finding("load", "load 3.20 above 2 cpu", load1=3.2, cpu_count=2),
    ]

    message = render_message(
        digest_with(unreachable="ssh_timeout", host_findings=findings),
        model_status="used",
        interpretation=worst,
    )

    assert len(message) <= MESSAGE_LIMIT


def test_oversized_message_drops_measured_lines_before_anything_else():
    """The interpretation is never truncated; measured lines go first."""
    from tasks.host_report.delivery import render_message

    mount = "/" + "m" * 4000
    findings = [
        host_finding("disk", "disk near full", mount=mount, used_percent=99),
        host_finding("memory", "memory available 4%", available_percent=4),
    ]

    message = render_message(
        digest_with(unreachable="ssh_timeout", host_findings=findings),
        model_status="used",
        interpretation=interpretation(),
    )

    assert len(message) <= MESSAGE_LIMIT
    assert "One container is failing." in message
    assert "↳ Por qué: The status lookup cannot reach its service." in message
    assert "↳ Acción: Check the missing environment variable." in message
    assert "Memoria disponible" not in message
    assert mount not in message
    assert "⚠️ No pude relevar la VPS: no respondió por SSH" in message


def test_message_without_the_new_keys_is_unchanged():
    from tasks.host_report.delivery import render_message

    message = render_message(
        digest_with(), model_status="used", interpretation=interpretation()
    )

    assert message == "\n".join(
        [
            "🖥️ Servidor: server · 24h",
            "📊 2000 líneas · 574 errores · 0 reinicios",
            "",
            "One container is failing.",
            "",
            "1. 🔴 dockge-dockge-1 (alta): Process exited with code 1.",
            "   ↳ Por qué: The status lookup cannot reach its service.",
            "   ↳ Acción: Check the missing environment variable.",
        ]
    )


# --- an unreachable source ---------------------------------------------- #


def unreachable_environment(monkeypatch, tmp_path) -> None:
    """A remote target whose SSH binary does not exist: ssh_failed, no network."""
    monkeypatch.setenv("HOST_REPORT_TARGET", "ssh")
    monkeypatch.setenv("HOST_REPORT_SSH_TARGET", "vps.internal")
    monkeypatch.setenv("HOST_REPORT_SSH_BINARY", str(tmp_path / "absent-ssh"))


def test_cli_reports_a_sent_unreachable_report_and_exits_one(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """The hub call is a seam here; the HTTP path has its own hub tests."""
    token = tmp_path / "send.token"
    token.write_text(SEND_TOKEN, encoding="utf-8")
    calls: list[dict] = []

    def fake_deliver(settings, text, key, *, diagnostics=None):
        calls.append({"settings": settings, "text": text, "key": key})
        return "sent"

    unreachable_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("HOST_REPORT_HUB_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOST_REPORT_HUB_TOKEN_FILE", str(token))
    monkeypatch.setattr("tasks.host_report.cli.deliver", fake_deliver)

    code, out, err = run_cli(["--report", "markdown", "--notify", "hub"], capsys)

    assert code == 1, err
    assert len(calls) == 1, err
    assert out.startswith("# Host log report:")
    assert "VPS unreachable: ssh failed" in out
    assert "host_report: VPS unreachable: ssh failed" in err
    assert "hub result=sent" in err
    assert "⚠️ No pude relevar la VPS: falló la conexión SSH" in calls[0]["text"]
    assert calls[0]["key"].startswith("host-report:")


def test_cli_keeps_the_delivery_failure_code_for_an_unreachable_digest(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    """A failed delivery is a delivery failure, not an unreachable source."""
    from tasks.host_report.delivery import DeliveryError

    token = tmp_path / "send.token"
    token.write_text(SEND_TOKEN, encoding="utf-8")

    def failing_deliver(settings, text, key, *, diagnostics=None):
        raise DeliveryError("hub returned HTTP 502")

    unreachable_environment(monkeypatch, tmp_path)
    monkeypatch.setenv("HOST_REPORT_HUB_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("HOST_REPORT_HUB_TOKEN_FILE", str(token))
    monkeypatch.setattr("tasks.host_report.cli.deliver", failing_deliver)

    code, out, err = run_cli(["--report", "markdown", "--notify", "hub"], capsys)

    assert code == 5
    assert out.startswith("# Host log report:")
    assert "host_report: delivery failed: hub returned HTTP 502" in err
    assert "VPS unreachable" not in err


def test_cli_writes_the_unreachable_report_and_exits_one(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    unreachable_environment(monkeypatch, tmp_path)

    code, out, err = run_cli(["--report", "markdown"], capsys)

    assert code == 1
    assert out.startswith("# Host log report:")
    assert "VPS unreachable: ssh failed" in out
    assert "host_report: VPS unreachable: ssh failed" in err
    assert "Traceback" not in err


def test_cli_exits_one_after_the_json_digest_of_an_unreachable_source(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys
):
    import json

    unreachable_environment(monkeypatch, tmp_path)

    code, out, err = run_cli(["--format", "json"], capsys)

    assert code == 1
    digest = json.loads(out)
    assert digest["unreachable"] == "ssh_failed"
    assert "host_report: VPS unreachable: ssh failed" in err


def test_cli_exits_zero_for_a_reachable_local_source(capsys):
    container = FakeContainer(id="d1" * 32, name="api", log_lines=[log_line("hello")])

    with fake_docker(container) as docker:
        code, out, err = run_cli(
            ["--docker-url", docker.base_url, "--report", "markdown"], capsys
        )

    assert code == 0, err
    assert out.startswith("# Host log report:")
    assert err == ""


def test_a_skipped_model_does_not_call_measured_news_no_news():
    """A full disk is not "sin novedades" just because no log qualified."""
    from tasks.host_report.delivery import NO_LOG_NEWS, NO_NEWS, render_message

    message = render_message(
        digest_with(
            host_findings=[
                host_finding("disk", "disk / at 96% used", mount="/", used_percent=96)
            ]
        ),
        model_status="skipped",
        model_reason="measured findings without container activity",
    )

    assert "💾 Disco / al 96%" in message
    assert NO_LOG_NEWS in message
    assert NO_NEWS not in message


def test_a_skipped_model_without_measured_news_still_says_no_news():
    from tasks.host_report.delivery import NO_LOG_NEWS, NO_NEWS, render_message

    message = render_message(digest_with(), model_status="skipped")

    assert NO_NEWS in message
    assert NO_LOG_NEWS not in message
