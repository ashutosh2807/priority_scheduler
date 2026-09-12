# Scheduler Control & Execution Application

## 1. Overview

This project is a standalone Python scheduling and execution engine designed to be controlled and monitored by a Django web application.

The scheduler is intentionally separated from Django.

```text
                    ┌───────────────────────────────┐
                    │       Django Web Application  │
                    │                               │
                    │ Dashboard                     │
                    │ Job Control                   │
                    │ Manual Run                    │
                    │ Pause / Resume                │
                    │ Datetime Override             │
                    │ STAGING / READY Monitoring    │
                    │ Execution History             │
                    └───────────────┬───────────────┘
                                    │
                              HTTP / REST API
                                    │
                                    ▼
                    ┌───────────────────────────────┐
                    │     Scheduler Application     │
                    │                               │
                    │ Schedule Master               │
                    │ Job Control                   │
                    │ Eligibility                   │
                    │ STAGING                       │
                    │ READY                         │
                    │ Priority Queue                │
                    │ Execution Manager             │
                    └───────────────┬───────────────┘
                                    │
                   ┌────────────────┴────────────────┐
                   ▼                                 ▼
            SQLite Application State              Oracle DB
            - job_control                         - Procedures
            - staging_jobs                        - Extract logic
            - ready_jobs
            - execution_jobs
```

The scheduler does not require Django to perform scheduling calculations. Django is a control, monitoring and presentation layer.

## Implemented control API

The calendar and work queue use these current operational contracts:

- `GET /v1/operations/calendar?start_date=2026-09-01&days=30` returns every
  projected/persisted occurrence in the requested range (maximum 62 days),
  grouped day counts, original report dates, execution dates, live status,
  scoped confirmation, and DATEMAST source/availability. Future daily dates
  remain explicitly unresolved until DATEMAST publishes them.
  `planned_execution_date` retains the scheduled day. A late or overnight
  execution also appears on its actual activity days through `calendar_date`
  and `day_context`, at most once per occurrence per day. Currently queued
  carryover work appears in today's list. Active retries show READY while
  retaining FAILED separately in `latest_attempt_status` and `retry_pending`.
  `calendar_days` contains every requested date, including adjacent-month grid
  cells, with `kind` (`WORKING_DAY`, `SAT`, `SUN`, or `HOLIDAY`),
  `is_working_day`, `is_holiday`, `holiday_name`, `day_label`, `source`, `reason`,
  and `is_provisional`, all taken from the scheduler's shared holiday evaluator.
  `is_bank_holiday` identifies a closed second/fourth Saturday.
  `calendar.holiday_source` identifies `oracle`, `local_snapshot`,
  `retained_snapshot`, or `unavailable`; `holiday_count` is the actual number of
  configured holiday dates, including otherwise working Saturdays.
  `holiday_rule_source` identifies the combined DATEMAST/bank rule;
  `holiday_observed_through` is yesterday when inference is available, otherwise
  null, and `holiday_inference_available` states whether absence is authoritative.
- DATEMAST is authoritative through **T-1**. On the 10th the latest published
  working report date can be the 9th, never the 10th. With a successful nonempty
  feed, a past date present in DATEMAST is working (even a special working Sunday)
  and a missing past date is a holiday. Today and future dates remain provisional;
  selecting a future calendar month does not advance this observation cutoff.
  Default bank rules close Sundays and second/fourth Saturdays, while first,
  third and fifth Saturdays work unless explicitly configured as holidays.
  An empty or failed DATEMAST read cannot establish absence-based holidays;
  retained positive dates remain useful and other dates use provisional defaults.
  Closed default weekends keep `SAT`/`SUN` HOLIDAY_RUN override kinds; a holiday
  on a normally working Saturday uses `HOLIDAY`. Historical DATEMAST presence
  overrides these defaults. Weekly schedules use the final working day of the
  Monday-Sunday week. DAILY execution and previews follow the same day policy.
  Overdue SAME_DAY work, manual requests and historical retries also recheck
  the actual execution day before calling Oracle; their original report date
  remains unchanged. A HOLIDAY_RUN permission can allow a closed day.
- The fiscal defaults are fortnightly on the 15th and month end, monthly at
  month end, quarterly on March/June/September/December month end, half-yearly
  on 31 March/30 September, and annually on 31 March. Explicit master rules
  continue to take precedence. Non-SAME_DAY DAILY selects the latest DATEMAST
  date strictly before execution (T+1); periodic boundaries run on the next
  working day. The existing Oracle-compatible T+margin parameter is preserved.
- `POST /v1/queue/reorder` requires the complete ordered `occurrence_keys`
  array and `queue_revision` from snapshot metadata, plus `actor`/`reason`.
  Stale views return HTTP 409. Ordering is durable, audited, and applied to the
  single derived priority heap after eligibility; it never inserts READY work.
- Job confirmation controls accept `occurrence_key` and authorize only that
  pending report date. An omitted key is accepted only when exactly one pending
  occurrence is unambiguous. Snapshot staging/ready rows and calendar rows expose
  `confirmation` and `confirmation_confirmed`; job-wide flags do not carry an
  approval into another day. Historical retry also respects revoked approval.
- While Oracle is executing, its committed RUNNING state remains visible and
  controls remain responsive. Pause/cancel affects future work; an active
  procedure is allowed to finish. SQLite and queue mutations remain serialized.

For the Oracle DATEMAST feed, set `SCHEDULER_CALENDAR_SOURCE=oracle` and the
existing protected Oracle connection settings. Table/column names use
`SCHEDULER_DATEMAST_TABLE` (default `DATEMAST`) and
`SCHEDULER_DATEMAST_DATE_COLUMN` (default `REPORT_DATE`). File mode remains
available for offline snapshots; the API identifies the selected source and
exposes refresh failures instead of inventing working dates.

`main.py` starts the scheduler-owned control API with the worker. It binds to
`127.0.0.1:8091` by default and shares the worker lock for all scheduler state
changes; the lock is released only during the external Oracle procedure call.

- `GET /health` is a loopback health check.
- `GET /v1/operations/snapshot` returns Schedule Master, control state,
  STAGING, READY, derived priority-queue inspection and execution history.
- `POST /v1/jobs/{job_id}/controls/{action}` records an existing control
  intent (`pause`, `resume`, `cancel`, `activate`, `manual_run`, confirmation,
  reset, and evaluation-time actions). It never directly inserts into READY
  or executes Oracle.
- `PATCH /v1/jobs/{job_id}/configuration` changes only an existing job's
  `is_active` state and `RUN_CONFIG.RUN_BY` window. Its JSON body accepts
  `is_active`, `run_by` (`from_time` / `to_time`, or `null` to clear the
  window), `actor`, and `reason`; frequency, margin, package/procedure, and
  calendar policy are never mutable through this API. File mode writes the
  local snapshot atomically. Oracle mode uses bind variables and then refreshes
  the safe scheduler snapshot.

Set `SCHEDULER_API_TOKEN` on the scheduler and Django processes to require an
`X-Scheduler-Token` header. A token is mandatory outside loopback. The
historical `/api/...` routes later in this document are illustrative; Django
uses the versioned `/v1/...` control plane above. There is deliberately no
general API to edit `Schedule_Master.json` or `datemaster.json`; the bounded
job configuration endpoint above is the one approved exception.

---

## Operator launch modes (Windows)

For a populated operations portal without running any bank procedures, use:

```bat
run_scheduler_service.bat --monitor
```

Monitor mode uses the real Schedule Master, DATEMAST, eligibility evaluator,
STAGING/READY tables and single priority heap. It evaluates every 30 seconds
(`SCHEDULER_MONITOR_INTERVAL_SECONDS`) and immediately after operator controls.
It skips procedure execution, historical retries and orphan recovery.
Durable Oracle status and audit logging remains active when configured. Existing service enable/stop controls are preserved.
Health and snapshot metadata expose `mode: "monitor"` and
`execution_enabled: false`, so the UI distinguishes monitoring from execution.

With the launcher's Oracle calendar source, monitor reads DATEMAST directly into memory;
it never rewrites `Schedule_Master.json` or `datemaster.json` during evaluation.
The default calendar uses DATEMAST plus bank rules; no Oracle holiday table is
required or queried. Existing dates in `holiday_master.json` remain available
as additional configured holidays. To read an extra Oracle holiday source,
explicitly set `SCHEDULER_HOLIDAY_TABLE` (and its date column if needed).
Only a failure of that configured source produces a holiday warning in
`calendar.refresh_error`; DATEMAST remains independently available in monitor
mode. `calendar.oracle_holiday_source_configured` exposes whether that optional
source is enabled. Blank/unset means disabled and is a healthy configuration.
`source_latest_report_date` is the raw maximum Oracle marker;
`latest_report_date` is capped strictly before today;
`effective_report_date` is the strict prior report date for today's T+1 work,
so a future Oracle marker cannot become today's report parameter.

The continuous launcher validates Python 3.10 or newer and `python-dotenv`.
It honors an explicit `SCHEDULER_PYTHON` first (and fails if that override is
invalid), then checks `..\.scheduler-venv`, the engine's `venv`, and an activated
`VIRTUAL_ENV`. Every project path is resolved from the launcher's location.

`run_scheduler_service.bat` starts only the continuous worker and its control
API, with Oracle Schedule Master and calendar sources. It accepts `--monitor`
or `--help`; cycle-only and API-disabling flags are rejected. It stays in the
foreground without a pause prompt, writes output/errors to
`logs\scheduler_service.log`, and returns the worker's exit code to Windows.
Use a single **At startup** task with **Do not start a new instance**, no
execution time limit, and restart-on-failure settings. See the portal's
[Windows worker setup](../scheduler_web/WINDOWS_WORKER_SETUP.md) for the task
action and Django connection settings.

The project includes two supported Windows entry points. Choose one operating
model for a scheduler installation; do **not** run both as normal production
workers.

| Need | Entry point | What it does |
|---|---|---|
| Windows Task Scheduler cycle every five minutes | `run_scheduler_cycle.bat` | Starts the application, recovers interrupted work, evaluates and executes one cycle, persists state, then exits. |
| Always-on Django dashboard / operator CLI control API | `run_scheduler_service.bat` | Keeps the scheduler and its local HTTP control API running in the foreground. Register it only at computer startup or under a service manager. |

Both modes take the same named Windows mutex. If an Oracle call from an earlier
cycle is still running, a new five-minute trigger exits successfully with a
`Scheduler worker already running` message; it never creates a second worker
or overlaps an active job. The mutex is released automatically if Windows
terminates the process.

Monitor mode takes that same mutex; stop its process before starting an
execution worker. Omitting `--monitor` explicitly selects normal execution
behavior in the service launcher.

### Five-minute Task Scheduler setup

1. Verify the project virtual environment with
   `venv\Scripts\python.exe --version`, or ensure `py -3` starts the approved
   office Python installation.
2. In **Task Scheduler**, create a task whose action is:
   `C:\...\scheduler_project\run_scheduler_cycle.bat`.
3. Add a daily trigger and choose **Repeat task every: 5 minutes** for the
   required duration (normally *Indefinitely*).
4. In task settings, select **If the task is already running: Do not start a
   new instance**. The worker mutex remains an independent protection if this
   setting is changed later.
5. Inspect `logs\task_scheduler.log` and `logs\scheduler.log` after the first
   run. A scheduler cycle can succeed even when a business procedure fails;
   procedure failures are intentionally persisted as retryable scheduler state,
   not hidden by Task Scheduler retries.

The batch mode does not keep a control API open between cycles. Use the
continuous service mode whenever the Django UI or `schedulerctl.bat` must be
able to read live state and record controls at any time. Do not schedule
`run_scheduler_service.bat` every five minutes; start it once at boot or use a
Windows service wrapper approved by infrastructure.

The continuous service preserves `SCHEDULER_INTERVAL_SECONDS` from
`config/settings.py` (currently 300 seconds). The five-minute Task Scheduler
cadence is intentionally an external deployment choice and does not change the
engine's configured service interval.

### Operator CLI

`schedulerctl.bat` is a safe command-line counterpart to the dashboard. It
only calls the scheduler-owned HTTP API and never writes SQLite state, READY
rows, or Schedule Master directly.

```bat
schedulerctl.bat health
schedulerctl.bat status
schedulerctl.bat jobs
schedulerctl.bat control 17 pause --reason "Month-end reconciliation"
schedulerctl.bat control 17 manual_run
schedulerctl.bat control 17 set_override --at 2026-09-11T10:30:00
schedulerctl.bat configure 17 --inactive --reason "Temporary control hold"
schedulerctl.bat configure 17 --active --from 22:00 --to 06:00 --reason "Approved overnight window"
schedulerctl.bat configure 17 --clear-window --reason "Return to unrestricted operation"
schedulerctl.bat service stop --reason "Planned maintenance"
schedulerctl.bat service start --reason "Maintenance complete"
schedulerctl.bat snapshot --output C:\Temp\scheduler-snapshot.json
schedulerctl.bat shell
```

`manual_run`, pause/resume, confirmation, override, and configuration actions
are recorded with the CLI actor and reason. The scheduler service applies
control intents during its next cycle; a CLI action never runs a stored
procedure directly. `configure` only permits `--active` / `--inactive` and a
complete `--from HH:MM --to HH:MM` RUN_BY window (or `--clear-window`); it
cannot edit the package, procedure, frequency, or margin. `schedulerctl.bat
cycle` is available for an approved operator smoke-run and uses the exact same
single-instance guard as Task Scheduler.

`schedulerctl.bat service stop` pauses **future** cycles but does not interrupt
an Oracle procedure that is already executing. `service start` re-enables
normal cycles. Both actions are recorded in the scheduler audit trail.

Every CLI control request includes an audit actor. It defaults to
`SCHEDULER_OPERATOR`, then the Windows `USERNAME` / `USER` environment value;
use `schedulerctl.bat --actor "employee-id" control ... --reason "..."` when
the signed-in operator must be recorded explicitly.

Configure the client with `SCHEDULER_API_URL` and, where configured,
`SCHEDULER_API_TOKEN`. Keep Oracle credentials and API tokens in the protected
`.env` / service account environment; neither batch file prints or embeds a
credential.

---

## 2. Core Design

The application follows this lifecycle:

```text
Schedule_Master.json
        ↓
      Scheduler
        ↓
     STAGING
        ↓
 Eligibility checks
        ↓
       READY
        ↓
 Priority Queue
        ↓
 ExecutionManager
        ↓
   SQLite execution_jobs
        ↓
      Oracle
        ↓
Schedule_extg.json
```

Important persistence rules:

- `STAGING` is persistent.
- `READY` is persistent source-of-truth state.
- The Python priority heap is derived state and can be rebuilt.
- A staged occurrence keeps its business context across scheduler cycles.
- `report_date` is the business/report date passed to Oracle.
- `execution_date` is the actual date on which execution is allowed.
- `t_date` is the DATEMAST T date associated with the occurrence.
- `target_date` is the fixed T + margin target for the occurrence.
- Manual execution is a one-shot control request.
- A manual execution does not replace the scheduled occurrence.
- Scheduled duplicate protection is based on `job_id + report_date`.
- Each three-minute worker tick selects at most one highest-priority READY
  occurrence. Remaining READY records stay persistent, making the real queue
  order observable until the next priority selection.

---

## 3. Date Semantics

The project deliberately separates five dates.

| Field | Meaning |
|---|---|
| `occurrence_date` | Scheduled business occurrence |
| `execution_date` | Date on which this occurrence is allowed to execute |
| `t_date` | DATEMAST T date used for the occurrence |
| `report_date` | Business/report date passed to Oracle |
| `target_date` | T + configured margin |

Example:

```text
Occurrence / report date = 15-Sep
T + 3 target             = 18-Sep
16-Sep                   = holiday
17-Sep                   = next working execution date
18-Sep                   = threshold reached
```

The scheduler must not replace `report_date` with the later execution date.

For `SAME_DAY=0`, the execution date is the next working day strictly after the occurrence.

---

## 4. Supported Frequencies

The current frequency evaluator supports:

- `DAILY`
- `WEEKLY`
- `FORTNIGHTLY`
- `MONTHLY`
- `QUARTERLY`
- `HALF-YEARLY`
- `ANNUALLY`

Current default conventions include:

```text
WEEKLY        → Final working day of the Monday-Sunday week
FORTNIGHTLY   → 15th and last calendar day of month
MONTHLY       → Last calendar day
QUARTERLY     → Month end of Mar/Jun/Sep/Dec
HALF-YEARLY   → 31-Mar and 30-Sep
ANNUALLY      → 31-Mar
```

---

## 5. Scheduler States

### Control states

```text
ACTIVE
PAUSED
CANCELLED
```

### Scheduling states

```text
STAGING
WAITING_CONFIRMATION
WAITING_TIME
WAITING_DATEMAST
WAITING_EXECUTION_DATE
READY
```

### Execution states

```text
PENDING
RUNNING
SUCCESS
FAILED
```

---

## 6. SQLite Persistence

SQLite stores scheduler/application state.

Current important tables:

```text
job_control
staging_jobs
ready_jobs
execution_jobs
```

`job_control` contains:

```text
job_id
control_status
manual_run
confirmation
override_datetime
updated_at
```

The SQLite database is intended for application state and scheduler monitoring. Oracle remains the execution/source system.

---

## 7. Job Control

Job controls are intentionally separate from STAGING and READY.

The Django application should modify controls rather than manipulating the priority heap directly.

Available control operations:

```text
pause(job_id)
resume(job_id)

request_manual_run(job_id)
clear_manual_run(job_id)

confirm(job_id)
clear_confirmation(job_id)

set_override_datetime(job_id, datetime)
clear_override_datetime(job_id)

cancel(job_id)
activate(job_id)

reset(job_id)
```

The repository already exposes corresponding control operations.

---

# 8. Application API

## 8.1 Purpose

Django should communicate with the scheduler through an HTTP API.

Django must not:

- directly modify the scheduler heap
- directly call Oracle procedures
- directly modify `staging_jobs`
- directly modify `ready_jobs`
- calculate scheduling eligibility itself

The scheduler application remains authoritative for scheduling and execution.

Django should perform:

```text
GET  → monitoring/read operations
POST → control/action operations
```

---

## 8.2 Proposed API Base URL

Development:

```text
http://127.0.0.1:8001/api
```

Production example:

```text
http://scheduler-server:8001/api
```

The exact host and port are deployment configuration.

The scheduler API and Django application are separate services/processes.

---

# 9. API Endpoints

## 9.1 Health Check

```http
GET /api/health
```

Response:

```json
{
  "status": "OK",
  "service": "scheduler",
  "timestamp": "2026-09-11T12:00:00"
}
```

Purpose:

- Check that the scheduler API process is alive.
- Used by Django and infrastructure monitoring.

---

## 9.2 Application Status

```http
GET /api/status
```

Example response:

```json
{
  "service": "scheduler",
  "status": "RUNNING",
  "scheduler_cycle": "2026-09-11T12:00:00",
  "scheduler_interval_seconds": 180,
  "total_jobs": 37,
  "staging_count": 31,
  "ready_count": 6,
  "running_count": 0
}
```

---

# 10. Job APIs

## 10.1 List Jobs

```http
GET /api/jobs
```

Example:

```json
{
  "jobs": [
    {
      "id": 2,
      "name": "FTD_EXTRACT",
      "package_name": "SCHEDULE_EXTRACTS.FTD_EXTRACT",
      "same_day": 0,
      "margin": "T",
      "is_active": 1
    }
  ]
}
```

Django can use this for the Jobs screen.

---

## 10.2 Get One Job

```http
GET /api/jobs/{job_id}
```

Example:

```json
{
  "id": 2,
  "name": "FTD_EXTRACT",
  "package_name": "SCHEDULE_EXTRACTS.FTD_EXTRACT",
  "same_day": 0,
  "margin": "T",
  "is_active": 1,
  "control": {
    "control_status": "ACTIVE",
    "manual_run": 0,
    "confirmation": 0,
    "override_datetime": null
  }
}
```

---

# 11. Control APIs

## 11.1 Pause

```http
POST /api/jobs/{job_id}/pause
```

Request body:

```json
{}
```

Response:

```json
{
  "job_id": 2,
  "control_status": "PAUSED"
}
```

---

## 11.2 Resume

```http
POST /api/jobs/{job_id}/resume
```

Response:

```json
{
  "job_id": 2,
  "control_status": "ACTIVE"
}
```

---

## 11.3 Cancel

```http
POST /api/jobs/{job_id}/cancel
```

Response:

```json
{
  "job_id": 2,
  "control_status": "CANCELLED"
}
```

---

## 11.4 Activate

```http
POST /api/jobs/{job_id}/activate
```

Response:

```json
{
  "job_id": 2,
  "control_status": "ACTIVE"
}
```

---

# 12. Manual Run API

## 12.1 Request Manual Run

```http
POST /api/jobs/{job_id}/manual-run
```

Optional body:

```json
{}
```

Response:

```json
{
  "job_id": 2,
  "manual_run": 1,
  "message": "Manual execution requested."
}
```

Important:

This endpoint only creates a control request.

It does not directly execute Oracle.

The next scheduler cycle processes the request.

---

# 13. Manual Run with Datetime Override

The recommended Django sequence is:

### Set override

```http
POST /api/jobs/{job_id}/override
```

Body:

```json
{
  "override_datetime": "2026-09-11T15:00:00"
}
```

Response:

```json
{
  "job_id": 2,
  "override_datetime": "2026-09-11T15:00:00"
}
```

### Request manual run

```http
POST /api/jobs/{job_id}/manual-run
```

The scheduler then processes the manual request using the effective datetime.

For an existing READY occurrence, the scheduler must preserve:

```text
occurrence_date
report_date
t_date
target_date
```

The override affects the execution/evaluation datetime; it does not redefine the business report date.

After the manual execution is consumed, the control is automatically cleared:

```text
manual_run        → 0
override_datetime → NULL
```

---

# 14. Clear Datetime Override

```http
DELETE /api/jobs/{job_id}/override
```

or:

```http
POST /api/jobs/{job_id}/override/clear
```

Recommended implementation:

```http
DELETE /api/jobs/{job_id}/override
```

Response:

```json
{
  "job_id": 2,
  "override_datetime": null
}
```

---

# 15. Confirmation APIs

## Confirm

```http
POST /api/jobs/{job_id}/confirm
```

## Clear Confirmation

```http
POST /api/jobs/{job_id}/confirmation/clear
```

Response:

```json
{
  "job_id": 2,
  "confirmation": 1
}
```

or:

```json
{
  "job_id": 2,
  "confirmation": 0
}
```

Confirmation remains a scheduler eligibility gate.

---

# 16. STAGING Monitoring API

## List STAGING Jobs

```http
GET /api/staging
```

Example:

```json
{
  "jobs": [
    {
      "job_id": 2,
      "job_name": "FTD_EXTRACT",
      "state": "WAITING_EXECUTION_DATE",
      "occurrence_date": "2026-09-10",
      "execution_date": "2026-09-11",
      "t_date": "2026-09-10",
      "report_date": "2026-09-10",
      "target_date": "2026-09-10",
      "waiting_for": "WAITING_EXECUTION_DATE",
      "reason": "Waiting for scheduled execution date."
    }
  ]
}
```

---

## Get STAGING Record

```http
GET /api/staging/{job_id}
```

---

# 17. READY Monitoring API

## List READY Jobs

```http
GET /api/ready
```

Example:

```json
{
  "jobs": [
    {
      "job_id": 2,
      "job_name": "FTD_EXTRACT",
      "occurrence_date": "2026-09-10",
      "execution_date": "2026-09-11",
      "t_date": "2026-09-10",
      "report_date": "2026-09-10",
      "target_date": "2026-09-10",
      "priority_key": [-1, 0, 0, 2]
    }
  ]
}
```

---

## Get READY Record

```http
GET /api/ready/{job_id}
```

---

# 18. Priority Queue API

The priority queue is derived state.

Django should treat READY as authoritative.

## Queue View

```http
GET /api/queue
```

Example:

```json
{
  "size": 2,
  "jobs": [
    {
      "job_id": 2,
      "job_name": "FTD_EXTRACT",
      "priority_key": [-1, 0, 0, 2]
    }
  ]
}
```

## Queue Peek

```http
GET /api/queue/next
```

This only inspects the highest-priority queue item.

Django should not remove an item from the queue.

---

# 19. Execution APIs

## Current Running Executions

```http
GET /api/executions/running
```

## Execution History

```http
GET /api/executions
```

Optional query parameters:

```text
job_id
report_date
status
from_date
to_date
limit
offset
```

Example:

```http
GET /api/executions?job_id=2&status=FAILED
```

---

## One Execution

```http
GET /api/executions/{execution_id}
```

Example:

```json
{
  "id": 10,
  "job_id": 2,
  "job_name": "FTD_EXTRACT",
  "procedure_name": "SCHEDULE_EXTRACTS.FTD_EXTRACT",
  "report_date": "2026-09-10",
  "status": "SUCCESS",
  "attempt_no": 2,
  "count": 9002,
  "duration_seconds": 0.1
}
```

---

# 20. Execution Trigger API

The scheduler application owns execution.

Therefore Django should not call Oracle directly.

For operational use, the application can expose:

```http
POST /api/execution/run-next
```

This asks the ExecutionManager to process the next READY item.

Optional body:

```json
{
  "max_jobs": 1
}
```

Response:

```json
{
  "results": [
    {
      "job_id": 2,
      "status": "SUCCESS",
      "executed": true,
      "report_date": "2026-09-10"
    }
  ]
}
```

Whether this endpoint is enabled in production should be controlled by deployment policy. The normal scheduler process should remain the primary execution mechanism.

---

# 21. Scheduler Cycle API

For administration/testing only:

```http
POST /api/scheduler/run-cycle
```

Optional body:

```json
{
  "current_datetime": "2026-09-11T12:00:00"
}
```

Response:

```json
{
  "timestamp": "2026-09-11T12:00:00",
  "total_jobs": 37,
  "staging_count": 31,
  "ready_count": 6,
  "queue_size": 6,
  "states": {
    "STAGING": 31,
    "READY": 6
  }
}
```

Production deployments may disable this endpoint or restrict it to administrators.

---

# 22. Crash Recovery API

For monitoring:

```http
GET /api/recovery/status
```

For an administrative recovery operation:

```http
POST /api/recovery/running
```

The scheduler already supports recovery of orphaned RUNNING executions after process restart.

---

# 23. API Authentication

The API should not be exposed anonymously in production.

Recommended architecture:

```text
Browser
   ↓
Django
   ↓
Authenticated server-to-server request
   ↓
Scheduler API
```

Django authentication remains responsible for the user.

The scheduler API should additionally verify the Django service identity.

Recommended options:

```text
Option A:
Static service token

Option B:
Internal network + service token

Option C:
Reverse proxy authentication
```

Example request:

```http
Authorization: Bearer <SCHEDULER_API_TOKEN>
```

The token must be stored in environment configuration, not in source code.

---

# 24. Django Integration

Django should have a dedicated API client/service.

Suggested structure:

```text
Django Project
│
├── scheduler_client/
│   ├── client.py
│   ├── exceptions.py
│   └── service.py
│
├── dashboard/
├── jobs/
└── ...
```

The Django application calls the scheduler API through `scheduler_client`.

---

## Example Django Configuration

```python
SCHEDULER_API_URL = "http://127.0.0.1:8001/api"

SCHEDULER_API_TOKEN = os.getenv(
    "SCHEDULER_API_TOKEN"
)
```

---

## Example Django HTTP Request

Conceptually:

```python
GET /api/jobs
```

with:

```http
Authorization: Bearer <token>
Accept: application/json
```

Django receives JSON and renders the dashboard.

---

# 25. Recommended Django Client Contract

Django should have methods conceptually equivalent to:

```text
get_health()
get_status()

get_jobs()
get_job(job_id)

pause_job(job_id)
resume_job(job_id)
cancel_job(job_id)
activate_job(job_id)

request_manual_run(job_id)

set_override(job_id, override_datetime)
clear_override(job_id)

confirm_job(job_id)
clear_confirmation(job_id)

get_staging()
get_staging_job(job_id)

get_ready()
get_ready_job(job_id)

get_queue()
peek_queue()

get_executions(...)
get_execution(execution_id)

run_next(...)
```

The Django UI should call these methods rather than embedding API URLs throughout templates/views.

---

# 26. HTTP Error Contract

The scheduler API should return consistent JSON errors.

Example:

```json
{
  "error": {
    "code": "JOB_NOT_FOUND",
    "message": "Job 999 does not exist."
  }
}
```

Suggested HTTP status codes:

```text
200 → successful GET/action
201 → resource/control request created
400 → invalid request
401 → authentication failure
403 → authorization failure
404 → job/record not found
409 → state conflict
422 → business validation error
500 → internal scheduler error
503 → scheduler/Oracle dependency unavailable
```

---

# 27. Concurrency Rules

The scheduler application is the authority for execution.

Django requests may arrive concurrently.

Therefore:

- Django must not maintain a second local READY queue.
- Django must not assume a READY item still exists after reading it.
- ExecutionManager must re-read persistent READY state before execution.
- Duplicate protection remains inside ExecutionManager.
- Manual control changes must be treated as requests, not direct execution commands.

---

# 28. Scheduler Process

The scheduler process runs independently.

Current intended schedule:

```text
Every 3 minutes
```

The main loop performs approximately:

```text
Scheduler cycle
        ↓
Persist STAGING / READY
        ↓
Rebuild priority queue
        ↓
ExecutionManager
        ↓
Oracle
```

On process startup:

```text
Recover orphaned RUNNING executions
        ↓
Start scheduler loop
```

---

# 29. Configuration

Configuration is expected to be environment-driven.

Important environment values include:

```text
ORACLE_USER
ORACLE_PASSWORD
ORACLE_HOST
ORACLE_PORT
ORACLE_SERVICE

SCHEDULER_INTERVAL_SECONDS
MAX_SCHEDULED_ATTEMPTS

SCHEDULER_API_HOST
SCHEDULER_API_PORT
SCHEDULER_API_TOKEN

# Oracle-owned scheduler inputs (optional; file snapshots remain the default)
SCHEDULER_MASTER_SOURCE=file|oracle
SCHEDULER_MASTER_TABLE=SCHEDULE_EXTRACT_MASTER
SCHEDULER_MASTER_REFRESH_SECONDS=3600
SCHEDULER_MASTER_ALLOW_STALE_SNAPSHOT=1
SCHEDULER_CALENDAR_SOURCE=file|oracle
SCHEDULER_DATEMAST_TABLE=DATEMAST
SCHEDULER_DATEMAST_DATE_COLUMN=REPORT_DATE
# Optional: blank/unset uses DATEMAST + bank rules and the local holiday snapshot
SCHEDULER_HOLIDAY_TABLE=
SCHEDULER_HOLIDAY_DATE_COLUMN=HOLIDAY_DATE
SCHEDULER_CALENDAR_RETRY_SECONDS=300
SCHEDULER_EXTG_MIRROR=0
SCHEDULER_EXTG_TABLE=SCHEDULE_EXTG
```

Do not commit credentials to source control.

### Oracle input snapshots

`Schedule_Master.json`, `datemaster.json`, and `holiday_master.json` are
worker-owned snapshots. When `SCHEDULER_MASTER_SOURCE=oracle`, the scheduler
reads every row from the configured Oracle master table, validates the entire
result, and only then atomically replaces `Schedule_Master.json`. A worker
therefore never consumes a partially written configuration file.

The Oracle calendar refresh reads `DATEMAST` once on service startup and then
once per local calendar day. A successful read is reused for the rest of that
day; the first cycle after midnight refreshes it. The calendar's **Refresh from
Oracle** action can pick up a same-day correction immediately. Failed reads
retry after `SCHEDULER_CALENDAR_RETRY_SECONDS` (default 300 seconds, minimum 30).
The legacy `SCHEDULER_CALENDAR_REFRESH_SECONDS` setting only supplies that
failure backoff when the new setting is absent. Schedule Master refreshes and
UI writes remain independent of this daily DATEMAST cache.

An extra holiday table
is read only when `SCHEDULER_HOLIDAY_TABLE` explicitly names one, such as
`HOLIDAY_MASTER`; its date column defaults to `HOLIDAY_DATE`. In normal worker
mode, only configured source snapshots are atomically published, so disabling
the extra source does not clear `holiday_master.json`. Monitor mode reads into
memory without publishing files. Failed reads retain the prior complete input
and surface a sanitised warning without passwords or connection descriptors.
`SCHEDULER_MASTER_ALLOW_STALE_SNAPSHOT` controls the separate Schedule Master
refresh; it does not make an optional holiday table mandatory.

The sync accepts `RUNS_ON` values including `DAILY`, `WEEKLY`, `FORTNIGHTLY`,
`MONTHLY`, `QUARTERLY`, `HALF-YEARLY`/`BI-ANNUALLY`, `ANNUALLY`, and
`SPECIFIC_DATE`/`ON_SPECIFIC_DATE`. A specific-date rule must include
`SPECIFIC_DATE` or `SPECIFIC_DATES` in `RUN_CONFIG`.

After applying the supplied schema migration, set
`SCHEDULER_MASTER_OPTIONAL_COLUMNS=TIME_FLAG,CONFIRMATION_NEEDED` when those
two top-level master controls are used. Leave it empty for an older master
table that does not expose those columns.

### Durable Oracle status and logging

`SCHEDULER_ORACLE_LOGGING=1` delivers current occurrence status to `SCHEDULE_EXTG`
and immutable operational events to `SCHEDULE_EXTG_LOG`. `JOB_ID` identifies the
schedule; `ID` identifies the status row. Status and execution attempt details,
controls, confirmations, queue changes, schedule definition edits and portal
work logs are persisted with actor, timestamp, reason and payload.

The worker commits events to a local SQLite outbox in the business transaction.
A separate delivery connection writes Oracle history and current status in one
transaction. Stable event IDs prevent duplicates after lost acknowledgements;
sequence guards prevent older replays overwriting current status. Failures retry
with backoff and remain visible in the portal. Keep the local databases.

The combined portal launcher also starts `manage.py deliver_audit_events --loop`,
which forwards portal audit events to `POST /v1/logging/events`. This endpoint
acknowledges durable worker acceptance, not Oracle completion. Inspect
`GET /v1/logging/status` or snapshot `meta.oracle_logging` for the Oracle backlog.
Logging continues in monitor mode; report procedures remain disabled there.

The one-time `rebuild_oracle_logging.py` defaults to a read-only preview. Its
`--apply` mode saves local DDL/data and an Oracle backup, validates the replacement,
then drops/replaces the legacy table using `sql/02_oracle_logging_schema.sql`.
Do not run a table drop as part of normal startup. Keep the generated backups.
The old `SCHEDULER_EXTG_MIRROR` must remain `0` with this implementation.
`Schedule_extg.json` is retained as a compatibility export; `Schedule_Master.json`
is a refreshed schedule snapshot in Oracle mode. Neither needs manual editing.

---

# 30. Oracle Execution Contract

The Oracle executor currently expects a procedure equivalent to:

```sql
PROCEDURE procedure_name(
    p_repdt IN DATE,
    v_cnt   OUT NUMBER
);
```

Python calls the configured procedure with:

```text
report_date
```

not:

```text execution_date
```

The `report_date` contract is a critical business invariant.

---

# 31. Schedule Master

The scheduler currently uses `Schedule_Master.json` as the schedule definition source.

Typical job entry:

```json
{
  "id": 2,
  "name": "FTD_EXTRACT",
  "package_name": "SCHEDULE_EXTRACTS.FTD_EXTRACT",
  "run_config": {
    "RUNS_ON": [
      "DAILY"
    ],
    "RUN_BY": {
      "FROM_TIME": "10:00",
      "TO_TIME": "11:00"
    },
    "HOLIDAY_RUN": [
      "SAT",
      "HOLIDAY"
    ]
  },
  "margin": "T",
  "same_day": 0,
  "time_flag": 1,
  "is_active": 1,
  "confirmation_needed": 0
}
```

---

# 32. DATEMAST

The scheduler's DATEMAST provider is intentionally abstracted.

Current application wiring uses the `DateMast` provider.

The Oracle snapshot synchroniser reads the configured `DATEMAST` date column
only within a rolling range: the 31 March boundary before the previous financial
year through today, inclusive. On 12 September 2026 this is 31 March 2025 through
12 September 2026; on 1 April 2027 the start advances to 31 March 2026. Bound
parameters filter rows in Oracle before fetching; the indexed date column stays
bare in the range predicates. Today's rows do not advance the T-1 publication
watermark. Optional future holiday dates are independent of this DATEMAST window.

Execution mode atomically replaces `datemaster.json` with `report_dates`,
`coverage_start` and `coverage_end`; the `DateMast` object reloads dates and bounds
together. Monitoring keeps the same bounded snapshot in memory. Legacy date-list
files remain supported. Dates outside verified coverage use provisional bank
rules, so excluded old dates are not inferred holidays. Startup/daily/manual
refresh behavior is unchanged. No records are deleted from Oracle.

The scheduler should treat the configured DATEMAST provider as authoritative for the report/T date used by an occurrence.

---

# 33. Holiday Calendar

Holiday evaluation is abstracted through `HolidayEvaluator`.

The working-day calculation is used for `SAME_DAY=0` execution-date determination.

DATEMAST plus the default banking calendar is a supported production calendar.
`SCHEDULER_HOLIDAY_TABLE` is blank by default; an empty extra holiday snapshot is
valid. First/third/fifth Saturdays are working by default, while second/fourth
Saturdays and Sundays are closed. Historical DATEMAST presence/absence through
T-1 remains authoritative. A populated local holiday snapshot or an explicitly
configured Oracle holiday source can add future holidays. An unconfigured
optional table is never queried or presented as a failure.

---

# 34. Retry Semantics

Scheduled execution retries are limited by:

```text
MAX_SCHEDULED_ATTEMPTS
```

Example:

```text
Attempt 1 → FAILED
Attempt 2 → FAILED
Attempt 3 → FAILED
Attempt 4 → blocked
```

The persistent READY row can remain available for monitoring after retry exhaustion.

Manual executions are not subject to the scheduled retry limit.

---

# 35. Duplicate Protection

Scheduled duplicate detection uses:

```text
job_id
report_date
```

A successful scheduled occurrence must never execute again.

Example:

```text
Manual execution:
job_id      = 2
report_date = 2026-09-10
SUCCESS

Later scheduled READY:
job_id      = 2
report_date = 2026-09-10

→ duplicate detected
→ Oracle not called again
```

---

# 36. Crash / Restart Semantics

Every Oracle execution is preceded by a persistent SQLite execution record.

Execution lifecycle:

```text
PENDING
   ↓
RUNNING
   ↓
SUCCESS / FAILED
```

If the Python process terminates while an execution is RUNNING, startup recovery can identify and recover orphaned RUNNING records.

For production safety, Oracle procedures should ideally be idempotent for the business key represented by:

```text
job_id + report_date
```

because a crash can happen after Oracle has committed but before Python records SUCCESS.

---

# 37. Recommended API Deployment

Recommended production layout:

```text
                 IIS / Nginx / Reverse Proxy
                           │
                ┌──────────┴──────────┐
                ▼                     ▼
          Django Web App       Scheduler API
                │                     │
                │                     │
                └──────────┬──────────┘
                           │
                         SQLite
                           │
                       Scheduler
                           │
                         Oracle
```

The scheduler API should normally be reachable only from the Django server or an internal network.

---

# 38. Development

Create/activate the virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Run the scheduler application:

```powershell
python main.py
```

Run the test suite:

```powershell
python -m unittest discover -v
```

---

# 39. Current Test Coverage

The project currently has regression coverage for:

```text
Frequency defaults
SAME_DAY
SAME_DAY=0
Holiday handling
T+N eligibility
Occurrence persistence
STAGING persistence
READY persistence
Execution-date persistence
Report-date contract
Priority queue
Retry behavior
Retry exhaustion
Crash recovery
Manual run
Datetime override
Manual + scheduled separation
Duplicate protection
Manual recovery after scheduled failure
One-shot control cleanup
```

---

# 40. Important Engineering Rules

The following rules should be maintained when extending the system:

1. Do not move scheduler calculations into Django.
2. Do not allow Django to directly execute Oracle.
3. Do not use the priority heap as persistent state.
4. Treat READY SQLite state as authoritative.
5. Preserve occurrence context across scheduler cycles.
6. Never replace `report_date` with `execution_date`.
7. Manual execution must not consume the scheduled occurrence unless that exact READY execution succeeds.
8. Scheduled duplicate protection must remain inside ExecutionManager.
9. Add tests before changing scheduling semantics.
10. Keep scheduler, execution and API layers separate.

---

# 41. Proposed API Service Implementation

The API layer should be implemented as a thin adapter around the existing application object graph.

Conceptually:

```text
HTTP Request
     ↓
API Router
     ↓
Application Service
     ↓
JobControlRepository / Scheduler / ExecutionManager
     ↓
JSON Response
```

The API layer should not duplicate scheduler logic.

For example:

```text
POST /api/jobs/2/manual-run
        ↓
JobControlRepository.request_manual_run(2)
        ↓
HTTP response
```

and:

```text
POST /api/jobs/2/pause
        ↓
JobControlRepository.pause(2)
        ↓
HTTP response
```

---

# 42. API Versioning

Use an explicit version from the beginning:

```text
/api/v1/
```

Recommended final paths:

```text
GET  /api/v1/health
GET  /api/v1/status

GET  /api/v1/jobs
GET  /api/v1/jobs/{id}

POST /api/v1/jobs/{id}/pause
POST /api/v1/jobs/{id}/resume
POST /api/v1/jobs/{id}/cancel
POST /api/v1/jobs/{id}/activate

POST /api/v1/jobs/{id}/manual-run

POST   /api/v1/jobs/{id}/override
DELETE /api/v1/jobs/{id}/override

POST /api/v1/jobs/{id}/confirm
POST /api/v1/jobs/{id}/confirmation/clear

GET /api/v1/staging
GET /api/v1/staging/{id}

GET /api/v1/ready
GET /api/v1/ready/{id}

GET /api/v1/queue
GET /api/v1/queue/next

GET /api/v1/executions
GET /api/v1/executions/{id}
GET /api/v1/executions/running

POST /api/v1/execution/run-next
```

Versioning now avoids breaking the Django integration later.

---

# 43. Suggested Next Implementation

The scheduler core is now sufficiently tested to begin the API layer.

Recommended implementation order:

```text
1. Scheduler API service
2. API authentication
3. Health/status endpoints
4. Job listing/control endpoints
5. STAGING/READY endpoints
6. Execution history endpoints
7. Django API client
8. Django dashboard
9. Django job control screens
```

The API should be implemented independently from the Django UI so that the scheduler can be monitored and controlled even before the complete dashboard exists.
