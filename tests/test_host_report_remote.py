"""Tests for the SSH bundle transport and the read-only remote program."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tasks.host_report.remote_bundle import (
    Bundle,
    BundleContainer,
    BundleDockerClient,
    BundleUnavailable,
    fetch_bundle,
    render_remote_error,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_PROGRAM = REPO_ROOT / "deploy" / "vps-report" / "vps-readonly"

GOOD_BUNDLE = {
    "version": 1,
    "collected_at": "2026-11-13T00:00:00Z",
    "truncated": False,
    "host": {"cpu_count": 2, "load": [0.5, 0.4, 0.3]},
    "containers": [
        {
            "listing": {"Id": "abc", "Names": ["/web"], "Image": "nginx:1"},
            "inspect": {"Name": "/web", "RestartCount": 1},
            "logs": "2026-11-13T00:00:00.000000000Z hello\n",
        }
    ],
}


def _write_ssh(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "ssh"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return script


def _payload(tmp_path: Path, document) -> Path:
    path = tmp_path / "payload.json"
    path.write_text(
        document if isinstance(document, str) else json.dumps(document)
    )
    return path


def _fetch_bundle(script: Path, **overrides):
    options = {
        "ssh_target": "vps-report@vps.example",
        "timeout_seconds": 5.0,
        "ssh_binary": str(script),
    }
    options.update(overrides)
    return fetch_bundle(**options)


@pytest.fixture()
def ssh_env(tmp_path, monkeypatch):
    """Install a fake ssh and expose the knobs the fake reads."""

    def install(body: str, document) -> Path:
        script = _write_ssh(tmp_path, body)
        payload = _payload(tmp_path, document)
        monkeypatch.setenv("FAKE_SSH_PAYLOAD", str(payload))
        monkeypatch.setenv("FAKE_SSH_ARGV", str(tmp_path / "argv.txt"))
        return script

    return install


def _reason(callable_, **options) -> str:
    with pytest.raises(BundleUnavailable) as caught:
        callable_(**options)
    return caught.value.reason


def test_fetch_parses_a_good_bundle(ssh_env):
    script = ssh_env('cat "$FAKE_SSH_PAYLOAD"\n', GOOD_BUNDLE)

    bundle = _fetch_bundle(script)

    assert isinstance(bundle, Bundle)
    assert bundle.collected_at == "2026-11-13T00:00:00Z"
    assert bundle.truncated is False
    assert bundle.host == {"cpu_count": 2, "load": [0.5, 0.4, 0.3]}
    assert len(bundle.containers) == 1
    container = bundle.containers[0]
    assert container.listing["Id"] == "abc"
    assert container.inspect["RestartCount"] == 1
    assert container.logs == b"2026-11-13T00:00:00.000000000Z hello\n"
    assert container.error is None


def test_fetch_accepts_a_bundle_with_no_containers(ssh_env):
    document = dict(GOOD_BUNDLE, containers=[])
    script = ssh_env('cat "$FAKE_SSH_PAYLOAD"\n', document)

    bundle = _fetch_bundle(script)

    assert bundle.containers == ()


def test_fetch_keeps_a_container_level_error(ssh_env):
    document = json.loads(json.dumps(GOOD_BUNDLE))
    document["containers"][0]["error"] = "logs_failed"
    script = ssh_env('cat "$FAKE_SSH_PAYLOAD"\n', document)

    bundle = _fetch_bundle(script)

    assert bundle.containers[0].error == "logs_failed"


def test_fetch_maps_a_docker_error_document(ssh_env):
    script = ssh_env(
        'cat "$FAKE_SSH_PAYLOAD"\nexit 1\n',
        {"version": 1, "error": "docker_unavailable"},
    )

    assert _reason(_fetch_bundle, script=script) == "docker_unavailable"


def test_fetch_maps_a_remote_failure_without_a_document(ssh_env):
    script = ssh_env("exit 255\n", "")

    assert _reason(_fetch_bundle, script=script) == "ssh_failed"


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        ("not json", "invalid_bundle"),
        ("", "invalid_bundle"),
        ({"version": 99}, "invalid_bundle"),
        ({"version": 1, "containers": "nope"}, "invalid_bundle"),
        ({"version": 1, "containers": [{"listing": "x", "inspect": {}, "logs": ""}]}, "invalid_bundle"),
        ({"version": 1, "containers": [], "error": "weird"}, "invalid_bundle"),
    ],
)
def test_fetch_rejects_a_malformed_bundle(ssh_env, document, expected):
    script = ssh_env('cat "$FAKE_SSH_PAYLOAD"\n', document)

    assert _reason(_fetch_bundle, script=script) == expected


def test_fetch_times_out_instead_of_waiting_forever(ssh_env):
    script = ssh_env('sleep 5\ncat "$FAKE_SSH_PAYLOAD"\n', GOOD_BUNDLE)

    assert (
        _reason(_fetch_bundle, script=script, timeout_seconds=0.2) == "ssh_timeout"
    )


def test_fetch_refuses_an_oversized_bundle(ssh_env):
    script = ssh_env('cat "$FAKE_SSH_PAYLOAD"\n', GOOD_BUNDLE)

    assert (
        _reason(_fetch_bundle, script=script, max_bytes=50) == "too_large"
    )


def test_fetch_reports_a_missing_ssh_binary(tmp_path):
    missing = tmp_path / "no-such-ssh"

    with pytest.raises(BundleUnavailable) as caught:
        fetch_bundle(ssh_target="vps", timeout_seconds=1.0, ssh_binary=str(missing))

    assert caught.value.reason == "ssh_failed"


def test_fetch_passes_a_fixed_argument_list(ssh_env, tmp_path):
    script = ssh_env('printf "%s\\n" "$@" > "$FAKE_SSH_ARGV"\ncat "$FAKE_SSH_PAYLOAD"\n', GOOD_BUNDLE)

    _fetch_bundle(script, timeout_seconds=10.0)

    argv = (tmp_path / "argv.txt").read_text().splitlines()
    assert argv == [
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
        "vps-report@vps.example",
        "vps-readonly",
    ]


def test_render_remote_error_uses_the_closed_vocabulary():
    assert render_remote_error(BundleUnavailable("ssh_timeout")) == (
        "VPS unreachable: ssh timeout"
    )
    assert render_remote_error(BundleUnavailable("docker_unavailable")) == (
        "VPS unreachable: docker unavailable"
    )
    assert render_remote_error(BundleUnavailable("too_large")) == (
        "VPS unreachable: bundle too large"
    )
    assert render_remote_error(ValueError("boom")) == "VPS unreachable: invalid bundle"


def _client() -> BundleDockerClient:
    container = BundleContainer(
        listing={"Id": "abc", "Names": ["/web"], "Image": "nginx:1"},
        inspect={"Name": "/web"},
        logs=(
            b"2026-11-13T00:00:00.000000000Z old\n"
            b"2026-11-13T06:00:00.000000000Z new\n"
            b"a line without a timestamp\n"
        ),
    )
    bundle = Bundle(
        containers=(container,),
        host={"cpu_count": 2},
        collected_at="2026-11-13T06:00:00Z",
    )
    return BundleDockerClient(bundle)


def test_client_serves_the_listing_and_the_host_state():
    client = _client()

    assert client.list_containers() == [
        {"Id": "abc", "Names": ["/web"], "Image": "nginx:1"}
    ]
    assert client.list_host_state() == {"cpu_count": 2}


def test_client_inspects_a_known_container_and_refuses_an_unknown_one():
    client = _client()

    assert client.inspect_container("abc") == {"Name": "/web"}
    assert _reason(client.inspect_container, container_id="missing") == "invalid_bundle"


def test_client_filters_lines_outside_the_window():
    from datetime import datetime, timezone

    client = _client()
    since = int(datetime(2026, 11, 13, 1, 0, 0, tzinfo=timezone.utc).timestamp())

    lines, truncated = client.read_log_lines(
        "abc", since=since, tail=100, max_lines=100
    )

    assert truncated is False
    assert lines[-2:] == [
        "2026-11-13T06:00:00.000000000Z new",
        "a line without a timestamp",
    ]
    assert all("old" not in line for line in lines)


def test_client_caps_lines_and_reports_the_truncation():
    client = _client()

    lines, truncated = client.read_log_lines(
        "abc", since=0, tail=100, max_lines=2
    )

    assert truncated is True
    assert len(lines) == 2


def test_client_open_logs_returns_a_readable_stream():
    client = _client()

    stream = client.open_logs("abc")

    assert stream.read().startswith(b"2026-11-13T00:00:00")


def _run_remote_program(tmp_path: Path, docker_body: str | None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if docker_body is not None:
        docker = bin_dir / "docker"
        docker.write_text("#!/bin/sh\n" + docker_body)
        docker.chmod(0o755)
    env = {"PATH": str(bin_dir), "HOME": str(tmp_path)}
    return subprocess.run(
        [sys.executable, str(REMOTE_PROGRAM), "--whatever", "ignored"],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


FAKE_DOCKER = """\
case "$1" in
  ps)
    echo '{"ID":"abc","Names":"web","Image":"nginx:1","State":"running","Status":"Up"}'
    echo '{"ID":"def","Names":"api","Image":"api:1","State":"running","Status":"Up"}'
    ;;
  inspect)
    if [ "$4" = "def" ]; then echo "no such container" ; exit 1 ; fi
    if [ "$4" = "abc" ]; then echo '{"Name":"/web","RestartCount":0,"State":{"Status":"running"},"Config":{"Image":"nginx:1"}}' ; exit 0 ; fi
    exit 1
    ;;
  logs)
    if [ "$5" = "def" ]; then exit 1 ; fi
    printf '2026-11-13T00:00:00.000000000Z line one\\n2026-11-13T00:00:01.000000000Z line two\\n'
    ;;
esac
"""


def test_remote_program_emits_one_valid_document(tmp_path):
    completed = _run_remote_program(tmp_path, FAKE_DOCKER)

    assert completed.returncode == 0
    document = json.loads(completed.stdout)
    assert document["version"] == 1
    assert document["collected_at"].endswith("Z")
    assert document["host"]["cpu_count"] >= 1
    assert isinstance(document["host"]["filesystems"], list)
    assert [item["listing"]["Id"] for item in document["containers"]] == ["abc", "def"]
    listing = document["containers"][0]["listing"]
    assert listing["Names"] == ["web"]
    assert listing["Image"] == "nginx:1"
    assert document["containers"][0]["inspect"]["RestartCount"] == 0
    assert "line one" in document["containers"][0]["logs"]


def test_remote_program_keeps_a_broken_container_in_the_bundle(tmp_path):
    completed = _run_remote_program(tmp_path, FAKE_DOCKER)

    document = json.loads(completed.stdout)

    assert document["containers"][1]["error"] == "inspect_failed"


def test_remote_program_refuses_to_run_without_docker(tmp_path):
    completed = _run_remote_program(tmp_path, None)

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "version": 1,
        "error": "docker_unavailable",
    }


def test_remote_program_is_executable_with_an_env_shebang():
    first_line = REMOTE_PROGRAM.read_text().splitlines()[0]
    mode = REMOTE_PROGRAM.stat().st_mode

    assert first_line == "#!/usr/bin/env python3"
    assert mode & 0o111
