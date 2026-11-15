"""Deploy contract: the forced command and the n8n workflow.

The wrapper is a shell script that runs on the report host, so these tests
exercise it as a subprocess with a fake ``date``, ``hostname`` and ``python3``
on ``PATH``. Nothing touches Docker, the network or the real filesystem outside
the test's temporary directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPOSITORY_ROOT / "deploy" / "host-report" / "host-report-run"
WORKFLOW = REPOSITORY_ROOT / "deploy" / "n8n" / "host-report.json"
CREDENTIAL_NAME = "host-report"
CREDENTIAL_ID_PLACEHOLDER = "REPLACE_WITH_CREDENTIAL_ID"
VPS_WRAPPER = REPOSITORY_ROOT / "deploy" / "host-report" / "vps-report-run"
VPS_WORKFLOW = REPOSITORY_ROOT / "deploy" / "n8n" / "vps-report.json"
VPS_CREDENTIAL_NAME = "vps-report"
FAKE_DATE = """#!/bin/sh
case "$1 $2" in
  "-u +%Y-%m-%d") echo "${FAKE_DAY:-2026-11-12}" ;;
  "-u +%H") echo "${FAKE_HOUR:-14}" ;;
esac
"""
FAKE_HOSTNAME = """#!/bin/sh
echo "${FAKE_HOST:-server}"
"""
FAKE_PYTHON = """#!/bin/sh
pwd
for argument in "$@"; do echo "$argument"; done
echo "KEY=${HOST_REPORT_NOTIFY_KEY:-}"
"""


@pytest.fixture
def wrapper_environment(tmp_path):
    """A PATH whose date, hostname and python3 are controlled by the test."""

    def build(*, hour: str = "14", day: str = "2026-11-12", host: str = "server"):
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir(exist_ok=True)
        for name, body in (
            ("date", FAKE_DATE),
            ("hostname", FAKE_HOSTNAME),
            ("python3", FAKE_PYTHON),
        ):
            path = fake_bin / name
            path.write_text(body, encoding="utf-8")
            path.chmod(0o755)
        checkout = tmp_path / "checkout"
        checkout.mkdir(exist_ok=True)
        environment = {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "FAKE_HOUR": hour,
            "FAKE_DAY": day,
            "FAKE_HOST": host,
            "HOST_REPORT_ROOT": str(checkout),
            "HOST_REPORT_ENV_FILE": str(tmp_path / "absent-env"),
        }
        return checkout, environment

    return build


def run_shell(script: str, environment: dict[str, str], cwd: Path):
    return subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(cwd),
        timeout=30,
    )


# --- the workflow -------------------------------------------------------


def load_workflow() -> dict:
    return json.loads(WORKFLOW.read_text(encoding="utf-8"))


def node_by_type(document: dict, node_type: str) -> dict:
    matches = [node for node in document["nodes"] if node["type"] == node_type]
    assert len(matches) == 1, f"expected exactly one {node_type} node"
    return matches[0]


def test_workflow_only_schedules_and_invokes():
    document = load_workflow()

    assert document["name"] == "host-report"
    assert {node["type"] for node in document["nodes"]} == {
        "n8n-nodes-base.scheduleTrigger",
        "n8n-nodes-base.ssh",
    }
    assert document["active"] is False


def test_schedule_runs_every_six_hours():
    document = load_workflow()
    schedule = node_by_type(document, "n8n-nodes-base.scheduleTrigger")

    assert schedule["parameters"]["rule"]["interval"] == [
        {"field": "hours", "hoursInterval": 6}
    ]
    assert document["settings"]["timezone"] == "America/Argentina/Buenos_Aires"


def test_ssh_node_runs_the_forced_command_and_nothing_else():
    document = load_workflow()
    ssh = node_by_type(document, "n8n-nodes-base.ssh")

    assert ssh["parameters"]["resource"] == "command"
    assert ssh["parameters"]["operation"] == "execute"
    assert ssh["parameters"]["authentication"] == "privateKey"
    assert ssh["parameters"]["command"] == "host-report-run"
    assert ssh["parameters"]["cwd"] == "/"


def test_workflow_references_the_credential_by_name_only():
    document = load_workflow()
    ssh = node_by_type(document, "n8n-nodes-base.ssh")
    credential = ssh["credentials"]["sshPrivateKey"]

    assert credential["name"] == CREDENTIAL_NAME
    assert credential["id"] == CREDENTIAL_ID_PLACEHOLDER
    assert "PRIVATE KEY" not in WORKFLOW.read_text(encoding="utf-8")


def test_schedule_connects_to_the_ssh_node():
    document = load_workflow()
    targets = document["connections"]["Every 6 hours"]["main"][0]

    assert [target["node"] for target in targets] == ["Run host report"]


# --- the vps workflow ---------------------------------------------------


def load_vps_workflow() -> dict:
    return json.loads(VPS_WORKFLOW.read_text(encoding="utf-8"))


def test_vps_workflow_only_schedules_and_invokes():
    document = load_vps_workflow()

    assert document["name"] == "vps-report"
    assert [node["name"] for node in document["nodes"]] == [
        "Every 6 hours",
        "Run vps report",
    ]
    assert {node["type"] for node in document["nodes"]} == {
        "n8n-nodes-base.scheduleTrigger",
        "n8n-nodes-base.ssh",
    }
    assert {node["typeVersion"] for node in document["nodes"]} == {1.2, 1}
    assert document["active"] is False


def test_vps_schedule_runs_every_six_hours():
    document = load_vps_workflow()
    schedule = node_by_type(document, "n8n-nodes-base.scheduleTrigger")

    assert schedule["typeVersion"] == 1.2
    assert schedule["parameters"]["rule"]["interval"] == [
        {"field": "hours", "hoursInterval": 6}
    ]
    assert document["settings"] == {
        "executionOrder": "v1",
        "timezone": "America/Argentina/Buenos_Aires",
    }


def test_vps_ssh_node_runs_the_forced_command_and_nothing_else():
    document = load_vps_workflow()
    ssh = node_by_type(document, "n8n-nodes-base.ssh")

    assert ssh["typeVersion"] == 1
    assert ssh["parameters"] == {
        "authentication": "privateKey",
        "resource": "command",
        "operation": "execute",
        "command": "vps-report-run",
        "cwd": "/",
    }


def test_vps_workflow_references_the_credential_by_name_only():
    document = load_vps_workflow()
    ssh = node_by_type(document, "n8n-nodes-base.ssh")
    credential = ssh["credentials"]["sshPrivateKey"]

    assert credential == {
        "id": CREDENTIAL_ID_PLACEHOLDER,
        "name": VPS_CREDENTIAL_NAME,
    }
    assert "PRIVATE KEY" not in VPS_WORKFLOW.read_text(encoding="utf-8")


def test_vps_workflow_schedule_connects_to_its_ssh_node():
    document = load_vps_workflow()
    targets = document["connections"]["Every 6 hours"]["main"][0]

    assert [target["node"] for target in targets] == ["Run vps report"]


def test_vps_workflow_node_ids_are_unique_and_not_the_sibling_ids():
    vps_ids = [node["id"] for node in load_vps_workflow()["nodes"]]
    sibling_ids = [node["id"] for node in load_workflow()["nodes"]]

    assert len(set(vps_ids)) == len(vps_ids)
    assert set(vps_ids).isdisjoint(sibling_ids)


# --- the forced command -------------------------------------------------


def test_slot_key_uses_the_six_hour_block_of_the_utc_day(wrapper_environment):
    expected = {
        "00": "host-report:server:2026-11-12T00",
        "05": "host-report:server:2026-11-12T00",
        "06": "host-report:server:2026-11-12T06",
        "11": "host-report:server:2026-11-12T06",
        "12": "host-report:server:2026-11-12T12",
        "18": "host-report:server:2026-11-12T18",
        "23": "host-report:server:2026-11-12T18",
    }
    for hour, key in expected.items():
        _, environment = wrapper_environment(hour=hour)
        result = run_shell(f". '{WRAPPER}'; slot_key", environment, Path(environment["HOME"]))
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == key


def test_slot_key_is_scoped_to_the_host(wrapper_environment):
    _, environment = wrapper_environment(host="other-host")

    result = run_shell(f". '{WRAPPER}'; slot_key", environment, Path(environment["HOME"]))

    assert result.stdout.strip() == "host-report:other-host:2026-11-12T12"


def test_sourcing_the_wrapper_does_not_start_a_report(wrapper_environment):
    _, environment = wrapper_environment()

    result = run_shell(
        f". '{WRAPPER}'; echo sourced", environment, Path(environment["HOME"])
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sourced"


def test_run_report_executes_the_module_in_the_checkout(wrapper_environment):
    checkout, environment = wrapper_environment()

    result = run_shell(
        f". '{WRAPPER}'; run_report", environment, Path(environment["HOME"])
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == str(checkout)
    assert lines[1:] == [
        "-m",
        "tasks.host_report",
        "--since",
        "6h",
        "--report",
        "markdown",
        "--notify",
        "hub",
        "KEY=host-report:server:2026-11-12T12",
    ]


def test_environment_file_can_pin_the_deduplication_key(
    wrapper_environment, tmp_path
):
    _, environment = wrapper_environment()
    env_file = tmp_path / "env"
    env_file.write_text("HOST_REPORT_NOTIFY_KEY=n8n-slot-42\n", encoding="utf-8")
    environment["HOST_REPORT_ENV_FILE"] = str(env_file)

    result = run_shell(
        f". '{WRAPPER}'; run_report", environment, Path(environment["HOME"])
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "KEY=n8n-slot-42"


def test_environment_file_carries_the_connection_settings(
    wrapper_environment, tmp_path
):
    _, environment = wrapper_environment()
    env_file = tmp_path / "env"
    env_file.write_text(
        "HOST_REPORT_HUB_URL=http://127.0.0.1:8000\n"
        "HOST_REPORT_GATEWAY_URL=https://gateway.internal\n",
        encoding="utf-8",
    )
    environment["HOST_REPORT_ENV_FILE"] = str(env_file)

    result = run_shell(
        f". '{WRAPPER}'; . '{env_file}'; echo \"$HOST_REPORT_HUB_URL\"",
        environment,
        Path(environment["HOME"]),
    )

    assert result.stdout.strip() == "http://127.0.0.1:8000"


def test_wrapper_is_syntactically_valid():
    result = subprocess.run(
        ["sh", "-n", str(WRAPPER)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **os.environ},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr


# --- the vps forced command ---------------------------------------------


def test_vps_wrapper_is_syntactically_valid_and_executable():
    result = subprocess.run(
        ["sh", "-n", str(VPS_WRAPPER)],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", **os.environ},
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert VPS_WRAPPER.stat().st_mode & 0o111 == 0o111


def test_vps_wrapper_defaults_point_at_the_vps_paths(wrapper_environment):
    _, environment = wrapper_environment()
    environment.pop("HOST_REPORT_ROOT")
    environment.pop("HOST_REPORT_ENV_FILE")

    result = run_shell(
        f". '{VPS_WRAPPER}'; echo \"$ENV_FILE $REPORT_ROOT $WINDOW $SLOT_HOURS\"",
        environment,
        Path(environment["HOME"]),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        "/etc/host-report/vps.env /opt/host-report/current 6h 6"
    )


def test_vps_wrapper_shares_the_window_defaults_with_the_sibling(
    wrapper_environment,
):
    """Both wrappers must read the same HOST_REPORT_* variables."""
    _, environment = wrapper_environment()

    sibling = run_shell(
        f". '{WRAPPER}'; echo \"$WINDOW $SLOT_HOURS\"",
        environment,
        Path(environment["HOME"]),
    )
    vps = run_shell(
        f". '{VPS_WRAPPER}'; echo \"$WINDOW $SLOT_HOURS\"",
        environment,
        Path(environment["HOME"]),
    )

    assert sibling.returncode == 0, sibling.stderr
    assert vps.returncode == 0, vps.stderr
    assert sibling.stdout.strip() == vps.stdout.strip() == "6h 6"

    environment["HOST_REPORT_WINDOW"] = "12h"
    environment["HOST_REPORT_SLOT_HOURS"] = "12"
    sibling = run_shell(
        f". '{WRAPPER}'; echo \"$WINDOW $SLOT_HOURS\"",
        environment,
        Path(environment["HOME"]),
    )
    vps = run_shell(
        f". '{VPS_WRAPPER}'; echo \"$WINDOW $SLOT_HOURS\"",
        environment,
        Path(environment["HOME"]),
    )

    assert sibling.stdout.strip() == vps.stdout.strip() == "12h 12"


def test_vps_slot_key_uses_the_six_hour_block_of_the_utc_day(wrapper_environment):
    expected = {
        "00": "vps-report:vps:2026-11-12T00",
        "05": "vps-report:vps:2026-11-12T00",
        "06": "vps-report:vps:2026-11-12T06",
        "12": "vps-report:vps:2026-11-12T12",
        "18": "vps-report:vps:2026-11-12T18",
        "23": "vps-report:vps:2026-11-12T18",
    }
    for hour, key in expected.items():
        _, environment = wrapper_environment(hour=hour)
        result = run_shell(
            f". '{VPS_WRAPPER}'; slot_key", environment, Path(environment["HOME"])
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == key


def test_vps_slot_key_labels_the_configured_host_not_the_local_one(
    wrapper_environment,
):
    """The wrapper runs on the server, so hostname is the wrong label."""
    _, environment = wrapper_environment(host="collector-server")
    environment["HOST_REPORT_HOST"] = "vps-prod"

    result = run_shell(
        f". '{VPS_WRAPPER}'; slot_key", environment, Path(environment["HOME"])
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "vps-report:vps-prod:2026-11-12T12"


def test_vps_sourcing_the_wrapper_does_not_start_a_report(wrapper_environment):
    _, environment = wrapper_environment()

    result = run_shell(
        f". '{VPS_WRAPPER}'; echo sourced", environment, Path(environment["HOME"])
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "sourced"


def test_vps_run_report_ignores_the_command_the_client_asks_for(
    wrapper_environment,
):
    checkout, environment = wrapper_environment()
    hostile = "--since 9999h --notify whatsapp --key stolen"
    environment["SSH_ORIGINAL_COMMAND"] = hostile

    result = run_shell(
        f". '{VPS_WRAPPER}'; run_report", environment, Path(environment["HOME"])
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == str(checkout)
    assert lines[1:] == [
        "-m",
        "tasks.host_report",
        "--since",
        "6h",
        "--report",
        "markdown",
        "--notify",
        "hub",
        "KEY=vps-report:vps:2026-11-12T12",
    ]
    assert "whatsapp" not in result.stdout
    assert "9999h" not in result.stdout
    assert "stolen" not in result.stdout


def test_vps_run_report_labels_the_host_from_the_environment_file(
    wrapper_environment, tmp_path
):
    _, environment = wrapper_environment(host="collector-server")
    env_file = tmp_path / "vps.env"
    env_file.write_text("HOST_REPORT_HOST=vps-prod\n", encoding="utf-8")
    environment["HOST_REPORT_ENV_FILE"] = str(env_file)

    result = run_shell(
        f". '{VPS_WRAPPER}'; run_report", environment, Path(environment["HOME"])
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "KEY=vps-report:vps-prod:2026-11-12T12"


def test_vps_environment_file_can_pin_the_deduplication_key(
    wrapper_environment, tmp_path
):
    _, environment = wrapper_environment()
    env_file = tmp_path / "vps.env"
    env_file.write_text("HOST_REPORT_NOTIFY_KEY=n8n-slot-42\n", encoding="utf-8")
    environment["HOST_REPORT_ENV_FILE"] = str(env_file)

    result = run_shell(
        f". '{VPS_WRAPPER}'; run_report", environment, Path(environment["HOME"])
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "KEY=n8n-slot-42"


# --- the installer ------------------------------------------------------

INSTALLER = REPOSITORY_ROOT / "deploy" / "host-report" / "install.sh"
ENV_EXAMPLE = REPOSITORY_ROOT / "deploy" / "host-report" / "env.example"
VPS_ENV_EXAMPLE = REPOSITORY_ROOT / "deploy" / "host-report" / "vps.env.example"
README = REPOSITORY_ROOT / "deploy" / "host-report" / "README.md"
RELEASE_TIMESTAMP = "20261112T120000Z"
FAKE_DATE_FOR_INSTALL = f"""#!/bin/sh
echo {RELEASE_TIMESTAMP}
"""


def staged_directory(
    tmp_path: Path, *, wrapper: bool = True, vps_wrapper: bool = True
) -> Path:
    """A staged dir shaped like the one the operator ships to the host."""
    staged = tmp_path / "staged"
    package = staged / "tasks" / "host_report"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name, source in (
        ("env.example", ENV_EXAMPLE),
        ("vps.env.example", VPS_ENV_EXAMPLE),
    ):
        (staged / name).write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    if wrapper:
        (staged / "host-report-run").write_text(
            WRAPPER.read_text(encoding="utf-8"), encoding="utf-8"
        )
    if vps_wrapper:
        (staged / "vps-report-run").write_text(
            VPS_WRAPPER.read_text(encoding="utf-8"), encoding="utf-8"
        )
    return staged


def installer_environment(tmp_path: Path) -> dict[str, str]:
    """An unprivileged PATH with a fixed clock and install roots in tmp_path."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    date = fake_bin / "date"
    date.write_text(FAKE_DATE_FOR_INSTALL, encoding="utf-8")
    date.chmod(0o755)
    return {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "HOST_REPORT_INSTALL_OPT_ROOT": str(tmp_path / "roots" / "opt"),
        "HOST_REPORT_INSTALL_BIN_DIR": str(tmp_path / "roots" / "bin"),
        "HOST_REPORT_INSTALL_CONFIG_DIR": str(tmp_path / "roots" / "etc"),
    }


def run_installer(arguments: list[str], environment: dict[str, str], cwd: Path):
    return subprocess.run(
        ["sh", str(INSTALLER), *arguments],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(cwd),
        timeout=30,
    )


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def current_account() -> tuple[str, str]:
    """The account the tests install for, and its primary group."""
    user = subprocess.run(
        ["id", "-un"], capture_output=True, text=True, check=True
    ).stdout.strip()
    group = subprocess.run(
        ["id", "-gn", user], capture_output=True, text=True, check=True
    ).stdout.strip()
    return user, group


def install_arguments(*arguments: str) -> list[str]:
    """Every installer call names the account, because it owns the secrets."""
    user, _ = current_account()
    return [*arguments, "--account", user]


def test_install_print_plan_lists_every_action(tmp_path):
    staged = staged_directory(tmp_path)
    environment = installer_environment(tmp_path)
    opt = Path(environment["HOST_REPORT_INSTALL_OPT_ROOT"])
    bin_dir = Path(environment["HOST_REPORT_INSTALL_BIN_DIR"])
    config = Path(environment["HOST_REPORT_INSTALL_CONFIG_DIR"])
    release = f"{opt}/releases/{RELEASE_TIMESTAMP}"

    result = run_installer(
        install_arguments("--print-plan", str(staged)), environment, tmp_path
    )

    assert result.returncode == 0, result.stderr
    plan = [
        line for line in result.stdout.splitlines() if line.startswith("would: ")
    ]
    _, group = current_account()
    in_order = [
        f"would: install -d -m 0755 {release}",
        f"would: ln -sfn {release} {opt}/current",
        f"would: install -m 0755 -o root -g root {staged}/host-report-run "
        f"{bin_dir}/host-report-run",
        f"would: install -m 0640 -o root -g {group} {staged}/env.example {config}/env",
        f"would: install -m 0640 -o root -g {group} /dev/null {config}/gateway.key",
        f"would: install -m 0640 -o root -g {group} /dev/null {config}/hub.token",
    ]
    positions = []
    for line in in_order:
        assert line in plan, plan
        positions.append(plan.index(line))
    assert positions == sorted(positions), plan


def test_install_print_plan_changes_nothing(tmp_path):
    staged = staged_directory(tmp_path)
    environment = installer_environment(tmp_path)
    work = tmp_path / "work"
    work.mkdir()
    before = snapshot(staged)

    result = run_installer(
        install_arguments("--print-plan", str(staged)), environment, work
    )

    assert result.returncode == 0, result.stderr
    assert snapshot(staged) == before
    assert list(work.iterdir()) == []
    for name in (
        "HOST_REPORT_INSTALL_OPT_ROOT",
        "HOST_REPORT_INSTALL_BIN_DIR",
        "HOST_REPORT_INSTALL_CONFIG_DIR",
    ):
        assert not Path(environment[name]).exists()
    # The VPS half of the plan writes nothing either: no env file, no wrapper.
    config = Path(environment["HOST_REPORT_INSTALL_CONFIG_DIR"])
    for name in ("env", "vps.env", "gateway.key", "hub.token"):
        assert not (config / name).exists()


def test_install_print_plan_lists_both_wrappers_and_both_env_files(tmp_path):
    """One installation carries the server and the VPS half, in one order."""
    staged = staged_directory(tmp_path)
    environment = installer_environment(tmp_path)
    bin_dir = Path(environment["HOST_REPORT_INSTALL_BIN_DIR"])
    config = Path(environment["HOST_REPORT_INSTALL_CONFIG_DIR"])

    result = run_installer(
        install_arguments("--print-plan", str(staged)), environment, tmp_path
    )

    assert result.returncode == 0, result.stderr
    _, group = current_account()
    expected = [
        f"would: install -m 0755 -o root -g root {staged}/host-report-run "
        f"{bin_dir}/host-report-run",
        f"would: install -m 0755 -o root -g root {staged}/vps-report-run "
        f"{bin_dir}/vps-report-run",
        f"would: install -m 0640 -o root -g {group} {staged}/env.example "
        f"{config}/env",
        f"would: install -m 0640 -o root -g {group} {staged}/vps.env.example "
        f"{config}/vps.env",
    ]
    plan = [
        line for line in result.stdout.splitlines() if line.startswith("would: ")
    ]
    positions = []
    for line in expected:
        assert line in plan, result.stdout
        assert plan.count(line) == 1, result.stdout
        positions.append(plan.index(line))
    # Stable order: both wrappers first, then both env files, sibling first.
    assert positions == sorted(positions), result.stdout
    # Still a plan and nothing else: no command, no skip line, no prose.
    assert all(line.startswith("would: ") for line in result.stdout.splitlines()), (
        result.stdout
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="needs an unprivileged process")
def test_install_refuses_without_root(tmp_path):
    staged = staged_directory(tmp_path)
    environment = installer_environment(tmp_path)

    result = run_installer(install_arguments(str(staged)), environment, tmp_path)

    assert result.returncode == 2, result.stdout
    assert "root is required" in result.stderr
    assert not Path(environment["HOST_REPORT_INSTALL_OPT_ROOT"]).exists()


def test_install_rejects_an_incomplete_staged_dir(tmp_path):
    staged = staged_directory(tmp_path, wrapper=False)
    environment = installer_environment(tmp_path)

    result = run_installer(
        install_arguments("--print-plan", str(staged)), environment, tmp_path
    )

    assert result.returncode == 2
    assert str(staged / "host-report-run") in result.stderr
    assert "would: " not in result.stdout


@pytest.mark.parametrize("missing", ["vps-report-run", "vps.env.example"])
def test_install_rejects_a_staged_dir_missing_the_vps_artifacts(tmp_path, missing):
    """Half a staged dir must fail validation, before any write."""
    staged = staged_directory(tmp_path)
    (staged / missing).unlink()
    environment = installer_environment(tmp_path)
    work = tmp_path / "work"
    work.mkdir()

    result = run_installer(
        install_arguments("--print-plan", str(staged)), environment, work
    )

    assert result.returncode == 2
    assert str(staged / missing) in result.stderr
    assert "would: " not in result.stdout
    assert list(work.iterdir()) == []
    for name in (
        "HOST_REPORT_INSTALL_OPT_ROOT",
        "HOST_REPORT_INSTALL_BIN_DIR",
        "HOST_REPORT_INSTALL_CONFIG_DIR",
    ):
        assert not Path(environment[name]).exists()


def test_install_requires_a_report_account(tmp_path):
    """Without an account the secrets would be root-only and the job locked out."""
    staged = staged_directory(tmp_path)
    environment = installer_environment(tmp_path)
    environment.pop("SUDO_USER", None)
    environment.pop("HOST_REPORT_ACCOUNT", None)

    result = run_installer(["--print-plan", str(staged)], environment, tmp_path)

    assert result.returncode == 2
    assert "--account" in result.stderr
    assert "would: " not in result.stdout


# --- the vps environment skeleton ---------------------------------------

#: Every key the VPS profile and the wrapper need from /etc/host-report/vps.env.
#: ``HOST_REPORT_IGNORE`` and ``HOST_REPORT_CONTAINER_LIMITS`` stay commented on
#: purpose: the defaults are the intended configuration.
VPS_PROFILE_KEYS = (
    "HOST_REPORT_HOST",
    "HOST_REPORT_TARGET",
    "HOST_REPORT_SSH_TARGET",
    "HOST_REPORT_SSH_TIMEOUT",
    "HOST_REPORT_ENDPOINTS",
    "HOST_REPORT_GATEWAY_URL",
    "HOST_REPORT_GATEWAY_KEY_FILE",
    "HOST_REPORT_GATEWAY_CA_BUNDLE",
    "HOST_REPORT_HUB_URL",
    "HOST_REPORT_HUB_TOKEN_FILE",
    "HOST_REPORT_HUB_CHAT",
)


def environment_assignments(path: Path) -> dict[str, str]:
    """The active ``KEY=value`` pairs of a shell-sourced env file."""
    assignments: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        assignments[key] = value
    return assignments


def test_vps_env_example_lists_every_key_the_profile_needs():
    assignments = environment_assignments(VPS_ENV_EXAMPLE)

    assert set(assignments) == set(VPS_PROFILE_KEYS), assignments
    assert assignments["HOST_REPORT_HOST"] == "vps"
    assert assignments["HOST_REPORT_TARGET"] == "ssh"
    assert assignments["HOST_REPORT_SSH_TIMEOUT"] == "60"
    # The operator fills these two; an installed skeleton sends nothing to the
    # wrong host and probes nothing it was not told about.
    assert assignments["HOST_REPORT_SSH_TARGET"] == ""
    assert assignments["HOST_REPORT_ENDPOINTS"] == ""
    text = VPS_ENV_EXAMPLE.read_text(encoding="utf-8")
    for defaulted in ("HOST_REPORT_IGNORE", "HOST_REPORT_CONTAINER_LIMITS"):
        assert f"# {defaulted}=" in text, defaulted


def test_vps_env_example_documents_the_two_endpoint_example():
    text = VPS_ENV_EXAMPLE.read_text(encoding="utf-8")
    commented = next(
        line
        for line in text.splitlines()
        if line.startswith("# HOST_REPORT_ENDPOINTS=")
    )
    document = json.loads(commented.split("=", 1)[1])

    assert len(document) == 2
    for entry in document:
        assert set(entry) == {"name", "url", "expect_status"}
        assert entry["url"].startswith("https://")
    assert [entry["name"] for entry in document] == ["app", "api"]


def test_vps_env_example_carries_no_secret_value():
    text = VPS_ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in text
    assignments = environment_assignments(VPS_ENV_EXAMPLE)
    for key, value in assignments.items():
        # Secrets are files: a key or token may only appear as a path to one.
        if "TOKEN" in key or "KEY" in key:
            assert key.endswith("_FILE"), key
        assert re.fullmatch(r"[A-Za-z0-9_\-]{32,}", value) is None, key
    # Same reporter, same secrets: the skeleton points at the existing files.
    server = environment_assignments(ENV_EXAMPLE)
    assert (
        assignments["HOST_REPORT_GATEWAY_KEY_FILE"]
        == server["HOST_REPORT_GATEWAY_KEY_FILE"]
    )
    assert (
        assignments["HOST_REPORT_HUB_TOKEN_FILE"]
        == server["HOST_REPORT_HUB_TOKEN_FILE"]
    )


# --- the README ---------------------------------------------------------


def test_readme_documents_both_forced_command_lines():
    """Both wrappers need their own ``restrict,command=`` line to be reachable."""
    text = README.read_text(encoding="utf-8")

    assert 'restrict,command="/usr/local/bin/host-report-run"' in text
    assert 'restrict,command="/usr/local/bin/vps-report-run"' in text
    assert "vps.env.example" in text
