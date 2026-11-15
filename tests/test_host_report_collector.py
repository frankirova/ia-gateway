"""Collection behaviour: Docker API mapping, log framing, caps, skips, exit codes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from host_report_fake import (
    STDERR,
    STDOUT,
    FakeContainer,
    closed_port_url,
    fake_docker,
    log_line,
    mux_frames,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

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
    """Run the collector in-process and return ``(exit_code, stdout, stderr)``."""
    from tasks.host_report.cli import main

    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def run_json(api, capsys: pytest.CaptureFixture[str], *extra: str) -> dict:
    code, out, err = run_cli(
        ["--docker-url", api.base_url, "--format", "json", *extra], capsys
    )
    assert code == 0, err
    return json.loads(out)


def container_by_name(report: dict, name: str) -> dict:
    for entry in report["containers"]:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"container {name!r} missing from report: {report}")


def test_listing_and_inspect_map_to_container_summaries(capsys):
    web = FakeContainer(
        id="c1" * 32,
        name="web",
        image="example/web:2.1",
        state="running",
        restart_count=3,
        log_lines=[log_line("listening on port 8080")],
    )
    batch = FakeContainer(
        id="c2" * 32,
        name="batch",
        image="example/batch:1.0",
        state="exited",
        restart_count=0,
        exit_code=137,
        log_lines=[log_line("backup finished")],
    )
    with fake_docker(web, batch) as api:
        report = run_json(api, capsys)

    web_entry = container_by_name(report, "web")
    assert web_entry["image"] == "example/web:2.1"
    assert web_entry["state"] == "running"
    assert web_entry["restart_count"] == 3
    assert web_entry["exit_code"] is None
    batch_entry = container_by_name(report, "batch")
    assert batch_entry["state"] == "exited"
    assert batch_entry["exit_code"] == 137
    assert report["host"] == "test-host"
    assert report["schema_version"] == 1
    assert any("/containers/c1" in path for path in api.paths)


def test_multiplexed_frames_are_parsed(capsys):
    body = mux_frames(
        [
            (STDOUT, b"first line\nsecond "),
            (STDERR, b"line from stderr\n"),
            (STDOUT, b"third line\n"),
        ]
    )
    container = FakeContainer(
        id="c3" * 32, name="mux", logs_body=body
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "mux")
    assert entry["lines_read"] == 3
    samples = [cluster["sample"] for cluster in entry["clusters"]]
    assert "first line" in samples
    assert "second line from stderr" in samples
    assert "third line" in samples


def test_plain_non_multiplexed_body_is_tolerated(capsys):
    container = FakeContainer(
        id="c4" * 32,
        name="plain",
        logs_mode="plain",
        log_lines=[log_line("alpha"), log_line("beta")],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "plain")
    assert entry["lines_read"] == 2
    assert {cluster["sample"] for cluster in entry["clusters"]} == {"alpha", "beta"}


def test_logs_request_carries_since_epoch_tail_and_timestamps(capsys):
    container = FakeContainer(
        id="c5" * 32, name="api", log_lines=[log_line("hello")]
    )
    with fake_docker(container) as api:
        run_json(api, capsys, "--since", "1d", "--max-lines", "11")

    logs_paths = [path for path in api.paths if path.endswith("/logs") or "/logs?" in path]
    assert len(logs_paths) == 1
    query = logs_paths[0]
    assert "stdout=1" in query
    assert "stderr=1" in query
    assert "timestamps=1" in query
    # Over-fetch: the collector asks for max_lines * HOST_REPORT_OVERFETCH lines
    # (default 4) so error and warning lines later in the stream survive the cap.
    assert f"tail={11 * 4}" in query
    match = re.search(r"since=(\d+)", query)
    assert match is not None
    since_epoch = int(match.group(1))
    now = __import__("time").time()
    assert now - 86400 - 30 <= since_epoch <= now - 86400 + 30


def test_since_duration_drives_window():
    from tasks.host_report.window import parse_duration, window_for

    assert parse_duration("24h") == 86400
    assert parse_duration("7d") == 604800
    assert parse_duration("90m") == 5400
    assert parse_duration("45s") == 45

    window = window_for(3600, now_epoch=1_700_000_000.0)
    assert window.since_epoch == 1_699_996_400
    assert window.until_epoch == 1_700_000_000
    assert window.since_iso == "2023-11-14T21:13:20Z"
    assert window.until_iso == "2023-11-14T22:13:20Z"


@pytest.mark.parametrize("value", ["", "24", "h", "-1h", "1.5h", "1d12h", "0h", "24H", " 24h"])
def test_invalid_since_is_rejected(value):
    from tasks.host_report.window import InvalidDuration, parse_duration

    with pytest.raises(InvalidDuration):
        parse_duration(value)


def test_invalid_since_exits_two_capsys(capsys):
    code, out, err = run_cli(["--since", "yesterday", "--docker-url", closed_port_url()], capsys)
    assert code == 2
    assert out == ""
    assert "since" in err.lower()


def test_invalid_since_exits_two_through_module_entry_point():
    result = subprocess.run(
        [sys.executable, "-m", "tasks.host_report", "--since", "yesterday"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 2
    assert result.stdout == ""
    assert "since" in result.stderr.lower()


def test_unreachable_endpoint_exits_one(capsys):
    code, out, err = run_cli(["--docker-url", closed_port_url()], capsys)
    assert code == 1
    assert out == ""
    assert "docker" in err.lower()


def test_container_404_lands_in_skipped(capsys):
    healthy = FakeContainer(id="c6" * 32, name="healthy", log_lines=[log_line("ok")])
    broken = FakeContainer(id="c7" * 32, name="broken", inspect_status=404)
    with fake_docker(healthy, broken) as api:
        report = run_json(api, capsys)

    assert [entry["name"] for entry in report["containers"]] == ["healthy"]
    assert [item["container"] for item in report["skipped"]] == ["broken"]
    assert "404" in report["skipped"][0]["reason"]


def test_container_log_timeout_lands_in_skipped(capsys):
    healthy = FakeContainer(id="c8" * 32, name="healthy", log_lines=[log_line("ok")])
    slow = FakeContainer(
        id="c9" * 32, name="slow", log_lines=[log_line("late")], logs_delay=2.0
    )
    with fake_docker(healthy, slow) as api:
        report = run_json(api, capsys, "--timeout", "0.3")

    assert [entry["name"] for entry in report["containers"]] == ["healthy"]
    skipped = {item["container"]: item["reason"] for item in report["skipped"]}
    assert "slow" in skipped
    assert skipped["slow"]


def test_max_lines_cap_is_recorded(capsys):
    lines = [log_line(f"line number {index}") for index in range(10)]
    container = FakeContainer(id="ca" * 32, name="chatty", log_lines=lines)
    with fake_docker(container) as api:
        report = run_json(api, capsys, "--max-lines", "4")

    entry = container_by_name(report, "chatty")
    # The stream is read up to the over-fetch tail; the cap decides which of the
    # lines that were read are analysed, so lines_read is larger than the cap.
    assert entry["lines_read"] == 10
    assert entry["total_lines"] == 4
    truncations = [t for t in report["truncations"] if t["kind"] == "lines"]
    assert truncations
    assert truncations[0]["container"] == "chatty"
    assert truncations[0]["limit"] == 4
    sampling = [t for t in report["truncations"] if t["kind"] == "sampling"]
    assert sampling
    assert sampling[0]["kept"] == 4
    assert sampling[0]["info_dropped"] == 6


def test_max_clusters_cap_keeps_highest_count(capsys):
    lines: list[str] = []
    for index in range(30):
        # Distinct alphabetic markers keep every noise line its own cluster.
        marker = f"{chr(97 + index // 26)}{chr(97 + index % 26)}"
        lines.append(log_line(f"noise event {marker}"))
    for _ in range(5):
        lines.append(log_line("beta crash reported"))
    container = FakeContainer(id="cb" * 32, name="noisy", log_lines=lines)
    with fake_docker(container) as api:
        report = run_json(api, capsys, "--max-clusters", "3", "--max-lines", "100")

    entry = container_by_name(report, "noisy")
    assert len(entry["clusters"]) == 3
    templates = [cluster["template"] for cluster in entry["clusters"]]
    assert "beta crash reported" in templates
    truncations = [t for t in report["truncations"] if t["kind"] == "clusters"]
    assert truncations
    assert truncations[0]["limit"] == 3
    assert truncations[0]["observed"] > 3


def test_max_containers_cap_is_recorded(capsys):
    containers = [
        FakeContainer(id=f"{index:02d}" * 16, name=f"svc-{index}", log_lines=[log_line("ok")])
        for index in range(5)
    ]
    with fake_docker(*containers) as api:
        report = run_json(api, capsys, "--max-containers", "2")

    assert [entry["name"] for entry in report["containers"]] == ["svc-0", "svc-1"]
    truncations = [t for t in report["truncations"] if t["kind"] == "containers"]
    assert truncations
    assert truncations[0]["limit"] == 2
    assert truncations[0]["observed"] == 5
    skipped = {item["container"] for item in report["skipped"]}
    assert {"svc-2", "svc-3", "svc-4"} <= skipped


def test_default_ignore_patterns_skip_routine_noise(capsys):
    container = FakeContainer(
        id="cc" * 32,
        name="api",
        log_lines=[
            log_line("GET /health HTTP/1.1 200"),
            log_line(""),
            log_line("real work happened"),
            log_line("docker healthcheck: healthy"),
        ],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "api")
    assert entry["lines_read"] == 4
    assert entry["ignored_lines"] == 3
    assert entry["total_lines"] == 1
    assert [cluster["sample"] for cluster in entry["clusters"]] == ["real work happened"]


def test_custom_ignore_pattern_from_cli(capsys):
    container = FakeContainer(
        id="cd" * 32,
        name="api",
        log_lines=[log_line("noise-42 here"), log_line("keep this")],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys, "--ignore", r"noise-\d+")

    entry = container_by_name(report, "api")
    assert entry["ignored_lines"] == 1
    assert [cluster["sample"] for cluster in entry["clusters"]] == ["keep this"]


def test_custom_ignore_pattern_from_environment(capsys, monkeypatch):
    monkeypatch.setenv("HOST_REPORT_IGNORE", r"noise-\d+")
    container = FakeContainer(
        id="ce" * 32,
        name="api",
        log_lines=[log_line("noise-42 here"), log_line("keep this")],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "api")
    assert entry["ignored_lines"] == 1


def test_invalid_ignore_pattern_exits_two(capsys):
    code, out, err = run_cli(["--ignore", "(", "--docker-url", closed_port_url()], capsys)
    assert code == 2
    assert out == ""
    assert "ignore" in err.lower()


def test_invalid_max_lines_exits_two(capsys):
    code, out, err = run_cli(["--max-lines", "0", "--docker-url", closed_port_url()], capsys)
    assert code == 2
    assert out == ""


def test_container_log_content_never_reaches_stderr(capsys):
    secret_line = "unique-log-marker-do-not-echo"
    container = FakeContainer(id="cf" * 32, name="api", log_lines=[log_line(secret_line)])
    with fake_docker(container) as api:
        code, out, err = run_cli(["--docker-url", api.base_url], capsys)

    assert code == 0
    assert err == ""
    assert secret_line in out
