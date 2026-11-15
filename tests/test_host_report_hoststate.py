"""Tests for the deterministic host-state parser, findings and renderer."""

from __future__ import annotations

import pytest

from tasks.host_report.hoststate import (
    Filesystem,
    HostState,
    host_findings,
    host_is_quiet,
    parse_host_state,
    render_host_state,
    summarise_host,
)

GIB = 1024**3


def _document(**overrides):
    document = {
        "cpu_count": 2,
        "load": [0.5, 0.4, 0.3],
        "uptime_seconds": 90061,
        "mem_total_kb": 4 * 1024 * 1024,
        "mem_available_kb": 1024 * 1024,
        "swap_total_kb": 2 * 1024 * 1024,
        "swap_free_kb": 2 * 1024 * 1024,
        "filesystems": [
            {
                "mount": "/",
                "total_bytes": 40 * GIB,
                "used_bytes": 10 * GIB,
                "avail_bytes": 30 * GIB,
                "used_percent": 25,
            }
        ],
    }
    document.update(overrides)
    return document


def _state(**overrides) -> HostState:
    return HostState(**overrides)


def test_parse_reads_every_field():
    state = parse_host_state(_document())

    assert state.cpu_count == 2
    assert state.load == (0.5, 0.4, 0.3)
    assert state.uptime_seconds == 90061
    assert state.mem_total_kb == 4 * 1024 * 1024
    assert state.mem_available_kb == 1024 * 1024
    assert state.swap_total_kb == 2 * 1024 * 1024
    assert state.swap_free_kb == 2 * 1024 * 1024
    assert state.filesystems == (
        Filesystem(
            mount="/",
            total_bytes=40 * GIB,
            used_bytes=10 * GIB,
            avail_bytes=30 * GIB,
            used_percent=25,
        ),
    )


def test_parse_returns_empty_state_for_none_and_for_non_dict():
    assert parse_host_state(None) == HostState()
    assert parse_host_state([]) == HostState()  # type: ignore[arg-type]


def test_parse_tolerates_missing_and_wrong_types():
    state = parse_host_state(
        _document(
            cpu_count="2",
            load=[0.5, 0.4],
            uptime_seconds=True,
            mem_total_kb=None,
            filesystems="not-a-list",
        )
    )

    assert state.cpu_count is None
    assert state.load is None
    assert state.uptime_seconds is None
    assert state.mem_total_kb is None
    assert state.filesystems == ()


def test_parse_skips_incomplete_filesystem_entries():
    state = parse_host_state(
        _document(
            filesystems=[
                {"total_bytes": 1, "used_bytes": 1, "avail_bytes": 0, "used_percent": 50},
                {"mount": "", "total_bytes": 1, "used_percent": 50},
                {"mount": "/data", "used_percent": 50},
                {"mount": "/var", "total_bytes": 10, "used_percent": 70},
            ]
        )
    )

    assert [item.mount for item in state.filesystems] == ["/var"]
    assert state.filesystems[0].used_bytes == 0
    assert state.filesystems[0].avail_bytes == 0


@pytest.mark.parametrize(
    ("percent", "expected"),
    [(84, None), (85, "warning"), (94, "warning"), (95, "critical"), (96, "critical")],
)
def test_disk_threshold_boundaries(percent, expected):
    state = parse_host_state(
        _document(
            filesystems=[
                {
                    "mount": "/",
                    "total_bytes": 40 * GIB,
                    "used_bytes": 1,
                    "avail_bytes": 1,
                    "used_percent": percent,
                }
            ]
        )
    )

    findings = host_findings(state)
    disk = [item for item in findings if item.code == "disk:/"]

    if expected is None:
        assert disk == []
    else:
        assert [item.severity for item in disk] == [expected]
        assert disk[0].message == f"disk / at {percent}% used"


@pytest.mark.parametrize(("available", "expected"), [(10, None), (9, "warning")])
def test_memory_threshold_boundary(available, expected):
    state = _state(mem_total_kb=1000, mem_available_kb=available * 10)

    findings = [item for item in host_findings(state) if item.code == "memory:available"]

    if expected is None:
        assert findings == []
    else:
        assert findings[0].severity == expected
        assert findings[0].message == f"memory available {available}%"


def test_memory_finding_is_skipped_without_total():
    assert host_findings(_state(mem_total_kb=0, mem_available_kb=0)) == ()


@pytest.mark.parametrize(("load1", "expected"), [(2.0, None), (2.01, "warning")])
def test_load_boundary_uses_cpu_count(load1, expected):
    state = _state(cpu_count=2, load=(load1, 0.1, 0.1))

    findings = [item for item in host_findings(state) if item.code == "load:1m"]

    if expected is None:
        assert findings == []
    else:
        assert findings[0].severity == expected
        assert findings[0].message == "load 2.01 above 2 cpu"


def test_load_is_skipped_without_cpu_count():
    assert host_findings(_state(cpu_count=None, load=(9.0, 9.0, 9.0))) == ()


def test_findings_are_ordered_critical_first_then_by_code():
    state = _state(
        cpu_count=1,
        load=(5.0, 1.0, 1.0),
        mem_total_kb=1000,
        mem_available_kb=10,
        filesystems=(
            Filesystem("/srv", 100, 50, 50, 85),
            Filesystem("/", 100, 99, 1, 99),
            Filesystem("/data", 100, 50, 50, 86),
        ),
    )

    codes = [item.code for item in host_findings(state)]

    assert codes == ["disk:/", "disk:/data", "disk:/srv", "load:1m", "memory:available"]


def test_quiet_host_reports_nothing():
    state = parse_host_state(_document())

    assert host_findings(state) == ()
    assert host_is_quiet(state) is True


def test_noisy_host_is_not_quiet():
    state = _state(filesystems=(Filesystem("/", 100, 99, 1, 99),))

    assert host_is_quiet(state) is False


def test_render_includes_one_line_per_known_fact():
    text = render_host_state(parse_host_state(_document()), limit_chars=2000)

    assert text.splitlines() == [
        "cpu 2 · load 0.50 0.40 0.30",
        "uptime 1d 1h",
        "mem 3.0/4.0 GiB used (25% available)",
        "swap 0.0/2.0 GiB used",
        "disk / 25% of 40.0 GiB (30.0 GiB free)",
    ]


def test_render_is_deterministic():
    state = parse_host_state(_document())

    assert render_host_state(state, limit_chars=2000) == render_host_state(
        state, limit_chars=2000
    )


def test_render_respects_the_character_cap():
    text = render_host_state(parse_host_state(_document()), limit_chars=20)

    assert len(text) <= 20
    assert text == "…"


def test_render_of_an_empty_state_and_of_a_zero_budget():
    assert render_host_state(HostState(), limit_chars=100) == ""
    assert render_host_state(parse_host_state(_document()), limit_chars=0) == ""


def test_render_without_load_or_memory_still_names_the_cpu_and_uptime():
    text = render_host_state(
        HostState(cpu_count=4, uptime_seconds=125), limit_chars=200
    )

    assert text.splitlines() == ["cpu 4", "uptime 2m"]


def test_render_shows_swap_only_when_it_exists():
    assert "swap" not in render_host_state(
        HostState(swap_total_kb=0, swap_free_kb=0), limit_chars=200
    )


def test_render_shows_small_free_space_in_mib():
    text = render_host_state(
        HostState(filesystems=(Filesystem("/", 40 * GIB, 39 * GIB, 100 * 1024**2, 97),)),
        limit_chars=200,
    )

    assert text == "disk / 97% of 40.0 GiB (100 MiB free)"


def test_summarise_host_is_machine_readable_and_ignores_missing_values():
    summary = summarise_host(parse_host_state(_document()))

    assert summary["cpu_count"] == 2
    assert summary["load"] == [0.5, 0.4, 0.3]
    assert summary["filesystems"][0]["used_percent"] == 25
    assert summarise_host(HostState())["load"] is None
    assert "memory_used_percent" not in summarise_host(HostState())
