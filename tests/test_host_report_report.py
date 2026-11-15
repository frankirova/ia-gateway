"""Report behaviour: clustering, redaction, ordering, digest schema, text format."""

from __future__ import annotations

import json

import pytest

from host_report_fake import FakeContainer, fake_docker, log_line

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
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch):
    for name in HOST_REPORT_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOST_REPORT_HOST", "test-host")
    return None


def run_cli(argv: list[str], capsys: pytest.CaptureFixture[str]):
    from tasks.host_report.cli import main

    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def report_for(api, capsys, *extra: str) -> dict:
    code, out, err = run_cli(["--docker-url", api.base_url, *extra], capsys)
    assert code == 0, err
    return json.loads(out)


def container_by_name(report: dict, name: str) -> dict:
    for entry in report["containers"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"container {name!r} missing from report: {report}")


def test_digest_schema(capsys):
    container = FakeContainer(id="d1" * 32, name="api", log_lines=[log_line("hello")])
    with fake_docker(container) as api:
        report = report_for(api, capsys)

    assert set(report) == {
        "schema_version",
        "host",
        "window",
        "containers",
        "skipped",
        "truncations",
    }
    assert set(report["window"]) >= {"since", "until", "duration"}
    entry = container_by_name(report, "api")
    assert set(entry) == {
        "name",
        "image",
        "state",
        "restart_count",
        "exit_code",
        "lines_read",
        "ignored_lines",
        "total_lines",
        "error_lines",
        "warning_lines",
        "clusters",
    }
    cluster = entry["clusters"][0]
    assert set(cluster) == {
        "template",
        "count",
        "first_seen",
        "last_seen",
        "severity",
        "sample",
    }
    assert report["window"]["since"].endswith("Z")
    assert report["window"]["until"].endswith("Z")


def test_clusters_group_lines_differing_only_by_volatile_parts(capsys):
    lines = [
        log_line(
            "worker 4a1b2c3d4e5f processed job 12 in 1.5s",
            ts="2024-05-01T10:00:01.000000001Z",
        ),
        log_line(
            "worker 9f8e7d6c5b4a processed job 47 in 2s",
            ts="2024-05-01T10:05:02.000000002Z",
        ),
        log_line(
            "worker 1234567890ab processed job 108 in 12s",
            ts="2024-05-01T10:09:03.000000003Z",
        ),
    ]
    container = FakeContainer(id="d2" * 32, name="worker", log_lines=lines)
    with fake_docker(container) as api:
        report = report_for(api, capsys)

    entry = container_by_name(report, "worker")
    assert len(entry["clusters"]) == 1
    cluster = entry["clusters"][0]
    assert cluster["count"] == 3
    assert cluster["severity"] == "info"
    assert cluster["first_seen"] == "2024-05-01T10:00:01.000000Z"
    assert cluster["last_seen"] == "2024-05-01T10:09:03.000000Z"
    assert "worker" in cluster["template"]
    assert "4a1b2c3d4e5f" not in cluster["template"]
    assert "1234567890ab" not in cluster["template"]
    assert "108" not in cluster["template"]
    assert cluster["sample"].startswith("worker 4a1b2c3d4e5f processed job 12")


def test_embedded_timestamps_and_quoted_paths_are_normalised(capsys):
    lines = [
        log_line('read "/var/lib/data/one.db" at 2024-05-01T10:00:00Z taking 3ms'),
        log_line('read "/var/lib/data/two.db" at 2024-05-02T11:11:11Z taking 7ms'),
    ]
    container = FakeContainer(id="d3" * 32, name="db", log_lines=lines)
    with fake_docker(container) as api:
        report = report_for(api, capsys)

    clusters = container_by_name(report, "db")["clusters"]
    assert len(clusters) == 1
    assert clusters[0]["count"] == 2
    assert "/var/lib/data/one.db" not in clusters[0]["template"]


def test_severity_and_error_warning_counts(capsys):
    lines = [
        log_line("INFO startup complete"),
        log_line("WARN disk almost full"),
        log_line("ERROR could not reach upstream"),
        log_line("FATAL giving up"),
    ]
    container = FakeContainer(id="d4" * 32, name="api", log_lines=lines)
    with fake_docker(container) as api:
        report = report_for(api, capsys)

    entry = container_by_name(report, "api")
    assert entry["error_lines"] == 2
    assert entry["warning_lines"] == 1
    assert entry["total_lines"] == 4
    severities = {cluster["sample"]: cluster["severity"] for cluster in entry["clusters"]}
    assert severities["FATAL giving up"] == "error"
    assert severities["WARN disk almost full"] == "warning"
    assert severities["INFO startup complete"] == "info"


def test_error_count_ordering_then_name(capsys):
    quiet_a = FakeContainer(id="d5" * 32, name="alpha", log_lines=[log_line("all good")])
    busy = FakeContainer(id="d6" * 32, name="zulu", log_lines=[log_line("ERROR boom")])
    quiet_b = FakeContainer(id="d7" * 32, name="beta", log_lines=[log_line("all good")])
    with fake_docker(quiet_a, busy, quiet_b) as api:
        report = report_for(api, capsys)

    assert [entry["name"] for entry in report["containers"]] == ["zulu", "alpha", "beta"]


def test_sample_is_truncated_to_fixed_length(capsys):
    long_line = "x" * 500
    container = FakeContainer(id="d8" * 32, name="api", log_lines=[log_line(long_line)])
    with fake_docker(container) as api:
        report = report_for(api, capsys)

    cluster = container_by_name(report, "api")["clusters"][0]
    assert len(cluster["sample"]) == 200


SECRET_CASES = [
    ("password=hunter2", "hunter2", "password"),
    ("passwd: swordfish", "swordfish", "passwd"),
    ("token=abc123token", "abc123token", "token"),
    ("secret: s3cr3tvalue", "s3cr3tvalue", "secret"),
    ("api-key=keyvalue123", "keyvalue123", "api-key"),
    ("api_key: keyvalue456", "keyvalue456", "api_key"),
    (
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payloadsignature",
        "eyJhbGciOiJIUzI1NiJ9.payloadsignature",
        "Authorization",
    ),
    ("X-Api-Key: 1234567890abcdef", "1234567890abcdef", "Api-Key"),
    (
        "digest 0123456789abcdef0123456789abcdef0123456789abcdef",
        "0123456789abcdef0123456789abcdef0123456789abcdef",
        "digest",
    ),
    (
        "GET https://cdn.example.test/v1/blob?data=QWxhZGRpbjpvcGVuIHNlc2FtZV9hbG9uZ2Jhc2U2NA%3D%3D",
        "QWxhZGRpbjpvcGVuIHNlc2FtZV9hbG9uZ2Jhc2U2NA",
        "data",
    ),
]


@pytest.mark.parametrize("line_text,secret,label", SECRET_CASES)
def test_redaction_hides_every_secret_shape(capsys, line_text, secret, label):
    other = log_line("unrelated line")
    container = FakeContainer(
        id="d9" * 32, name="api", log_lines=[log_line(line_text), other]
    )
    with fake_docker(container) as api:
        code, out, err = run_cli(["--docker-url", api.base_url], capsys)

    assert code == 0, err
    assert secret not in out
    assert "[redacted]" in out
    assert label in out
    assert json.loads(out)["containers"][0]["name"] == "api"


def test_redaction_keeps_harmless_pairs(capsys):
    container = FakeContainer(
        id="da" * 32,
        name="api",
        log_lines=[log_line("level=info msg=started attempts=3")],
    )
    with fake_docker(container) as api:
        code, out, _err = run_cli(["--docker-url", api.base_url], capsys)

    assert code == 0
    assert "level=info" in out


def test_redaction_unit_keeps_key_and_masks_value():
    from tasks.host_report.redaction import redact

    assert redact("password=hunter2") == "password=[redacted]"
    assert redact('"token": "abc"') == '"token": "[redacted]"'
    assert (
        redact("Authorization: Bearer abcdef123456") == "Authorization: Bearer [redacted]"
    )


def test_text_format_is_a_short_human_summary(capsys):
    container = FakeContainer(
        id="db" * 32,
        name="api",
        image="example/api:1",
        restart_count=1,
        log_lines=[log_line("ERROR upstream unreachable"), log_line("all quiet")],
    )
    with fake_docker(container) as api:
        code, out, err = run_cli(
            ["--docker-url", api.base_url, "--format", "text"], capsys
        )

    assert code == 0, err
    assert "test-host" in out
    assert "api" in out
    assert "1" in out
    with pytest.raises(json.JSONDecodeError):
        json.loads(out)


def test_invalid_format_exits_two(capsys):
    code, out, _err = run_cli(["--format", "yaml"], capsys)
    assert code == 2
    assert out == ""
