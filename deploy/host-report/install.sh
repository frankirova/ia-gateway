#!/bin/sh
# One-shot installer for the host report, run as root on the report host.
#
#   sh install.sh --print-plan <staged-dir>   # dry run, unprivileged, writes nothing
#   sudo sh install.sh <staged-dir>           # install
#
# <staged-dir> holds tasks/host_report/ (the package), a copy of
# host-report-run, vps-report-run and env.example, vps.env.example. Both
# wrappers and both environment files are part of one installation, because the
# second reported host is collected from this same server.
# No network, no sudo, no docker, no git.
# The account that runs the report defaults to $SUDO_USER; --account overrides it
# and owns the config directory and the two secret files through its group.
set -eu

# Roots are overridable so an unprivileged dry run, and the tests, never depend
# on the real /opt, /usr/local/bin or /etc of the machine they run on.
OPT_ROOT=${HOST_REPORT_INSTALL_OPT_ROOT:-/opt/host-report}
BIN_DIR=${HOST_REPORT_INSTALL_BIN_DIR:-/usr/local/bin}
CONFIG_DIR=${HOST_REPORT_INSTALL_CONFIG_DIR:-/etc/host-report}

usage() {
	printf 'usage: install.sh [--print-plan] [--account NAME] <staged-dir>\n' >&2
}

plan_mode=0
staged=
report_account=${HOST_REPORT_ACCOUNT:-${SUDO_USER:-}}
while [ "$#" -gt 0 ]; do
	argument=$1
	shift
	case $argument in
	--print-plan) plan_mode=1 ;;
	--account)
		[ "$#" -gt 0 ] || { usage; exit 2; }
		report_account=$1
		shift
		;;
	-h | --help) usage; exit 0 ;;
	-*) usage; exit 2 ;;
	*)
		if [ -n "$staged" ]; then usage; exit 2; fi
		staged=$argument
		;;
	esac
done
[ -n "$staged" ] || { usage; exit 2; }

# The account that runs the report needs the config and the secrets, so its
# group owns them. Without it the files would be root-only and the forced
# command would fail at the first read.
if [ -z "$report_account" ]; then
	printf 'install: pass --account NAME, or run it with sudo from the account\n' >&2
	printf 'install: that will run the report\n' >&2
	exit 2
fi
if ! report_group=$(id -gn "$report_account" 2>/dev/null); then
	printf 'install: unknown report account: %s\n' "$report_account" >&2
	exit 2
fi

# The plan stays available without privileges; the real run must not be.
if [ "$plan_mode" -eq 0 ] && [ "$(id -u)" -ne 0 ]; then
	printf 'install: root is required; run it with sudo\n' >&2
	exit 2
fi

# Validate before the first write, so a bad staged dir leaves the host untouched.
for required in \
	tasks/host_report/__init__.py host-report-run vps-report-run \
	env.example vps.env.example; do
	if [ ! -f "$staged/$required" ]; then
		printf 'install: missing %s\n' "$staged/$required" >&2
		exit 2
	fi
done

# `ln -sfn` onto a real directory drops the link inside it, so refuse instead.
if [ -e "$OPT_ROOT/current" ] && [ ! -L "$OPT_ROOT/current" ]; then
	printf 'install: %s is not a symlink; refusing to repoint it\n' \
		"$OPT_ROOT/current" >&2
	exit 2
fi

# One choke point for every write: the plan prints the line and stops, the real
# run prints the same line and then performs it, so the two cannot drift.
act() {
	description=$1
	shift
	if [ "$plan_mode" -eq 1 ]; then
		printf 'would: %s\n' "$description"
		return 0
	fi
	printf '%s\n' "$description"
	"$@"
}

copy_tree() {
	cp -R "$staged/tasks" "$release/tasks"
	find "$release/tasks" -type d -exec chmod 0755 {} +
	find "$release/tasks" -type f -exec chmod 0644 {} +
}

# Never overwrite an existing file: the env, the key and the token may already
# hold operator state that a second run must preserve.
install_once() {
	if [ -e "$2" ]; then
		printf 'skip: %s exists, not overwritten\n' "$2"
		return 0
	fi
	act "install -m $3 -o root -g $report_group $1 $2" \
		install -m "$3" -o root -g "$report_group" "$1" "$2"
}

# UTC on purpose: the release name sorts as time and two runs never collide.
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
release=$OPT_ROOT/releases/$timestamp

act "install -d -m 0755 $release" install -d -m 0755 "$release"
act "copy $staged/tasks to $release/tasks (0755 directories, 0644 files)" copy_tree
# Atomic repoint: a reader sees the old release or the new one, never a gap.
act "ln -sfn $release $OPT_ROOT/current" ln -sfn "$release" "$OPT_ROOT/current"
act "install -m 0755 -o root -g root $staged/host-report-run $BIN_DIR/host-report-run" \
	install -m 0755 -o root -g root "$staged/host-report-run" \
	"$BIN_DIR/host-report-run"
act "install -m 0755 -o root -g root $staged/vps-report-run $BIN_DIR/vps-report-run" \
	install -m 0755 -o root -g root "$staged/vps-report-run" \
	"$BIN_DIR/vps-report-run"

if [ -d "$CONFIG_DIR" ]; then
	printf 'skip: %s exists\n' "$CONFIG_DIR"
else
	act "install -d -o root -g $report_group -m 0750 $CONFIG_DIR" \
		install -d -o root -g "$report_group" -m 0750 "$CONFIG_DIR"
fi
install_once "$staged/env.example" "$CONFIG_DIR/env" 0640
install_once "$staged/vps.env.example" "$CONFIG_DIR/vps.env" 0640
install_once /dev/null "$CONFIG_DIR/gateway.key" 0640
install_once /dev/null "$CONFIG_DIR/hub.token" 0640

if [ "$plan_mode" -eq 1 ]; then
	exit 0
fi

cat <<EOF
Manual steps that remain:
  - put the gateway application key in $CONFIG_DIR/gateway.key
  - put the hub send token in $CONFIG_DIR/hub.token
  - append the restrict,command="$BIN_DIR/host-report-run" public key line
    to the report account's authorized_keys
  - append the restrict,command="$BIN_DIR/vps-report-run" public key line,
    a different key from the one above, to the same authorized_keys
  - fill in HOST_REPORT_SSH_TARGET (and HOST_REPORT_ENDPOINTS) in
    $CONFIG_DIR/vps.env
  - check that host-report-run and vps-report-run run as that account
EOF
