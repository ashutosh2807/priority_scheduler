# Running the ITRP scheduler portal

Start [run_portal.bat](../run_portal.bat) from **django_app**, or [start_portal.bat](start_portal.bat) inside **scheduler_web**. Both use the same combined launcher to start the Django portal and separate scheduler together. Open [Daily tasks](http://127.0.0.1:8010/scheduler/day/) and sign in with your existing account. Normal startup needs no separate `main.py` command or second application window.

The project root is the parent `django_app` folder; it can be moved to another approved path. Keep these folders together:

| Location | Purpose |
| --- | --- |
| [scheduler_web](.) | Django UI, accounts, leave, confirmation assignments, guides and human work logs. |
| [scheduler_project](../scheduler_project) | Worker, Oracle access, DATEMAST rules, eligibility, queue and procedure execution. |
| `.scheduler-venv` | Shared Python environment used by the combined launcher. |
| [scheduler_web/run_portal.py](run_portal.py) | Combined launcher called by `start_portal.bat`. |
| [scheduler_project/main.py](../scheduler_project/main.py) | Standalone worker entry point. Its default enables execution; `--monitor` disables procedure execution. |

## Which files are relevant

| Path below `django_app` | What it does | Run it yourself? |
| --- | --- | --- |
| `run_portal.bat` or `scheduler_web/start_portal.bat` | Starts both services through the combined launcher. | **Yes: choose either one.** |
| `scheduler_web/run_portal.py` | Finds the sibling worker by path, selects its configuration, starts services and checks their connection. | Only as the Python alternative to the batch launcher. |
| `scheduler_web/manage.py` | Django administration, migrations and tests. | For maintenance commands, not separate normal startup. |
| `scheduler_web/apps/` | Accounts, dashboard, leave, task controls, confirmation assignments and audit. | No; loaded by Django. |
| `scheduler_web/templates/` and `static/` | Page templates, styling and browser behaviour. | No. |
| `scheduler_web/config/` | Django configuration and URL routing. | No. |
| `scheduler_project/main.py` and `control_api.py` | Worker lifecycle and the local API used by the portal. | The combined launcher starts these. |
| `scheduler_project/scheduler/` and `scheduler_queue/` | Dates, holidays, eligibility, retry policy and priority ordering. | No; loaded by the worker. |
| `scheduler_project/execution/`, `repositories/`, `database/`, `models/`, `control/` | Procedure calls, storage, records and worker controls. | No; loaded by the worker. |
| `scheduler_project/import_schedule_master.py` | Optional migration of a JSON schedule list into Oracle. | Only for an intentional import, as described below. |
| Both `requirements.txt` files, migrations and tests | Dependencies, database upgrades and regression checks. | Keep them; setup/test commands use them. |

Keep both `.env` configurations, both SQLite databases (including any `-wal`/`-shm` companions while running), `scheduler_project/file_repository`, import `backups`, and the shared environment. They contain configuration or saved state. Logs are useful for diagnosing operations. Temporary preview/staged-code files in `.qa`, old `output.txt` dumps and the unreferenced `execution_manager.txt` draft have been removed from the delivery.

The connection is **browser → Django portal (:8010) → worker API (:8091) → Oracle**. The applications are separate Python processes, not separate copies of the scheduling logic. The combined launcher resolves the worker as a sibling folder; keep the existing folder names and relative layout when moving the project.

Recreate the virtual environment on a different machine. For office `cx_Oracle`, different credentials, isolated office state, required Oracle schema and staff administration, follow [OFFICE_DEPLOYMENT.md](OFFICE_DEPLOYMENT.md). The `.env.office.example` files provide a separate office configuration without overwriting the current home setup. `start_portal.bat --check` tests the configured Oracle connection and readable schema without starting jobs.

## Start from either folder

From **django_app**, use either command:

```bat
run_portal.bat
```

```powershell
& '.\.scheduler-venv\Scripts\python.exe' '.\scheduler_web\run_portal.py'
```

From **scheduler_web**, use either command:

```bat
start_portal.bat
```

```powershell
& '..\.scheduler-venv\Scripts\python.exe' '.\run_portal.py'
```

In PowerShell, prefix a batch file in the current directory with `& '.\'`, for example `& '.\start_portal.bat'`. The launcher checks Django, applies pending portal migrations, starts missing services and reuses compatible services already running.

The top-level [run_portal.bat](../run_portal.bat) delegates to `scheduler_web/start_portal.bat` and passes through options such as `--execute` and `--port`. Both default to portal port **8010** and worker API port **8091**.

The sidebar's **Applications** section, above **Scheduler**, contains **SULOG**, **ELOADLOG** and **MAILER**. Their pages are `/sulog/`, `/eloadlog/` and `/mailer/` on the same portal; no separate startup command is needed.

The launcher verifies that an existing portal has loaded the current application code. If it reports **older project code**, stop the original portal/launcher window with **Ctrl+C**, then run the batch file again. A combined launcher also stops any worker it started; a separately started worker can remain running. New portal launches use Django's automatic reload, so route and application changes reload Django without restarting the worker. Starting the batch file again while a current portal is running reuses it without creating another audit delivery process.

## Monitoring and execution

The combined launcher defaults to **monitoring mode**. It evaluates real schedules, saves their pending/ready state and writes operational status and audit events to Oracle. It does not call report procedures. Schedule edits, operator controls, confirmation actions and guide changes are real saved changes.

Its default calendar source is Oracle. The schedule source follows `--master-source`, the process environment or the worker's `.env`, then defaults to Oracle. The startup message reports the actual source. To explicitly select Oracle for both inputs:

```bat
start_portal.bat --master-source oracle --calendar-source oracle
```

To enable actual Oracle procedure execution, stop the monitoring launcher first, then run from **scheduler_web**:

```bat
start_portal.bat --execute --master-source oracle --calendar-source oracle
```

Equivalent PowerShell commands are:

```powershell
# From django_app
& '.\.scheduler-venv\Scripts\python.exe' '.\scheduler_web\run_portal.py' --execute --master-source oracle --calendar-source oracle

# From scheduler_web
& '..\.scheduler-venv\Scripts\python.exe' '.\run_portal.py' --execute --master-source oracle --calendar-source oracle
```

The launcher does not switch an existing monitoring worker into execution mode. Stop that worker before restarting with `--execute`. If a worker already has execution enabled, connecting through the default launcher preserves that running mode; check the portal's mode notice.

The standalone [run_scheduler_service.bat](../scheduler_project/run_scheduler_service.bat) starts a persistent worker and enables execution unless passed `--monitor`. [run_scheduler_cycle.bat](../scheduler_project/run_scheduler_cycle.bat) performs one execution cycle and exits; it cannot provide continuous UI controls. Use one worker arrangement for this installation.

## Configuration and saved data

Oracle credentials and worker settings belong in `scheduler_project/.env`, using [its example](../scheduler_project/.env.example). Portal settings belong in `scheduler_web/.env`, using [its example](.env.example). Keep Oracle credentials out of the portal configuration. Both processes use the same `SCHEDULER_API_TOKEN`; the portal's default API address is `http://127.0.0.1:8091`.

Relevant worker settings are `SCHEDULER_MASTER_SOURCE`, `SCHEDULER_MASTER_TABLE` (default `SCHEDULE_EXTRACT_MASTER`), `SCHEDULER_MASTER_OPTIONAL_COLUMNS`, `SCHEDULER_CALENDAR_SOURCE`, `SCHEDULER_DATEMAST_TABLE` and `SCHEDULER_DATEMAST_DATE_COLUMN`. For a master table containing `TIME_FLAG` and `CONFIRMATION`, configure those optional columns. The code also supports `CONFIRMATION_NEEDED` where that is the actual column name.

| Data | Where it persists |
| --- | --- |
| Schedule definitions in Oracle mode | Oracle `SCHEDULE_EXTRACT_MASTER`, or the configured master table. UI add/edit/delete actions are committed there by the worker. `file_repository/Schedule_Master.json` is a refreshed local snapshot. |
| Schedule definitions in file mode | `scheduler_project/file_repository/Schedule_Master.json`. File mode does not update Oracle master definitions. |
| Bank working dates | Read from Oracle DATEMAST in Oracle calendar mode. Successful reads are cached for the local day; a SUPERUSER can request a same-day refresh from Calendar. Failed reads retry with backoff. Monitoring keeps calendar refreshes in memory; execution mode refreshes the local calendar snapshots. |
| Queue, pending occurrences, execution history and worker controls/audit | `scheduler_project/scheduler.db`. These are worker-owned local records. |
| Current operational status in Oracle | `SCHEDULE_EXTG`: one current row per worker/occurrence, with status, planned date, attempts, results and error details. Local worker state remains responsible for scheduling. |
| Complete operational audit in Oracle | `SCHEDULE_EXTG_LOG`: immutable events for execution attempts, eligibility/status changes, queue and schedule controls, definition changes and portal actions. Actor, timestamp, reason and full details are retained. |
| Durable delivery | Worker `scheduler.db` holds the Oracle outbox; portal `db.sqlite3` holds its audit delivery outbox. Failed delivery retries automatically. These databases must be retained even though logs are also stored in Oracle. |
| Legacy status export | `file_repository/Schedule_extg.json` remains a compatibility export for execution mode. The durable Oracle logger replaces the old best-effort mirror. |
| Accounts, leave, named confirmation owners/cover, guides, checklists and portal audit | `scheduler_web/db.sqlite3`. The human checklist is separate from a successful Oracle execution. |

Normal UI operation requires no manual JSON edits. [OPERATIONS.md](OPERATIONS.md) explains schedule management, confirmation routing and the fiscal calendar in more detail.

## Oracle status and logging

This installation uses `SCHEDULER_ORACLE_LOGGING=1`, `SCHEDULER_EXTG_TABLE=SCHEDULE_EXTG` and `SCHEDULER_EXTG_LOG_TABLE=SCHEDULE_EXTG_LOG`. Keep `SCHEDULER_EXTG_MIRROR=0`; it is the older implementation. Logging is independent of `--execute`, so monitoring still records real operations.

The combined launcher starts an additional `deliver_audit_events --loop` process for portal audit delivery. The worker then forwards accepted events to Oracle from a separate delivery thread. A portal acknowledgement means the worker has saved the event durably; the Oracle backlog shows whether delivery has finished. The runtime notice and Audit page show both backlogs and the latest delivery/error. Replaying after an interrupted acknowledgement does not duplicate the Oracle event, and older replayed states cannot overwrite a newer occurrence state.

`SCHEDULE_EXTG.ID` identifies a status row. Use `JOB_ID` for the schedule definition and `OCCURRENCE_KEY`/`REPORT_DATE` for the report occurrence. Every attempt is retained in `SCHEDULE_EXTG_LOG`; the current row changes as work progresses. Portal checklist completion is logged separately and does not mark a report execution successful. Historical imports contain only the history that was actually retained locally.

The one-time upgrade utility is `scheduler_project/rebuild_oracle_logging.py`, backed by `sql/02_oracle_logging_schema.sql`. It defaults to a read-only preview. Its `--apply` mode backs up the old table and verifies the replacement before dropping it. **The upgrade is already applied for this installation; do not drop the table during normal startup.** Keep the generated Oracle backup table and `scheduler_project/backups/oracle-logging-*` files.

Example read-only checks in Oracle:

```sql
SELECT job_id, name, report_date, planned_execution_date, status,
       attempt_no, records_loaded, updated_at
FROM schedule_extg
ORDER BY updated_at DESC;

SELECT event_type, job_id, actor, occurred_at, status, reason
FROM schedule_extg_log
ORDER BY occurred_at DESC, event_seq DESC;
```

`Schedule_Master.json` is a local snapshot in Oracle mode. Add/change/delete schedules in the UI; the worker commits definitions to `SCHEDULE_EXTRACT_MASTER` and refreshes that snapshot. Do not edit the snapshot to change live schedules.

Schedule Master refresh is configured to **one hour** (`SCHEDULER_MASTER_REFRESH_SECONDS=3600`). The worker reads Oracle on startup, then uses cached definitions until the interval expires; the next schedule read refreshes them. UI add/edit/delete actions still refresh immediately after their Oracle commit. Changes made directly in Oracle are picked up at the next scheduled refresh. DATEMAST keeps its separate daily refresh and **Calendar → Refresh from Oracle** action for a SUPERUSER.

DATEMAST reads only dates from the **31 March boundary before the previous financial year through today**, inclusive. For example, on 12 September 2026 the range is **31 March 2025–12 September 2026**. The start moves to 31 March 2026 on 1 April 2027. Oracle applies the date bounds in its query, so older and future entries are not downloaded. Today's entry, if present, still does not advance the T−1 publication watermark. Calendar notices show the loaded range; older dates use provisional bank-calendar rules rather than being declared holidays because they were excluded. The local DATEMAST snapshot stores dates with their coverage bounds so those rules also survive a restart.

## Optional one-time JSON import

[import_schedule_master.py](../scheduler_project/import_schedule_master.py) reconciles an existing JSON schedule list with Oracle when migrating master data. It is not a startup step. Routine schedule changes belong in the portal.

From **scheduler_project**, preview the proposed inserts and updates first:

```powershell
& '..\.scheduler-venv\Scripts\python.exe' '.\import_schedule_master.py'
```

The default is a dry run using `file_repository/Schedule_Master.json` and the worker's `.env`. After reviewing the preview, apply that list in one Oracle transaction:

```powershell
& '..\.scheduler-venv\Scripts\python.exe' '.\import_schedule_master.py' --apply
```

In Command Prompt, use `..\.scheduler-venv\Scripts\python.exe import_schedule_master.py`, adding `--apply` only for the commit. `--source`, `--env-file` and `--backup-dir` select alternative paths. Applying writes a dated backup under `scheduler_project/backups` by default before changing Oracle; retain these backups. The importer preserves unlisted Oracle schedules, `LAST_RUN` and execution history. An unchanged result means the source already matches Oracle, so no additional insertion is needed.

## Where to look in the UI

| Page | Address |
| --- | --- |
| Calendar-first dashboard | [Dashboard](http://127.0.0.1:8010/) |
| Month calendar and Oracle refresh | [Calendar](http://127.0.0.1:8010/calendar/) |
| Task status for a selected day | [Daily tasks](http://127.0.0.1:8010/scheduler/day/) |
| Add, edit or delete definitions | [Schedules](http://127.0.0.1:8010/scheduler/jobs/) |
| Waiting for named confirmation | [Confirmations](http://127.0.0.1:8010/scheduler/confirmation/) |
| Eligible tasks in execution order | [Live queue](http://127.0.0.1:8010/scheduler/queue/) |
| Help | [Help](http://127.0.0.1:8010/help/) |

An empty live queue does not mean Oracle is disconnected. It contains eligible READY occurrences. A task can instead be inactive, paused, cancelled, completed, waiting for DATEMAST/confirmation/time, outside its permitted holiday rules, or overdue and requiring a manual run. Inspect **Daily tasks**, the task's reason and the source/mode notice. With all-day retries configured, failed work can retry on its intended execution day up to its attempt limit; after that day, another run requires a manual request.

Monitoring evaluates eligibility every 30 seconds by default. Execution cycles default to five minutes. These intervals are independent of the once-per-day DATEMAST refresh and the separately configured master-definition refresh.

## Stop or diagnose startup

Keep the combined launcher open. **Ctrl+C stops only the services it started**; services it reused remain running. During actual execution, use **Stop scheduler** in the portal to prevent future cycles and let the active Oracle call finish before shutting down the worker process.

Check `scheduler_web/logs/worker-launch.log` and `portal-launch.log` for combined-launcher errors, and `scheduler_project/logs/scheduler.log` for worker decisions. The standalone batch service additionally logs to `scheduler_service.log` in the worker's logs folder. [Portal health](http://127.0.0.1:8010/healthz/) and [worker health](http://127.0.0.1:8091/health) identify the respective services.

On a new workstation, run [setup_scheduler_environment.bat](../setup_scheduler_environment.bat) once from `django_app` to create the shared environment, install both requirements files and migrate the portal. Create the first administrator only if needed:

```powershell
& '.\.scheduler-venv\Scripts\python.exe' '.\scheduler_web\manage.py' createsuperuser
```

These startup commands use Django's local development server. Office service deployment settings are described in [OPERATIONS.md](OPERATIONS.md).
