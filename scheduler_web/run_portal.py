"""Run the portal and its separate scheduler with one development command.

The default evaluates real schedules in monitoring mode. --execute explicitly
starts the worker with Oracle execution enabled. Neither mode seeds test data.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from dotenv import dotenv_values, load_dotenv
from project_paths import project_path
from portal_revision import current_portal_revision


BASE_DIR = Path(__file__).resolve().parent


def scheduler_snapshot(url, token=""):
    headers = {"X-Scheduler-Token": token} if token else {}
    try:
        with urlopen(Request(url.rstrip("/") + "/v1/operations/snapshot", headers=headers), timeout=3) as response:
            snapshot = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"Scheduler API returned HTTP {exc.code}; check the matching API token.") from exc
    except (URLError, TimeoutError, ConnectionError):
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("The configured API address did not return a scheduler JSON response.") from exc
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("meta"), dict) or snapshot["meta"].get("service") != "scheduler-control-api":
        raise RuntimeError("The configured API address belongs to a different service.")
    return snapshot


def worker_command(project, *, execute=False, calendar_source="oracle"):
    return [sys.executable, "-u", str(project / "main.py"), "--calendar-source", calendar_source] + ([] if execute else ["--monitor"])


def selected_master_source(explicit, worker_settings):
    source = explicit or os.getenv("SCHEDULER_MASTER_SOURCE") or worker_settings.get("SCHEDULER_MASTER_SOURCE") or "oracle"
    source = source.strip().lower()
    if source not in {"oracle", "file"}:
        raise RuntimeError("SCHEDULER_MASTER_SOURCE must be oracle or file.")
    return source


def portal_health(url, expected_api_url):
    """Reuse only a current portal with the expected scheduler connection."""
    try:
        with urlopen(url.rstrip("/") + "/healthz/", timeout=3) as response:
            health = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(
            "The portal port is occupied by an incompatible or older service. "
            "Stop that service or choose another --port."
        ) from exc
    except (URLError, TimeoutError, ConnectionError):
        return None
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("The portal port is occupied by a different service. Choose another --port.") from exc
    # Recognize older installations so the error explains how to update them.
    if not isinstance(health, dict) or health.get("service") not in {"itrp-scheduler-portal", "sbi-scheduler-portal"}:
        raise RuntimeError("The portal port is occupied by a different service. Choose another --port.")
    if str(health.get("scheduler_api_base_url", "")).rstrip("/") != expected_api_url.rstrip("/"):
        raise RuntimeError("The existing portal uses another scheduler API. Stop it or choose another --port.")
    if health.get("portal_revision") != current_portal_revision():
        raise RuntimeError(
            "The portal already running on this port has older project code. "
            "Stop its original portal/launcher window with Ctrl+C, then run run_portal.bat again. "
            "A separately running scheduler worker can stay running. "
            "No existing process was stopped by this launcher."
        )
    if health.get("scheduler_connected") is False:
        raise RuntimeError("The existing portal cannot authenticate or connect to the scheduler. Restart it with the matching API settings.")
    return health


def wait_for_ready(process, probe, service, *, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{service} exited with code {process.returncode}. Check the logs folder.")
        result = probe()
        if result is not None:
            return result
        time.sleep(0.5)
    raise RuntimeError(f"{service} did not become ready. Check the logs folder.")


def stop_children(children):
    # Own only the processes launched here; leave pre-existing services alone.
    for process in reversed(children):
        if process.poll() is None:
            if os.name == "nt":
                # A Windows venv redirector may own a second Python process.
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
            else:
                process.terminate()
    for process in reversed(children):
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="enable real Oracle procedure execution in the worker")
    parser.add_argument("--calendar-source", choices=["oracle", "file"], default="oracle", help="authoritative calendar source (default: oracle)")
    parser.add_argument("--master-source", choices=["oracle", "file"], help="schedule source (default: configured source or Oracle)")
    parser.add_argument("--port", type=int, default=8010, help="local portal port (default: 8010)")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")

    load_dotenv(BASE_DIR / ".env")
    project = project_path(os.getenv("SCHEDULER_PROJECT_PATH"), BASE_DIR.parent / "scheduler_project")
    if not (project / "main.py").is_file():
        raise RuntimeError("SCHEDULER_PROJECT_PATH must point to the existing scheduler_project folder.")
    # Only the API token is shared with Django. Oracle settings stay in the worker.
    worker_settings = dotenv_values(project / ".env")
    master_source = selected_master_source(args.master_source, worker_settings)
    token = os.getenv("SCHEDULER_API_TOKEN", worker_settings.get("SCHEDULER_API_TOKEN") or "")
    api_url = os.getenv("SCHEDULER_API_BASE_URL", "http://127.0.0.1:8091").rstrip("/")
    parsed = urlparse(api_url)
    # This launcher supervises a separate delivery process. Standalone
    # manage.py runserver owns its own delivery loop instead.
    child_env = {**os.environ, "SCHEDULER_API_TOKEN": token, "SCHEDULER_API_BASE_URL": api_url,
                 "SCHEDULER_PORTAL_AUDIT_AUTOSTART": "0"}
    children = []
    handles = []
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(exist_ok=True)

    def start(command, cwd, log_name, env):
        handle = (log_dir / log_name).open("a", encoding="utf-8")
        handles.append(handle)
        process = subprocess.Popen(command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, creationflags=flags)
        children.append(process)
        return process

    try:
        snapshot = scheduler_snapshot(api_url, token)
        if snapshot is None:
            if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
                raise RuntimeError("The remote scheduler is unavailable; start it on its configured host.")
            worker_env = {**child_env, "SCHEDULER_API_HOST": parsed.hostname,
                          "SCHEDULER_API_PORT": str(parsed.port or 80), "SCHEDULER_API_ENABLED": "1",
                          "SCHEDULER_MASTER_SOURCE": master_source}
            print("Starting scheduler: " + ("Oracle execution enabled." if args.execute else "monitoring mode; Oracle execution disabled."), flush=True)
            worker = start(worker_command(project, execute=args.execute, calendar_source=args.calendar_source), project, "worker-launch.log", worker_env)
            snapshot = wait_for_ready(worker, lambda: scheduler_snapshot(api_url, token), "Scheduler")
        elif args.execute and snapshot.get("meta", {}).get("execution_enabled") is False:
            raise RuntimeError("The existing scheduler is in monitoring mode. Stop that process before starting with --execute.")

        actual_source = snapshot.get("meta", {}).get("master_source")
        if actual_source != master_source:
            raise RuntimeError("The running scheduler uses another or an older schedule source. Restart it with the updated launcher.")
        print(f"Scheduler connected: {len(snapshot.get('schedule_master', []))} configured schedules; source: {actual_source}.", flush=True)
        if snapshot.get("meta", {}).get("execution_enabled") is False:
            print("Monitoring mode is active. Queue controls are saved; Oracle procedures are not executed.", flush=True)
        else:
            print("Connected worker has Oracle execution enabled.", flush=True)

        portal_url = f"http://127.0.0.1:{args.port}"
        existing_portal = portal_health(portal_url, api_url)
        if existing_portal:
            print("Reusing the current portal and its audit delivery service; no duplicate services were started.", flush=True)
        else:
            subprocess.run([sys.executable, "manage.py", "check"], cwd=BASE_DIR, env=child_env, check=True)
            subprocess.run([sys.executable, "manage.py", "migrate", "--noinput"], cwd=BASE_DIR, env=child_env, check=True)
            # A separate supervised process owns network delivery. Pending events
            # survive worker/Oracle outages and launcher restarts in the portal DB.
            start([sys.executable, "-u", "manage.py", "deliver_audit_events", "--loop"],
                  BASE_DIR, "audit-delivery.log", child_env)
            # Retain Django's reloader so later route/app/template edits become
            # visible without silently keeping an old portal in memory.
            portal = start([sys.executable, "-u", "manage.py", "runserver", f"127.0.0.1:{args.port}"],
                           BASE_DIR, "portal-launch.log", child_env)
            wait_for_ready(portal, lambda: portal_health(portal_url, api_url), "Portal")
        print(f"Open {portal_url}/scheduler/day/", flush=True)
        if children:
            print("Keep this launcher open. Press Ctrl+C to stop services it started.", flush=True)
        while children:
            for process in children:
                if process.poll() is not None:
                    raise RuntimeError(f"A launched service stopped (exit {process.returncode}). Check the logs folder.")
            time.sleep(1)
        return 0
    except KeyboardInterrupt:
        print("\nStopping the services started by this launcher.", flush=True)
        return 0
    finally:
        stop_children(children)
        for handle in handles:
            handle.close()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        raise SystemExit(1)
