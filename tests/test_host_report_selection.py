"""Container selection: the VPS decides what it exposes, the report says what is missing.

The remote program reads one root-owned config file. Without it nothing changes;
with it, only the named containers travel and any configured container that is
not running becomes a measured finding instead of an absence of news.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tasks.host_report.collector import selection_findings
from tasks.host_report.delivery import _measured_lines
from tasks.host_report.remote_bundle import fetch_bundle

REPO_ROOT = Path(__file__).resolve().parent.parent
REMOTE_PROGRAM = REPO_ROOT / "deploy" / "vps-report" / "vps-readonly"

#: The path the program reads by default. Tests rewrite it in a copy so the
#: program itself keeps ignoring every argument and environment variable.
DEFAULT_CONFIG_PATH = "/etc/vps-readonly.json"

FAKE_DOCKER = """\
case "$1" in
  ps)
    echo '{"ID":"abc","Names":"web","Image":"nginx:1","State":"running","Status":"Up"}'
    echo '{"ID":"def","Names":"api","Image":"api:1","State":"running","Status":"Up"}'
    ;;
  inspect) echo '{"Name":"/x","RestartCount":0,"State":{"Status":"running"}}' ;;
  logs) printf '2026-11-13T00:00:00.000000000Z line\\n' ;;
esac
"""


def _run_program(
    tmp_path: Path,
    config,
    docker_body: str | None = FAKE_DOCKER,
    replace: tuple[str, str] | None = None,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if docker_body is not None:
        docker = bin_dir / "docker"
        docker.write_text("#!/bin/sh\n" + docker_body)
        docker.chmod(0o755)
    config_path = tmp_path / "vps-readonly.json"
    if config is not None:
        config_path.write_text(
            config if isinstance(config, str) else json.dumps(config)
        )
    program = tmp_path / "vps-readonly"
    body = REMOTE_PROGRAM.read_text(encoding="utf-8").replace(
        DEFAULT_CONFIG_PATH, str(config_path)
    )
    if replace is not None:
        body = body.replace(*replace)
    program.write_text(body)
    program.chmod(0o755)
    return subprocess.run(
        [sys.executable, str(program), "--ignored", "ignored-too"],
        capture_output=True,
        text=True,
        env={"PATH": str(bin_dir), "HOME": str(tmp_path)},
        timeout=30,
    )


def _bundle(**overrides):
    document = {
        "version": 1,
        "collected_at": "2026-11-13T00:00:00Z",
        "truncated": False,
        "host": {"cpu_count": 2},
        "containers": [
            {
                "listing": {"Id": "abc", "Names": ["web"], "Image": "nginx:1"},
                "inspect": {"Name": "/web", "RestartCount": 0},
                "logs": "",
            }
        ],
    }
    document.update(overrides)
    return document


def _fetch(tmp_path: Path, document):
    payload = tmp_path / "payload.json"
    payload.write_text(json.dumps(document))
    script = tmp_path / "ssh"
    script.write_text("#!/bin/sh\ncat " + str(payload) + "\n")
    script.chmod(0o755)
    return fetch_bundle(
        ssh_target="vps-report@vps.example",
        timeout_seconds=5.0,
        ssh_binary=str(script),
    )


def test_without_a_config_every_running_container_is_collected(tmp_path):
    completed = _run_program(tmp_path, config=None)

    assert completed.returncode == 0
    document = json.loads(completed.stdout)
    assert [item["listing"]["Id"] for item in document["containers"]] == ["abc", "def"]
    assert "selection" not in document


def test_the_config_limits_the_bundle_to_the_named_containers(tmp_path):
    completed = _run_program(tmp_path, config={"containers": ["web"]})

    assert completed.returncode == 0
    document = json.loads(completed.stdout)
    assert [item["listing"]["Id"] for item in document["containers"]] == ["abc"]
    assert document["selection"] == {"configured": ["web"], "missing": []}


def test_a_configured_container_that_is_not_running_is_named(tmp_path):
    completed = _run_program(tmp_path, config={"containers": ["web", "db"]})

    document = json.loads(completed.stdout)

    assert document["selection"] == {"configured": ["web", "db"], "missing": ["db"]}


def test_an_empty_config_collects_no_containers_on_purpose(tmp_path):
    completed = _run_program(tmp_path, config={"containers": []})

    assert completed.returncode == 0
    document = json.loads(completed.stdout)
    assert document["containers"] == []
    assert document["selection"] == {"configured": [], "missing": []}
    assert document["host"]["cpu_count"] >= 1


@pytest.mark.parametrize(
    "config",
    [
        "{not json",
        {"container": ["web"]},
        {"containers": "web"},
        {"containers": [""]},
    ],
)
def test_a_broken_config_is_refused_instead_of_ignored(tmp_path, config):
    completed = _run_program(tmp_path, config=config)

    assert completed.returncode == 1
    assert json.loads(completed.stdout) == {
        "version": 1,
        "error": "config_invalid",
    }


def test_the_bundle_carries_the_selection_to_the_report(tmp_path):
    bundle = _fetch(
        tmp_path,
        _bundle(selection={"configured": ["web", "db"], "missing": ["db"]}),
    )

    assert bundle.selection_missing == ("db",)


def test_a_bundle_without_selection_has_nothing_missing(tmp_path):
    bundle = _fetch(tmp_path, _bundle())

    assert bundle.selection_missing == ()


def test_a_missing_expected_container_becomes_a_finding():
    findings = selection_findings(("db",))

    assert len(findings) == 1
    finding = findings[0]
    assert finding.code == "expected_container_missing"
    assert finding.severity == "high"
    assert finding.kind == "service"
    assert finding.values["name"] == "db"
    assert "db" in finding.message


def test_a_local_report_has_no_selection_findings():
    assert selection_findings(()) == []


def test_the_message_says_a_expected_service_is_not_running():
    lines = _measured_lines(
        {
            "host_findings": [
                {
                    "code": "expected_container_missing",
                    "severity": "high",
                    "message": "expected container 'db' is not running",
                    "kind": "service",
                    "values": {"name": "db"},
                }
            ]
        }
    )

    assert "⛔ Servicio sin correr: db" in lines


def test_a_remote_config_error_is_its_own_reason(tmp_path):
    from tasks.host_report.remote_bundle import BundleUnavailable

    with pytest.raises(BundleUnavailable) as caught:
        _fetch(tmp_path, {"version": 1, "error": "config_invalid"})

    assert caught.value.reason == "config_invalid"


def test_the_message_names_the_remote_config_as_the_problem():
    from tasks.host_report.delivery import UNREACHABLE_SENTENCES

    assert "vps-readonly" in UNREACHABLE_SENTENCES["config_invalid"]


CHATTY_DOCKER = """\
case "$1" in
  ps)
    echo '{"ID":"abc","Names":"web","Image":"nginx:1","State":"running","Status":"Up"}'
    echo '{"ID":"def","Names":"db","Image":"pg:1","State":"running","Status":"Up"}'
    ;;
  inspect) echo '{"Name":"/x","RestartCount":0,"State":{"Status":"running"}}' ;;
  logs)
    if [ "$5" = "abc" ]; then
      i=0
      while [ "$i" -lt 40 ]; do
        echo "2026-11-13T00:00:00.000000000Z web line $i"
        i=$((i + 1))
      done
    else
      i=0
      while [ "$i" -lt 40 ]; do
        echo "2026-11-13T00:00:00.000000000Z db a very noisy postgres line number $i"
        i=$((i + 1))
      done
    fi
    ;;
esac
"""


def test_a_noisy_container_cannot_starve_the_others(tmp_path):
    completed = _run_program(
        tmp_path,
        config=None,
        docker_body=CHATTY_DOCKER,
        replace=("LOG_BYTES_PER_CONTAINER = 300 * 1024", "LOG_BYTES_PER_CONTAINER = 400"),
    )

    document = json.loads(completed.stdout)
    by_name = {c["listing"]["Names"][0]: c for c in document["containers"]}

    assert by_name["web"]["logs"], "the quiet container must keep its lines"
    assert by_name["db"]["logs_truncated"] is True
    assert len(by_name["db"]["logs"].encode()) <= 400
    assert document["truncated"] is False


def test_a_small_log_is_not_marked_as_cut(tmp_path):
    completed = _run_program(tmp_path, config=None, docker_body=CHATTY_DOCKER)

    document = json.loads(completed.stdout)
    by_name = {c["listing"]["Names"][0]: c for c in document["containers"]}

    assert "logs_truncated" not in by_name["web"]
    assert "line 39" in by_name["web"]["logs"]
