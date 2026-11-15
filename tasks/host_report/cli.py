"""Command line entry point: ``python -m tasks.host_report``.

Exit codes: ``0`` report written, ``1`` a source is unreachable (the Docker
endpoint or the remote SSH bundle) or the container listing failed, ``2``
invalid arguments or configuration, ``3`` rendered prompt over budget, ``4``
strict mode could not obtain an interpretation, ``5`` delivery through the hub
failed. Only the report is written to stdout; diagnostics go to stderr.
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from tasks.host_report.collector import collect
from tasks.host_report.delivery import (
    DeliveryError,
    deduplication_key,
    deliver,
    render_message,
)
from tasks.host_report.docker_api import DockerError, DockerUnavailable
from tasks.host_report.prompt import PromptBudgetExceeded
from tasks.host_report.remote_bundle import remote_sentence
from tasks.host_report.report import render_json, render_text
from tasks.host_report.settings import SettingsError, build_settings
from tasks.host_report.writer import ModelFailure, write_report

__all__ = ["build_parser", "main"]

PROGRAM = "python -m tasks.host_report"

EXIT_OK = 0
EXIT_DOCKER = 1
EXIT_ARGUMENTS = 2
EXIT_BUDGET = 3
EXIT_MODEL = 4
EXIT_DELIVERY = 5


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be greater than zero, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROGRAM,
        description=(
            "Collect a deterministic digest of every running container's logs. "
            "No model is called; diagnostics go to stderr, the report to stdout."
        ),
    )
    parser.add_argument(
        "--docker-url",
        default=None,
        metavar="URL",
        help=(
            "Docker Engine base URL, unix:///var/run/docker.sock or "
            "http://host:port (default: $HOST_REPORT_DOCKER_URL or "
            "unix:///var/run/docker.sock)"
        ),
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="DURATION",
        help="log window ending now, e.g. 24h or 7d (default: $HOST_REPORT_SINCE or 24h)",
    )
    parser.add_argument(
        "--format",
        choices=("json", "text"),
        default=None,
        help="output format (default: json)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="per-request timeout (default: $HOST_REPORT_TIMEOUT or 10)",
    )
    parser.add_argument(
        "--max-lines",
        type=_positive_int,
        default=None,
        metavar="N",
        help="maximum log lines read per container (default: $HOST_REPORT_MAX_LINES or 2000)",
    )
    parser.add_argument(
        "--max-clusters",
        type=_positive_int,
        default=None,
        metavar="N",
        help="maximum clusters kept per container (default: $HOST_REPORT_MAX_CLUSTERS or 25)",
    )
    parser.add_argument(
        "--max-containers",
        type=_positive_int,
        default=None,
        metavar="N",
        help="maximum containers collected (default: $HOST_REPORT_MAX_CONTAINERS or 50)",
    )
    parser.add_argument(
        "--ignore",
        action="append",
        default=None,
        metavar="REGEX",
        help=(
            "extra line pattern dropped before clustering; repeatable and "
            "added to the defaults (env: $HOST_REPORT_IGNORE, comma separated)"
        ),
    )
    parser.add_argument(
        "--report",
        choices=("none", "markdown"),
        default=None,
        help=(
            "output the markdown report with facts and interpretation, calling "
            "the gateway once (default: none, the JSON digest)"
        ),
    )
    parser.add_argument(
        "--strict-model",
        action="store_true",
        default=False,
        help=(
            "with --report markdown, exit 4 instead of falling back to a "
            "facts-only report when the interpretation is unavailable"
        ),
    )
    parser.add_argument(
        "--prompt-budget",
        type=_positive_int,
        default=None,
        metavar="N",
        help=(
            "maximum rendered prompt characters before the gateway is called "
            "(default: $HOST_REPORT_PROMPT_BUDGET or 3200)"
        ),
    )
    parser.add_argument(
        "--notify",
        choices=("none", "hub"),
        default=None,
        help=(
            "send the compact summary to WhatsApp through the personal hub; "
            "requires --report markdown (default: $HOST_REPORT_NOTIFY or none)"
        ),
    )
    return parser


def _hub_diagnostics(status: int | None, elapsed: float) -> None:
    """Log the status code and elapsed time; never a token or a body."""
    status_text = "none" if status is None else str(status)
    print(
        f"host_report: hub status={status_text} elapsed={elapsed:.2f}s",
        file=sys.stderr,
    )


def _gateway_diagnostics(status: int | None, elapsed: float) -> None:
    """Log only the status code and the elapsed time; never a body or a key."""
    status_text = "none" if status is None else str(status)
    print(
        f"host_report: gateway status={status_text} elapsed={elapsed:.2f}s",
        file=sys.stderr,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run one collection and return the process exit code."""
    parser = build_parser()
    try:
        arguments = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exit_request:
        return int(exit_request.code or 0)

    try:
        settings = build_settings(arguments)
    except SettingsError as error:
        print(f"host_report: error: {error}", file=sys.stderr)
        return EXIT_ARGUMENTS

    try:
        report = collect(settings)
    except DockerUnavailable as error:
        print(f"host_report: docker endpoint unreachable: {error}", file=sys.stderr)
        return EXIT_DOCKER
    except DockerError as error:
        print(f"host_report: docker error: {error}", file=sys.stderr)
        return EXIT_DOCKER

    if settings.report_format == "markdown":
        return _unreachable_result(report, _write_markdown(report, settings))

    if settings.output_format == "text":
        sys.stdout.write(render_text(report))
    else:
        sys.stdout.write(render_json(report))
    return _unreachable_result(report, EXIT_OK)


def _unreachable_result(report: dict, code: int) -> int:
    """Turn an unreachable source into exit ``1`` after the report went out.

    The report is written and delivered first, so the human still gets the
    message when the host is down. Any other non-zero code keeps its meaning;
    a delivery failure stays a delivery failure.
    """
    reason = report.get("unreachable")
    if code != EXIT_OK or not reason:
        return code
    print(f"host_report: {remote_sentence(str(reason))}", file=sys.stderr)
    return EXIT_DOCKER


def _write_markdown(report: dict, settings) -> int:
    try:
        rendered = write_report(
            report, settings, diagnostics=_gateway_diagnostics
        )
    except PromptBudgetExceeded as error:
        print(f"host_report: error: {error}", file=sys.stderr)
        return EXIT_BUDGET
    except ModelFailure as error:
        print(
            "host_report: error: model interpretation unavailable: "
            f"{error.reason}",
            file=sys.stderr,
        )
        return EXIT_MODEL
    sys.stdout.write(rendered.markdown)
    if settings.hub is None:
        return EXIT_OK
    return _deliver(report, rendered, settings)


def _deliver(report: dict, rendered, settings) -> int:
    """Send the compact summary; the report is already on stdout."""
    message = render_message(
        report,
        model_status=rendered.model_status,
        model_reason=rendered.model_reason,
        interpretation=rendered.interpretation,
    )
    key = deduplication_key(report, settings.hub.deduplication_key)
    try:
        result = deliver(
            settings.hub, message, key, diagnostics=_hub_diagnostics
        )
    except DeliveryError as error:
        print(f"host_report: delivery failed: {error.reason}", file=sys.stderr)
        return EXIT_DELIVERY
    print(f"host_report: hub result={result}", file=sys.stderr)
    return EXIT_OK
