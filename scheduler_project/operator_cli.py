"""Operator CLI for the scheduler-owned control API.

The CLI deliberately uses the same local HTTP boundary as the Django portal.
It does not write ``scheduler.db``, manipulate READY rows, or edit the Oracle
Schedule Master. That keeps a command-line action observable and safe in the
same way as an action from the web UI.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_API_URL = "http://127.0.0.1:8091"
CONTROL_ACTIONS = (
    "pause",
    "resume",
    "cancel",
    "activate",
    "manual_run",
    "clear_manual_run",
    "confirm",
    "clear_confirmation",
    "reset",
    "set_override",
    "clear_override",
)

_CONFIG_UNSET = object()


class ControlApiError(RuntimeError):
    """A concise, safe error returned by the local scheduler control plane."""


class SchedulerControlClient:
    """Tiny stdlib-only client for the scheduler-owned HTTP API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 8,
        actor: str | None = None,
    ):
        self.base_url = (base_url or os.environ.get("SCHEDULER_API_URL") or DEFAULT_API_URL).rstrip("/")
        self.token = token if token is not None else os.environ.get("SCHEDULER_API_TOKEN", "")
        self.timeout = timeout
        self.actor = (
            actor
            or os.environ.get("SCHEDULER_OPERATOR")
            or os.environ.get("USERNAME")
            or os.environ.get("USER")
            or "cli-operator"
        )

    def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["X-Scheduler-Token"] = self.token
        request = Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310 - admin-configured local API
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            detail = _read_error_detail(error)
            raise ControlApiError(f"API returned HTTP {error.code}: {detail}") from error
        except URLError as error:
            raise ControlApiError(
                f"Cannot reach the scheduler control API at {self.base_url}. "
                "Start the scheduler service or check SCHEDULER_API_URL. "
                f"({error.reason})"
            ) from error
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as error:
            raise ControlApiError("The scheduler control API returned invalid JSON.") from error

    def health(self) -> dict:
        return self._request("GET", "/health")

    def snapshot(self) -> dict:
        return self._request("GET", "/v1/operations/snapshot")

    def control(
        self,
        job_id: int,
        action: str,
        override_datetime: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """Record an intent with an accountable CLI actor and optional reason."""
        payload = {"actor": self.actor}
        if override_datetime:
            payload["override_datetime"] = override_datetime
        if reason:
            payload["reason"] = reason
        return self._request("POST", f"/v1/jobs/{job_id}/controls/{action}", payload)

    def service_control(self, action: str, reason: str | None = None) -> dict:
        """Safely enable or pause future scheduler cycles through the API."""
        payload = {"actor": self.actor}
        if reason:
            payload["reason"] = reason
        return self._request("POST", f"/v1/scheduler/controls/{action}", payload)

    def configure(
        self,
        job_id: int,
        *,
        is_active=_CONFIG_UNSET,
        run_by=_CONFIG_UNSET,
        reason: str | None = None,
    ) -> dict:
        """Change only the approved Scheduler Master operational fields."""
        if is_active is _CONFIG_UNSET and run_by is _CONFIG_UNSET:
            raise ControlApiError(
                "Choose --active/--inactive, a complete --from/--to window, or --clear-window."
            )
        payload = {"actor": self.actor}
        if is_active is not _CONFIG_UNSET:
            payload["is_active"] = is_active
        if run_by is not _CONFIG_UNSET:
            payload["run_by"] = run_by
        if reason:
            payload["reason"] = reason
        return self._request("PATCH", f"/v1/jobs/{job_id}/configuration", payload)


def _read_error_detail(error: HTTPError) -> str:
    try:
        payload = json.loads(error.read().decode("utf-8"))
        return str(payload.get("detail") or payload)
    except Exception:
        return error.reason or "request rejected"


def _status_counts(records: list[dict], key: str) -> str:
    counts = Counter(str(record.get(key, "UNKNOWN")).upper() for record in records)
    return ", ".join(f"{name}: {count}" for name, count in sorted(counts.items())) or "none"


def render_status(snapshot: dict, api_url: str) -> str:
    """Render a concise, human-readable operations snapshot."""
    meta = snapshot.get("meta", {})
    controls = snapshot.get("controls", [])
    staging = snapshot.get("staging", [])
    ready = snapshot.get("ready", [])
    queue = snapshot.get("priority_queue", [])
    executions = snapshot.get("executions", [])
    upcoming = snapshot.get("upcoming", [])
    lines = [
        "ITRP Oracle Scheduler — operator status",
        f"Control API: {api_url}",
        f"Snapshot: {meta.get('generated_at', 'not available')}",
        f"Worker started: {meta.get('service_started_at', 'not available')}",
        f"Next service cycle: {meta.get('next_cycle_at', 'not yet scheduled')}",
        f"Schedule Master jobs: {len(snapshot.get('schedule_master', []))}",
        f"Controls: {_status_counts(controls, 'control_status')}",
        f"Staging: {len(staging)} ({_status_counts(staging, 'state')})",
        f"Ready: {len(ready)}",
        f"Priority queue: {len(queue)}",
        f"Executions: {len(executions)} ({_status_counts(executions, 'status')})",
        f"Upcoming forecast: {len(upcoming)}",
    ]
    last_cycle = meta.get("last_cycle")
    if last_cycle:
        execution = last_cycle.get("execution", {})
        lines.append(
            "Last cycle: "
            f"processed={execution.get('total_processed', 0)}, "
            f"successful={execution.get('successful', 0)}, "
            f"failed={execution.get('failed', 0)}"
        )
    if upcoming:
        next_items = []
        for item in upcoming[:3]:
            when = item.get("execution_date") or item.get("occurrence_date") or "date pending"
            next_items.append(f"{item.get('job_name', 'Job')} ({when})")
        lines.append("Next upcoming: " + "; ".join(next_items))
    return "\n".join(lines)


def render_jobs(snapshot: dict) -> str:
    """Render Schedule Master jobs with their current control intent."""
    controls = {str(item.get("job_id")): item for item in snapshot.get("controls", [])}
    rows = []
    for job in snapshot.get("schedule_master", []):
        control = controls.get(str(job.get("id")), {})
        run_config = job.get("run_config") or {}
        frequencies = ", ".join(run_config.get("RUNS_ON", [])) or "—"
        rows.append(
            (
                str(job.get("id", "—")),
                str(job.get("name", "—")),
                "ACTIVE" if job.get("is_active") else "INACTIVE",
                str(control.get("control_status", "ACTIVE")),
                frequencies,
                str(job.get("margin", "T")),
            )
        )
    headers = ("ID", "JOB", "MASTER", "CONTROL", "RUNS", "MARGIN")
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))
    separator = "  ".join("-" * width for width in widths)
    rendered = ["  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)), separator]
    rendered.extend("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)) for row in rows)
    return "\n".join(rendered) if rows else "No schedules are available in Scheduler Master."


def _write_snapshot(snapshot: dict, output: str | None) -> None:
    text = json.dumps(snapshot, indent=2, default=str)
    if not output:
        print(text)
        return
    target = Path(output).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(target)
    print(f"Snapshot saved to {target}")


def _run_single_cycle() -> int:
    """Run the normal locked worker once; useful for an operator smoke run."""
    command = [sys.executable, str(BASE_DIR / "main.py"), "--once"]
    return subprocess.run(command, cwd=BASE_DIR, check=False).returncode


def _configuration_changes_from_args(args) -> dict:
    """Validate CLI flags before a request reaches the scheduler service."""
    is_active = getattr(args, "is_active", _CONFIG_UNSET)
    from_time = getattr(args, "from_time", None)
    to_time = getattr(args, "to_time", None)
    clear_window = bool(getattr(args, "clear_window", False))

    has_from = bool(from_time and str(from_time).strip())
    has_to = bool(to_time and str(to_time).strip())
    if has_from != has_to:
        raise ControlApiError("--from and --to must be supplied together.")
    if clear_window and (has_from or has_to):
        raise ControlApiError("Use either --clear-window or --from/--to, not both.")

    changes = {"is_active": is_active, "run_by": _CONFIG_UNSET}
    if has_from:
        changes["run_by"] = {
            "from_time": _normalise_cli_time(from_time),
            "to_time": _normalise_cli_time(to_time),
        }
        if changes["run_by"]["from_time"] == changes["run_by"]["to_time"]:
            raise ControlApiError("--from and --to must be different times.")
    elif clear_window:
        changes["run_by"] = None

    if changes["is_active"] is _CONFIG_UNSET and changes["run_by"] is _CONFIG_UNSET:
        raise ControlApiError(
            "No configuration change was supplied. Use --active/--inactive, --from with --to, or --clear-window."
        )
    return changes


def _normalise_cli_time(value: str) -> str:
    """Keep client-side time feedback clear; server repeats this validation."""
    from datetime import datetime

    try:
        return datetime.strptime(str(value).strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ControlApiError("Execution times must use 24-hour HH:MM format.") from None


def _positive_job_id(value: str) -> int:
    """Argparse validator matching the scheduler's positive numeric IDs."""
    try:
        job_id = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("job_id must be a positive integer.") from None
    if job_id < 1:
        raise argparse.ArgumentTypeError("job_id must be a positive integer.")
    return job_id


def execute_command(args, client: SchedulerControlClient) -> int:
    """Execute a parsed non-interactive CLI command."""
    if args.command == "cycle":
        return _run_single_cycle()
    if args.command == "health":
        print(json.dumps(client.health(), indent=2))
        return 0
    if args.command == "status":
        print(render_status(client.snapshot(), client.base_url))
        return 0
    if args.command == "snapshot":
        _write_snapshot(client.snapshot(), args.output)
        return 0
    if args.command == "jobs":
        print(render_jobs(client.snapshot()))
        return 0
    if args.command == "control":
        if args.action == "set_override" and not args.at:
            raise ControlApiError("--at is required for the set_override action.")
        response = client.control(args.job_id, args.action, args.at, getattr(args, "reason", None))
        control = response.get("control", response)
        print(
            f"Recorded {args.action} for job {args.job_id}. "
            f"Control state: {control.get('control_status', 'updated')}. "
            f"Actor: {client.actor}"
        )
        return 0
    if args.command == "configure":
        changes = _configuration_changes_from_args(args)
        response = client.configure(
            args.job_id,
            is_active=changes["is_active"],
            run_by=changes["run_by"],
            reason=getattr(args, "reason", None),
        )
        configuration = response.get("configuration", response)
        after = configuration.get("after", {})
        state = "active" if after.get("is_active") else "inactive"
        run_by = after.get("run_by")
        window = (
            f"{run_by.get('from_time')}–{run_by.get('to_time')}"
            if isinstance(run_by, dict)
            else "no time restriction"
        )
        print(
            f"Updated Scheduler Master job {args.job_id}: {state}; runs {window}. "
            f"Actor: {client.actor}"
        )
        warning = configuration.get("warning")
        if warning:
            print(f"Warning: {warning}")
        return 0
    if args.command == "service":
        response = client.service_control(args.action, args.reason)
        state = response.get("service_control", response)
        label = "enabled" if state.get("scheduler_enabled") else "paused"
        print(f"Future scheduler cycles are {label}. Actor: {client.actor}")
        return 0
    if args.command == "watch":
        while True:
            print("\x1bc", end="")
            print(render_status(client.snapshot(), client.base_url))
            time.sleep(args.interval)
    raise ControlApiError("Choose a scheduler command. Use --help for usage.")


def _interactive_shell(client: SchedulerControlClient) -> int:
    print("ITRP Oracle Scheduler interactive console. Type 'help' for commands; 'exit' to leave.")
    print("The scheduler service must be running for status and control commands.")
    while True:
        try:
            line = input("scheduler> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in {"exit", "quit"}:
            return 0
        if line.lower() == "help":
            print("status | jobs | snapshot [file] | health | cycle | start | stop | configure <job_id> [--active|--inactive] [--from HH:MM --to HH:MM|--clear-window] [--reason text] | <action> <job_id> [ISO datetime] [reason] | exit")
            print("Actions: " + ", ".join(CONTROL_ACTIONS))
            continue
        try:
            parts = shlex.split(line)
            command = parts[0].lower()
            if command in {"status", "jobs", "health"} and len(parts) == 1:
                parsed = argparse.Namespace(command=command)
            elif command == "snapshot":
                parsed = argparse.Namespace(command="snapshot", output=parts[1] if len(parts) > 1 else None)
            elif command == "cycle" and len(parts) == 1:
                parsed = argparse.Namespace(command="cycle")
            elif command in {"start", "stop"}:
                parsed = argparse.Namespace(
                    command="service",
                    action=command,
                    reason=" ".join(parts[1:]) or None,
                )
            elif command == "configure":
                # Reuse the same parser and validation as non-interactive CLI
                # usage, so an interactive action has the same audit payload.
                parsed = build_parser().parse_args(parts)
            elif command in CONTROL_ACTIONS and len(parts) >= 2:
                at = None
                reason_parts = parts[2:]
                if command in {"manual_run", "set_override"} and reason_parts:
                    at = reason_parts.pop(0)
                parsed = argparse.Namespace(
                    command="control",
                    job_id=int(parts[1]),
                    action=command,
                    at=at,
                    reason=" ".join(reason_parts) or None,
                )
            else:
                raise ControlApiError("Invalid command. Type 'help' for the supported syntax.")
            execute_command(parsed, client)
        except (ValueError, ControlApiError) as error:
            print(f"Error: {error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate the local ITRP Oracle scheduler service")
    parser.add_argument("--url", help="scheduler API URL (defaults to SCHEDULER_API_URL or localhost:8091)")
    parser.add_argument("--token", help="scheduler API token (defaults to SCHEDULER_API_TOKEN)")
    parser.add_argument(
        "--actor",
        help="audit actor (defaults to SCHEDULER_OPERATOR, USERNAME, or USER)",
    )
    parser.add_argument("--timeout", type=float, default=8, help="HTTP timeout in seconds (default: 8)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("health", help="check whether the local control API is responding")
    subparsers.add_parser("status", help="show a concise operational status")
    subparsers.add_parser("jobs", help="list Schedule Master jobs and their control state")
    snapshot = subparsers.add_parser("snapshot", help="emit the complete scheduler-owned state")
    snapshot.add_argument("--output", help="write JSON atomically to this file instead of stdout")
    control = subparsers.add_parser("control", help="record a safe job-control intent")
    control.add_argument("job_id", type=int)
    control.add_argument("action", choices=CONTROL_ACTIONS)
    control.add_argument("--at", help="ISO evaluation date/time required by set_override")
    control.add_argument("--reason", help="human reason recorded with the control action")
    configure = subparsers.add_parser(
        "configure",
        help="change an existing job's active state or RUN_BY window",
    )
    configure.add_argument("job_id", type=_positive_job_id)
    state = configure.add_mutually_exclusive_group()
    state.add_argument("--active", dest="is_active", action="store_const", const=True)
    state.add_argument("--inactive", dest="is_active", action="store_const", const=False)
    configure.set_defaults(is_active=_CONFIG_UNSET)
    configure.add_argument("--from", dest="from_time", help="execution window start, 24-hour HH:MM")
    configure.add_argument("--to", dest="to_time", help="execution window end, 24-hour HH:MM")
    configure.add_argument(
        "--clear-window",
        action="store_true",
        help="remove RUN_CONFIG.RUN_BY; cannot be combined with --from/--to",
    )
    configure.add_argument("--reason", help="human reason recorded with the configuration change")
    watch = subparsers.add_parser("watch", help="continuously refresh the operator status")
    watch.add_argument("--interval", type=float, default=5, help="refresh interval in seconds (default: 5)")
    service = subparsers.add_parser("service", help="start or stop future scheduler cycles safely")
    service.add_argument("action", choices=("start", "stop"))
    service.add_argument("--reason", help="human reason recorded with the service action")
    subparsers.add_parser("cycle", help="run one locked worker cycle now, then exit")
    subparsers.add_parser("shell", help="start the interactive scheduler console")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    client = SchedulerControlClient(args.url, args.token, args.timeout, args.actor)
    try:
        if args.command == "shell":
            return _interactive_shell(client)
        return execute_command(args, client)
    except ControlApiError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
