# Background worker with a separately started Django portal

Use this arrangement when Windows Task Scheduler should keep the report scheduler running and you want to open the UI with `python manage.py runserver`.

```text
Windows Task Scheduler
  -> scheduler_project/run_scheduler_service.bat
     -> main.py: persistent worker + control API on 127.0.0.1:8091
        -> Oracle

python manage.py runserver
  -> Django UI on 127.0.0.1:8000
     -> worker API on 127.0.0.1:8091
     -> automatic delivery of saved portal audit events
```

The worker evaluates schedules every five minutes internally. Windows starts it once at startup and leaves it running. Closing the browser or stopping Django does not stop the separately scheduled worker. Oracle credentials stay in `scheduler_project/.env`; Django uses the worker API.

## 1. Prepare the installation once

Keep `scheduler_project` and `scheduler_web` beside each other under your `django_app` folder. On another machine, recreate the Python environment and use that machine's `.env` files. [OFFICE_DEPLOYMENT.md](OFFICE_DEPLOYMENT.md) covers `cx_Oracle`, Oracle Client, isolated office state and the required tables/grants.

The worker batch file selects its interpreter in this order:

1. The full `python.exe` path in the Windows environment variable `SCHEDULER_PYTHON`, if explicitly set. An invalid override stops startup instead of silently selecting a different interpreter.
2. `django_app/.scheduler-venv/Scripts/python.exe`.
3. `scheduler_project/venv/Scripts/python.exe`.
4. An activated virtual environment's `Scripts/python.exe`.

For unattended startup, use a working project environment or configure `SCHEDULER_PYTHON` for the Windows account that runs the task. A virtual environment activated in your interactive terminal is not automatically activated for an unattended Windows task.

In `scheduler_project/.env`, retain the appropriate Oracle credentials/driver and set:

```dotenv
SCHEDULER_API_HOST=127.0.0.1
SCHEDULER_API_PORT=8091
SCHEDULER_API_TOKEN=your-private-control-api-token
SCHEDULER_MASTER_SOURCE=oracle
SCHEDULER_CALENDAR_SOURCE=oracle
```

The service batch file explicitly selects Oracle master/calendar mode. It does not edit the `.env`, import JSON schedules, rebuild tables or start Django. Preserve the office `SCHEDULER_DATA_DIR` and logging settings when using the office profile.

In `scheduler_web/.env`, use the same API token:

```dotenv
SCHEDULER_API_BASE_URL=http://127.0.0.1:8091
SCHEDULER_API_TOKEN=your-private-control-api-token
SCHEDULER_PORTAL_AUDIT_AUTOSTART=1
```

These values must agree between processes. No Oracle username, password or driver setting is required in Django's `.env`. Restart the affected processes after changing their `.env` files. A browser refresh does not reload environment configuration.

From an activated project environment inside `scheduler_web`, run:

```bat
python check_environment.py
python manage.py migrate
```

On a new portal database only, create your administrator with `python manage.py createsuperuser`. The connection check uses SELECT-only Oracle probes and does not run report procedures. A normal Django `runserver` command does not apply migrations automatically.

## 2. Register the worker in Windows Task Scheduler

Choose **Create Task** and name it **ITRP Scheduler Worker**. The name is a label you choose; this does not install a Windows Service Control Manager service.

The following example assumes the project is installed at `D:\ITRP\django_app`. Replace that path with the real installation folder.

| Task Scheduler field | Value |
| --- | --- |
| Name | `ITRP Scheduler Worker` |
| User account | The office account configured to read the project, its `.env`, write its runtime/log directories and load Oracle Client |
| Security option | Run whether user is logged on or not |
| Trigger | At startup; a one-minute delay allows ordinary startup services to initialize |
| Action | Start a program |
| Program/script | `C:\Windows\System32\cmd.exe` (use the system's actual Windows directory if different) |
| Add arguments | `/d /c ""D:\ITRP\django_app\scheduler_project\run_scheduler_service.bat""` |
| Start in | `D:\ITRP\django_app\scheduler_project` (without surrounding quotes) |

Microsoft documents startup triggers and full executable/script paths in [Task Scheduler's create reference](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create).

In **Settings**:

- Allow the task to be run on demand.
- If it is already running, choose **Do not start a new instance**. See [Microsoft's instance policy](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-multipleinstances).
- Uncheck **Stop the task if it runs longer than...**. The default execution limit is 72 hours; a persistent worker needs no time limit. See [Microsoft's execution-time setting](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-executiontimelimit).
- Enable **restart on failure**, for example every minute with 10 attempts. See [Microsoft's restart setting](https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings-restartinterval).

The machine must remain powered on and awake for report processing. Review any task conditions that stop the worker on battery or require idle time, according to the office machine's operating policy.

The batch remains attached to the Python worker, records its output in `scheduler_project/logs/scheduler_service.log`, and returns the worker's exit code. It never waits for a key press, so a startup failure can be seen and retried by Windows. Additional worker logs are under its configured data directory's `logs/` folder.

**The normal batch command enables real Oracle report execution.** For monitoring only, use this arguments field instead:

```text
/d /c ""D:\ITRP\django_app\scheduler_project\run_scheduler_service.bat" --monitor"
```

The batch accepts `--monitor` and `--help`; `--help` displays usage and exits without starting the worker. It rejects one-cycle/API-disabled options because this entry point provides the continuously available service. Do not add a separate repeating `run_scheduler_cycle.bat` task alongside it.

## 3. Start Django whenever you need the UI

Once the Windows worker task is running, open a command prompt in `scheduler_web` and activate the project Python environment:

```bat
..\.scheduler-venv\Scripts\activate
python manage.py runserver
```

Open [http://127.0.0.1:8000/](http://127.0.0.1:8000/) or [Django admin](http://127.0.0.1:8000/admin/). If using an existing approved office interpreter instead of `.scheduler-venv`, activate that environment or invoke its Python directly.

To retain the portal's earlier port of 8010, use:

```bat
python manage.py runserver 127.0.0.1:8010
```

Django reads `SCHEDULER_API_BASE_URL` and connects to the already-running worker automatically when the UI reads status or sends an operator action. Both portal ports use the same worker on port 8091. The health URL, such as `http://127.0.0.1:8000/healthz/`, should report `scheduler_connected: true`.

Standalone `runserver` also starts a small background audit-delivery loop. It checks the **local portal outbox**, sends only pending events to the worker, and stops with Django. It does not evaluate schedules or execute Oracle procedures. Saved events survive a stopped/unavailable worker and retry when it returns. Reloading Django does not create a second persistent worker. The combined `run_portal.py` launcher disables this automatic loop because it already supervises a separate audit-delivery process.

If Django is stopped, scheduling still runs at Windows startup independently. Portal actions require Django to be running. `runserver` remains the project's local development UI server; use the office's approved web-server deployment for a shared production/LAN portal. Other Django server entry points need their own supervised `manage.py deliver_audit_events --loop` process.

## 4. Switch from the existing combined launcher

The previous `start_portal.bat --execute` arrangement already owns a worker, portal and delivery process. Before using this new split arrangement:

1. In the portal, stop future scheduler cycles and let any currently running Oracle task finish.
2. Stop the old combined launcher. Keep the existing databases and task state.
3. Run **ITRP Scheduler Worker** from Windows Task Scheduler.
4. Start Django with `python manage.py runserver`.
5. Check the portal connection and re-enable scheduler cycles if you paused them in step 1. That setting persists across restarts.

Avoid running both launcher arrangements at the same time. The single-worker lock prevents duplicate Oracle execution, but a second launcher cannot take ownership of a worker already running elsewhere. Windows showing the task as **Running** continuously is expected; “worker already running” means another process already owns it.

When you run `run_scheduler_service.bat` manually, it prints the service log location. A newly started worker keeps the command occupied until it stops. If the prompt returns, the batch prints its exit code and the latest invocation's output. **“Scheduler worker already running; this trigger was skipped”** means the existing worker continues running and this second launch was skipped. It does not mean the existing worker stopped. Do not remove the worker lock to start another copy.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Windows task exits immediately | Check the displayed exit output and `scheduler_project/logs/scheduler_service.log`. **Worker already running** means another launcher owns the worker; use that instance or follow the switch-over steps above. For other errors, verify the interpreter, permissions and `.env`; run `python check_environment.py` from `scheduler_web` with the same interpreter. |
| UI reports scheduler unavailable | Confirm the Windows worker task is Running, API port is 8091, and both API tokens match. |
| Port 8010 is already occupied | Stop the older combined portal or use plain `runserver` on port 8000. |
| Worker is running but no task executes | Check execution/monitoring mode, the persisted scheduler-enabled control, task eligibility, confirmation and queue state in the UI. |
| Audit events remain pending | Keep Django running with `SCHEDULER_PORTAL_AUDIT_AUTOSTART=1`, check worker connectivity, and review Oracle logging health in the portal. |
| Office database details changed | Stop the worker after active work finishes, update its `.env`, run the connection check and start it again. |

This guide and the batch file are ready to use. No Windows scheduled task is registered automatically by creating these files.
