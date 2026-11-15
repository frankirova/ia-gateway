"""Noise handling (T6): ANSI stripping, severity-aware sampling, per-container caps.

Everything runs in-process against the loopback Docker double. No network, no
Docker daemon and no secrets.
"""

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

ESC = "\x1b"


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


def records(report: dict, kind: str, name: str | None = None) -> list[dict]:
    return [
        item
        for item in report["truncations"]
        if item["kind"] == kind and (name is None or item["container"] == name)
    ]


def samples(entry: dict) -> set[str]:
    return {cluster["sample"] for cluster in entry["clusters"]}


def logs_query(api) -> str:
    queries = [path for path in api.paths if "/logs?" in path]
    assert len(queries) == 1, queries
    return queries[0]


# -- ANSI stripping ---------------------------------------------------------


def test_strip_ansi_removes_colour_codes():
    from tasks.host_report.ansi import strip_ansi

    assert strip_ansi(f"{ESC}[31merror{ESC}[0m: boom") == "error: boom"
    assert strip_ansi(f"{ESC}[1;33m  warn {ESC}[m") == "  warn "


def test_strip_ansi_removes_osc_sequences():
    from tasks.host_report.ansi import strip_ansi

    assert strip_ansi(f"{ESC}]0;window title\x07body") == "body"
    assert strip_ansi(f"{ESC}]8;;http://example.invalid{ESC}\\link text") == "link text"


def test_strip_ansi_removes_two_character_escapes():
    from tasks.host_report.ansi import strip_ansi

    assert strip_ansi(f"{ESC}Mreverse{ESC}7save{ESC}=safe") == "reversesavesafe"
    assert strip_ansi(f"{ESC}Mreverse index") == "reverse index"


def test_strip_ansi_keeps_textual_lookalikes():
    from tasks.host_report.ansi import strip_ansi

    literal = r"value \u001b[31mis text\u001b[0m here"
    caret = "value ^[[31mis text^[[0m here"
    assert strip_ansi(literal) == literal
    assert strip_ansi(caret) == caret


def test_strip_ansi_is_idempotent():
    from tasks.host_report.ansi import strip_ansi

    once = strip_ansi(f"{ESC}[31mred{ESC}[0m {ESC}]0;title\x07{ESC}Htext")
    assert strip_ansi(once) == once
    assert once == "red text"


def test_strip_ansi_never_raises_on_a_truncated_sequence():
    from tasks.host_report.ansi import strip_ansi

    assert strip_ansi(f"partial {ESC}[3") == "partial "
    assert strip_ansi(f"dangling {ESC}") == "dangling "
    assert strip_ansi(ESC) == ""
    assert ESC not in strip_ansi(f"{ESC}]0;unterminated title")


def test_strip_ansi_keeps_utf8_text():
    from tasks.host_report.ansi import strip_ansi

    assert strip_ansi(f"{ESC}[32mseñal ünïcode ✓{ESC}[0m") == "señal ünïcode ✓"


def test_colour_codes_are_stripped_before_severity_classification(capsys):
    container = FakeContainer(
        id="a1" * 32,
        name="colour",
        log_lines=[
            log_line(f"{ESC}[31merror{ESC}[0m: database connection refused"),
            log_line(f"{ESC}[33mwarning{ESC}[0m: retrying in {ESC}[1m5s{ESC}[0m"),
        ],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "colour")
    assert entry["error_lines"] == 1
    assert entry["warning_lines"] == 1
    severities = {cluster["sample"]: cluster["severity"] for cluster in entry["clusters"]}
    assert severities == {
        "error: database connection refused": "error",
        "warning: retrying in 5s": "warning",
    }
    assert ESC not in json.dumps(report)


def test_osc_and_two_character_escapes_are_removed_from_templates(capsys):
    container = FakeContainer(
        id="a2" * 32,
        name="osc",
        log_lines=[
            log_line(f"{ESC}]0;window title\x07first marker"),
            log_line(f"{ESC}]8;;http://example.invalid{ESC}\\second marker"),
            log_line(f"{ESC}Mthird marker"),
            log_line(f"{ESC}7fourth marker{ESC}="),
        ],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "osc")
    assert samples(entry) == {
        "first marker",
        "second marker",
        "third marker",
        "fourth marker",
    }


def test_textual_escape_lookalikes_are_preserved(capsys):
    literal = r"reported \u001b[31mraw text\u001b[0m verbatim"
    caret = "reported ^[[31mcaret form^[[0m verbatim"
    container = FakeContainer(
        id="a3" * 32, name="lookalike", log_lines=[log_line(literal), log_line(caret)]
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "lookalike")
    assert samples(entry) == {literal, caret}


def test_truncated_escape_at_line_end_does_not_eat_the_next_line(capsys):
    container = FakeContainer(
        id="a4" * 32,
        name="truncated",
        log_lines=[log_line(f"partial {ESC}[3"), log_line("next line survives")],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "truncated")
    assert entry["total_lines"] == 2
    templates = {cluster["template"] for cluster in entry["clusters"]}
    assert templates == {"partial", "next line survives"}


def test_utf8_accents_survive_stripping(capsys):
    container = FakeContainer(
        id="a5" * 32,
        name="utf8",
        log_lines=[log_line(f"{ESC}[32mseñal ünïcode: configuración lista ✓{ESC}[0m")],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "utf8")
    assert samples(entry) == {"señal ünïcode: configuración lista ✓"}


# -- severity-aware sampling ------------------------------------------------


def test_sampling_keeps_every_error_and_fills_with_info(capsys, monkeypatch):
    monkeypatch.setenv("HOST_REPORT_OVERFETCH", "100")
    info_lines = [log_line(f"progress step {index:03d} finished") for index in range(100)]
    error_lines = [
        log_line(f"error: pipeline stage {marker} exploded")
        for marker in ("alpha", "beta", "gamma")
    ]
    container = FakeContainer(
        id="b1" * 32, name="noisy", log_lines=info_lines + error_lines
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys, "--max-lines", "10")

    entry = container_by_name(report, "noisy")
    assert entry["lines_read"] == 103
    assert entry["total_lines"] == 10
    assert entry["error_lines"] == 3
    assert entry["warning_lines"] == 0
    assert sorted(cluster["template"] for cluster in entry["clusters"]) == [
        "error: pipeline stage alpha exploded",
        "error: pipeline stage beta exploded",
        "error: pipeline stage gamma exploded",
        "progress step <num> finished",
    ]

    assert records(report, "lines", "noisy"), report["truncations"]
    assert records(report, "lines", "noisy")[0]["limit"] == 10
    sampling = records(report, "sampling", "noisy")
    assert len(sampling) == 1, report["truncations"]
    assert sampling[0]["limit"] == 10
    assert sampling[0]["lines_read"] == 103
    assert sampling[0]["kept"] == 10
    assert sampling[0]["errors_kept"] == 3
    assert sampling[0]["warnings_kept"] == 0
    assert sampling[0]["info_dropped"] == 93


def test_sampling_never_drops_an_error_while_keeping_info(capsys, monkeypatch):
    monkeypatch.setenv("HOST_REPORT_OVERFETCH", "100")
    lines: list[str] = []
    for index in range(10):
        lines.append(log_line(f"info heartbeat {index} recorded"))
        lines.append(log_line(f"error upstream call {index} failed"))
    container = FakeContainer(id="b2" * 32, name="interleaved", log_lines=lines)
    with fake_docker(container) as api:
        report = run_json(api, capsys, "--max-lines", "5")

    entry = container_by_name(report, "interleaved")
    assert entry["lines_read"] == 20
    assert entry["total_lines"] == 5
    assert entry["error_lines"] == 5
    assert entry["clusters"] and all(
        cluster["severity"] == "error" for cluster in entry["clusters"]
    )
    sampling = records(report, "sampling", "interleaved")
    assert len(sampling) == 1
    assert sampling[0]["errors_kept"] == 5
    assert sampling[0]["info_dropped"] == 10
    assert sampling[0]["kept"] == 5


def test_clean_container_records_no_sampling(capsys):
    lines = [log_line(f"job tick {index} done") for index in range(10)]
    container = FakeContainer(id="b3" * 32, name="calm", log_lines=lines)
    with fake_docker(container) as api:
        report = run_json(api, capsys, "--max-lines", "100")

    entry = container_by_name(report, "calm")
    assert entry["lines_read"] == 10
    assert entry["total_lines"] == 10
    assert report["truncations"] == []


def test_overfetch_tail_is_max_lines_times_overfetch(capsys, monkeypatch):
    monkeypatch.setenv("HOST_REPORT_OVERFETCH", "3")
    container = FakeContainer(id="b4" * 32, name="api", log_lines=[log_line("hello")])
    with fake_docker(container) as api:
        run_json(api, capsys, "--max-lines", "7")

    assert "tail=21" in logs_query(api)


def test_overfetch_tail_is_clamped(capsys, monkeypatch):
    monkeypatch.setenv("HOST_REPORT_OVERFETCH", "9")
    container = FakeContainer(id="b5" * 32, name="api", log_lines=[log_line("hello")])
    with fake_docker(container) as api:
        run_json(api, capsys, "--max-lines", "3000")

    assert "tail=20000" in logs_query(api)


# -- per-container caps -----------------------------------------------------


def test_per_container_limits_apply_to_the_named_container(capsys, monkeypatch):
    monkeypatch.setenv(
        "HOST_REPORT_CONTAINER_LIMITS",
        json.dumps({"noisy": {"max_lines": 4, "max_clusters": 2}}),
    )
    markers = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
    noisy_lines = [log_line(f"error: distinct {marker} failure") for marker in markers]
    noisy_lines += [log_line(f"queue drain step {index} done") for index in range(20)]
    quiet_lines = [log_line(f"queue drain step {index} done") for index in range(20)]
    noisy = FakeContainer(id="c1" * 32, name="noisy", log_lines=noisy_lines)
    quiet = FakeContainer(id="c2" * 32, name="quiet", log_lines=quiet_lines)
    with fake_docker(noisy, quiet) as api:
        report = run_json(api, capsys)

    noisy_entry = container_by_name(report, "noisy")
    assert noisy_entry["total_lines"] == 4
    assert noisy_entry["error_lines"] == 4
    assert len(noisy_entry["clusters"]) == 2
    assert records(report, "lines", "noisy")[0]["limit"] == 4
    assert records(report, "sampling", "noisy")[0]["limit"] == 4
    cluster_caps = records(report, "clusters", "noisy")
    assert cluster_caps and cluster_caps[0]["limit"] == 2
    assert cluster_caps[0]["observed"] == 4

    quiet_entry = container_by_name(report, "quiet")
    assert quiet_entry["total_lines"] == 20
    assert len(quiet_entry["clusters"]) == 1
    assert records(report, "sampling", "quiet") == []
    assert records(report, "lines", "quiet") == []
    assert records(report, "clusters", "quiet") == []


def test_container_limits_may_set_one_cap_only(capsys, monkeypatch):
    monkeypatch.setenv(
        "HOST_REPORT_CONTAINER_LIMITS", json.dumps({"api": {"max_lines": 2}})
    )
    container = FakeContainer(
        id="c3" * 32,
        name="api",
        log_lines=[log_line(f"job tick {index} done") for index in range(5)],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "api")
    assert entry["total_lines"] == 2
    assert records(report, "sampling", "api")[0]["limit"] == 2


def test_container_limits_for_absent_containers_are_unused(capsys, monkeypatch):
    monkeypatch.setenv(
        "HOST_REPORT_CONTAINER_LIMITS",
        json.dumps({"gone": {"max_lines": 1, "max_clusters": 1}}),
    )
    container = FakeContainer(
        id="c4" * 32,
        name="api",
        log_lines=[log_line(f"job tick {index} done") for index in range(5)],
    )
    with fake_docker(container) as api:
        report = run_json(api, capsys)

    entry = container_by_name(report, "api")
    assert entry["total_lines"] == 5
    assert report["truncations"] == []


@pytest.mark.parametrize(
    "value",
    [
        "{not json",
        "[]",
        '"api"',
        "null",
        '{"api": 5}',
        '{"api": {"max_lines": -1, "max_clusters": 5}}',
        '{"api": {"max_lines": 0, "max_clusters": 5}}',
        '{"api": {"max_lines": 5, "max_clusters": 0}}',
        '{"api": {"max_lines": "5", "max_clusters": 5}}',
        '{"api": {"max_lines": 5.5, "max_clusters": 5}}',
        '{"api": {"max_lines": true, "max_clusters": 5}}',
        '{"api": {"max_lines": 5, "max_clusters": 5, "max_container": 2}}',
        '{"": {"max_lines": 5, "max_clusters": 5}}',
    ],
)
def test_invalid_container_limits_exit_two_without_touching_docker(
    value, capsys, monkeypatch
):
    monkeypatch.setenv("HOST_REPORT_CONTAINER_LIMITS", value)
    container = FakeContainer(id="c5" * 32, name="api", log_lines=[log_line("hello")])
    with fake_docker(container) as api:
        code, out, err = run_cli(["--docker-url", api.base_url], capsys)
        assert api.paths == []

    assert code == 2
    assert out == ""
    assert "HOST_REPORT_CONTAINER_LIMITS" in err


def test_invalid_overfetch_exits_two(capsys, monkeypatch):
    monkeypatch.setenv("HOST_REPORT_OVERFETCH", "0")
    code, out, err = run_cli(
        ["--docker-url", "http://127.0.0.1:1"], capsys
    )
    assert code == 2
    assert out == ""
    assert "HOST_REPORT_OVERFETCH" in err
