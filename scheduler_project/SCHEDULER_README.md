# Scheduler Control Application

The implemented API contracts, including the full day calendar, occurrence
confirmation, queue revision/reorder, fiscal March year end, and live RUNNING
monitoring, are documented in `README.md` under **Implemented control API**.
Those versioned `/v1/...` routes are authoritative; historical proposed routes
and generic eligibility examples below describe the earlier compatibility API.

The shared banking calendar treats DATEMAST as observed working dates through
T-1: on the 10th, the latest report marker can be the 9th. With a successful
nonempty feed, a missing past date is a holiday and a present past date is
working, including exceptional working weekends. Today/future use provisional
bank defaults: Sundays and second/fourth Saturdays are closed; first/third/fifth
Saturdays are working unless explicitly configured as holidays. Empty/failed
reads do not infer holidays from missing rows. The API supplies these decisions
to Django, including their source, reason, and provisional status. Future raw
DATEMAST entries never advance publication gates past T-1.

No extra Oracle holiday table is required. Leave `SCHEDULER_HOLIDAY_TABLE`
blank/unset for DATEMAST plus the banking defaults and any existing local extra
holiday dates. Set it explicitly only when the bank provides such a table;
`SCHEDULER_HOLIDAY_DATE_COLUMN` then defaults to `HOLIDAY_DATE`. A configured
source failure remains visible, but an unconfigured table is not queried and
does not generate a warning. Monitor reads preserve both protected JSON files.

## 1. Purpose

This project is the backend scheduler and execution engine for scheduled report/extract jobs.

The scheduler is deliberately kept separate from the Django monitoring/control application.

The scheduler is responsible for schedule evaluation, DATEMAST-based report-date eligibility, STAGING/READY persistence, priority ordering, Oracle execution, retries, duplicate protection, crash recovery, and execution history.

Django is responsible for authentication, dashboard/monitoring, job search/filtering, user actions, and calling scheduler control APIs.

Django must **not** reproduce scheduler eligibility logic and must **not** execute Oracle procedures directly.

---

## 2. High-Level Architecture

```text
                         ┌───────────────────────────┐
                         │        Django Web App      │
                         │                           │
                         │ Dashboard / Job Control  │
                         │ Authentication / Users   │
                         └─────────────┬─────────────┘
                                       │
                                       │ HTTP/JSON
                                       │ /api/v1/*
                                       ▼
                         ┌───────────────────────────┐
                         │      Scheduler API        │
                         │                           │
                         │ Thin HTTP adapter only   │
                         └─────────────┬─────────────┘
                                       │
                                       ▼
                         ┌───────────────────────────┐
                         │    Scheduler Application  │
                         │                           │
                         │ Scheduler                 │
                         │ JobControlRepository      │
                         │ ExecutionManager          │
                         └─────────────┬─────────────┘
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
                    ▼                  ▼                  ▼
              scheduler.db        Priority Queue       JSON files
              SQLite              derived state        Schedule_Master
              persistent state                           DATEMAST
              STAGING / READY                            Schedule_extg
                    │
                    ▼
                Oracle DB
```

The integration boundary is:

```text
Django
   |
   | HTTP/JSON
   v
Scheduler API
   |
   v
Scheduler application
   |
   +--> SQLite
   +--> JSON files
   +--> Oracle
```

Django does not import scheduler internals just to control a remote scheduler process.

---

## 3. Current Scheduler Flow

```text
Schedule_Master.json
        |
        v
    Scheduler
        |
        v
     STAGING
        |
        v
 Eligibility / DateMast / Control
        |
        v
      READY
        |
        v
 Priority Queue
        |
        v
 ExecutionManager
        |
        +----> SQLite execution_jobs
        |
        +----> Oracle
        |
        +----> Schedule_extg.json
        |
        v
 SUCCESS / FAILED
```

`READY` is the persistent source of truth.

The priority heap is derived state and can be rebuilt after restart.

---

## 4. Important Date Semantics

The application keeps these dates separate:

```text
occurrence_date  = scheduled business/report occurrence
report_date      = business/report date passed to Oracle
t_date           = T date used for T+N eligibility
target_date      = T+N threshold date
execution_date   = date on which execution is allowed
```

Example:

```text
Report occurrence : 15-Sep
T+3 target        : 18-Sep
16-Sep            : holiday
17-Sep            : next working execution date
18-Sep            : threshold reached
```

On 17-Sep the job may still wait because the T+3 threshold has not been reached.

On 18-Sep it can become READY.

Oracle still receives:

```text
report_date = 15-Sep
```

`report_date` must never be replaced by `execution_date`.

---

## 5. Manual Run Semantics

A manual run is an execution request through the control layer.

```text
Django
  |
  | POST /api/v1/jobs/{id}/manual-run
  v
Scheduler API
  |
  v
Job Control
  |
  v
Scheduler re-evaluates
  |
  v
READY
  |
  v
Priority Queue
  |
  v
ExecutionManager
```

Manual execution does not create a new scheduled occurrence.

If an existing scheduled READY occurrence exists, manual recovery reuses that existing occurrence context.

A datetime override can change the manual execution slot, but not the scheduled report/occurrence date.

After manual execution:

```text
manual_run        -> 0
override_datetime -> NULL
```

---

## 6. Scheduled Failure and Manual Recovery

A failed scheduled execution keeps the same READY occurrence available for retry or manual recovery.

```text
Scheduled READY
      |
      v
Attempt 1
      |
      v
FAILED
      |
      v
READY remains
      |
      v
Manual recovery
      |
      v
Attempt 2
      |
      v
SUCCESS
```

The report date remains the original scheduled report date.

---

## 7. Retry Semantics

Scheduled retries are limited by each task's `run_config.MAX_ATTEMPTS`.  
If a task does not define this value, the worker uses `MAX_SCHEDULED_ATTEMPTS`.

Example:

```text
Attempt 1 -> FAILED
Attempt 2 -> FAILED
Attempt 3 -> FAILED
Attempt 4 -> blocked
```

After retry exhaustion, READY can remain available for monitoring/control.

Manual executions are not subject to the scheduled retry limit.

---

## 8. Duplicate Protection

Scheduled duplicate protection uses:

```text
job_id + report_date
```

A successful occurrence must never execute again.

Example:

```text
Manual execution
job_id      = 2
report_date = 2026-09-10
SUCCESS

Later scheduled READY
job_id      = 2
report_date = 2026-09-10

=> duplicate detected
=> Oracle is not called again
```

---

## 9. Crash / Restart Semantics

Execution records are persisted before Oracle execution.

```text
PENDING
   |
   v
RUNNING
   |
   +----> SUCCESS
   |
   +----> FAILED
```

If the Python process terminates while an execution is RUNNING, startup recovery can recover orphaned RUNNING records.

For production safety, Oracle procedures should ideally be idempotent for:

```text
job_id + report_date
```

because Python can theoretically crash after Oracle commits but before Python records SUCCESS.

---

## 10. Persistent SQLite State

SQLite contains the scheduler's persistent state, including:

```text
job_control
staging_jobs
ready_jobs
execution_jobs
```

Django should inspect scheduler state through the API rather than directly modifying the SQLite database.

The API is the application control boundary.

---

# Scheduler API Contract

## 11. API Design Principles

The API is a thin adapter over the existing application object graph.

```text
HTTP Request
      |
      v
API Router
      |
      v
Application Service
      |
      +--> JobControlRepository
      +--> Scheduler
      +--> ExecutionManager
      +--> repositories
      |
      v
JSON Response
```

The API must not duplicate scheduler algorithms.

Example:

```text
POST /api/v1/jobs/2/manual-run
        |
        v
JobControlRepository.request_manual_run(2)
        |
        v
JSON response
```

and:

```text
POST /api/v1/jobs/2/pause
        |
        v
JobControlRepository.pause(2)
        |
        v
JSON response
```

---

## 12. API Versioning

Use:

```text
/api/v1/
```

Versioning is included from the beginning so that future API changes can be introduced as `/api/v2/` without breaking the Django client.

---

## 13. API Base URL

### Development

```text
http://127.0.0.1:8090
```

Full base path:

```text
http://127.0.0.1:8090/api/v1/
```

### Production

Use an internal hostname or reverse-proxy route, for example:

```text
https://scheduler-internal.example.com
```

The actual hostname is environment-specific.

The scheduler API should normally be reachable only from the Django server or another trusted internal network.

---

## 14. Authentication

Recommended application-to-application authentication:

```http
Authorization: Bearer <SCHEDULER_API_TOKEN>
```

Scheduler `.env`:

```env
SCHEDULER_API_HOST=127.0.0.1
SCHEDULER_API_PORT=8090
SCHEDULER_API_TOKEN=<strong-random-secret>
```

Django `.env`:

```env
SCHEDULER_API_BASE_URL=http://127.0.0.1:8090
SCHEDULER_API_TOKEN=<same-secret>
SCHEDULER_API_TIMEOUT=10
```

Do not hard-code the token in source code.

For production, also protect the API through a reverse proxy/firewall.

---

# 15. Health and Status

## GET `/api/v1/health`

Purpose: verify that the API process is alive.

Example response:

```json
{
  "status": "ok",
  "service": "scheduler-api"
}
```

This should be lightweight and should not require an Oracle call.

## GET `/api/v1/status`

Example response:

```json
{
  "status": "running",
  "scheduler": {
    "running": true,
    "last_cycle": "2026-09-11T10:42:00"
  },
  "execution": {
    "running": 1,
    "ready": 5,
    "failed": 2
  }
}
```

---

# 16. Job APIs

## GET `/api/v1/jobs`

Optional query parameters:

```text
?status=
?active=
?search=
?page=
?page_size=
```

Example:

```http
GET /api/v1/jobs?active=1&search=FTD
```

Example response:

```json
{
  "count": 1,
  "results": [
    {
      "id": 2,
      "name": "FTD_EXTRACT",
      "is_active": 1,
      "control": {
        "paused": 0,
        "manual_run": 0,
        "override_datetime": null
      }
    }
  ]
}
```

## GET `/api/v1/jobs/{id}`

Returns job, control, STAGING and READY information.

Example:

```json
{
  "id": 2,
  "name": "FTD_EXTRACT",
  "is_active": 1,
  "control": {
    "paused": 0,
    "manual_run": 0,
    "override_datetime": null
  },
  "staging": null,
  "ready": {
    "occurrence_date": "2026-09-10",
    "execution_date": "2026-09-11",
    "t_date": "2026-09-10",
    "report_date": "2026-09-10",
    "target_date": "2026-09-10"
  }
}
```

---

# 17. Job Control APIs

## POST `/api/v1/jobs/{id}/pause`

```json
{
  "success": true,
  "job_id": 2,
  "action": "pause"
}
```

## POST `/api/v1/jobs/{id}/resume`

```json
{
  "success": true,
  "job_id": 2,
  "action": "resume"
}
```

The scheduler reevaluates the job during the normal cycle.

## POST `/api/v1/jobs/{id}/cancel`

Cancels the current control/scheduled request according to scheduler control semantics.

```json
{
  "success": true,
  "job_id": 2,
  "action": "cancel"
}
```

## POST `/api/v1/jobs/{id}/activate`

```json
{
  "success": true,
  "job_id": 2,
  "action": "activate"
}
```

The API must call the control service/repository rather than directly editing READY/STAGING.

---

# 18. Manual Run API

## POST `/api/v1/jobs/{id}/manual-run`

Basic request:

```json
{}
```

With datetime override:

```json
{
  "override_datetime": "2026-09-11T15:00:00"
}
```

The API stores the control request. It does not directly execute Oracle.

Example response:

```json
{
  "success": true,
  "job_id": 2,
  "action": "manual_run",
  "manual_run": 1,
  "override_datetime": "2026-09-11T15:00:00"
}
```

---

# 19. Datetime Override APIs

## POST `/api/v1/jobs/{id}/override`

Request:

```json
{
  "override_datetime": "2026-09-11T15:00:00"
}
```

Response:

```json
{
  "success": true,
  "job_id": 2,
  "override_datetime": "2026-09-11T15:00:00"
}
```

## DELETE `/api/v1/jobs/{id}/override`

Response:

```json
{
  "success": true,
  "job_id": 2,
  "override_datetime": null
}
```

---

# 20. Confirmation APIs

## POST `/api/v1/jobs/{id}/confirm`

```json
{
  "success": true,
  "job_id": 2,
  "action": "confirm"
}
```

## POST `/api/v1/jobs/{id}/confirmation/clear`

```json
{
  "success": true,
  "job_id": 2,
  "action": "confirmation_clear"
}
```

Confirmation is a gate, not a priority value.

---

# 21. STAGING APIs

## GET `/api/v1/staging`

Example:

```json
{
  "count": 1,
  "results": [
    {
      "job_id": 2,
      "occurrence_date": "2026-09-10",
      "execution_date": "2026-09-11",
      "t_date": "2026-09-10",
      "report_date": "2026-09-10",
      "target_date": "2026-09-10",
      "status": "STAGING"
    }
  ]
}
```

## GET `/api/v1/staging/{id}`

Returns one STAGING record.

---

# 22. READY APIs

## GET `/api/v1/ready`

Example:

```json
{
  "count": 1,
  "results": [
    {
      "id": 10,
      "job_id": 2,
      "occurrence_date": "2026-09-10",
      "execution_date": "2026-09-11",
      "t_date": "2026-09-10",
      "report_date": "2026-09-10",
      "target_date": "2026-09-10",
      "priority": 15
    }
  ]
}
```

## GET `/api/v1/ready/{id}`

Returns one READY record.

---

# 23. Priority Queue APIs

## GET `/api/v1/queue`

Returns derived queue information for monitoring.

```json
{
  "size": 2,
  "jobs": [
    {
      "job_id": 2,
      "ready_id": 10,
      "priority": 15
    }
  ]
}
```

The heap must not become the persistent source of truth.

## GET `/api/v1/queue/next`

Returns the next item without executing Oracle.

```json
{
  "job_id": 2,
  "ready_id": 10,
  "report_date": "2026-09-10",
  "execution_date": "2026-09-11",
  "priority": 15
}
```

---

# 24. Execution APIs

## GET `/api/v1/executions`

Recommended query parameters:

```text
?job_id=
?status=
?report_date=
?from_date=
?to_date=
?page=
?page_size=
```

Example:

```http
GET /api/v1/executions?job_id=2&status=FAILED
```

Example response:

```json
{
  "count": 1,
  "results": [
    {
      "id": 9,
      "job_id": 2,
      "status": "FAILED",
      "attempt_no": 1,
      "report_date": "2026-09-10",
      "started_at": "2026-09-11T11:00:00",
      "finished_at": "2026-09-11T11:01:10",
      "error_info": "Oracle execution failed"
    }
  ]
}
```

## GET `/api/v1/executions/{id}`

Returns one execution.

## GET `/api/v1/executions/running`

Returns currently RUNNING executions.

## POST `/api/v1/execution/run-next`

Runs the next READY item through `ExecutionManager`.

This is primarily for controlled/administrative use. Normal production execution remains driven by the scheduler's normal execution loop.

Example:

```json
{
  "success": true,
  "job_id": 2,
  "execution_id": 10,
  "status": "SUCCESS"
}
```

---

# 25. API Error Contract

Use a consistent JSON error format:

```json
{
  "success": false,
  "error": {
    "code": "JOB_NOT_FOUND",
    "message": "Job 999 was not found."
  }
}
```

Recommended error codes:

```text
AUTHENTICATION_REQUIRED
FORBIDDEN
JOB_NOT_FOUND
READY_NOT_FOUND
STAGING_NOT_FOUND
INVALID_REQUEST
INVALID_DATETIME
INVALID_STATE
CONTROL_OPERATION_FAILED
SCHEDULER_UNAVAILABLE
EXECUTION_FAILED
INTERNAL_ERROR
```

Recommended HTTP statuses:

```text
200 OK
201 Created
400 Bad Request
401 Unauthorized
403 Forbidden
404 Not Found
409 Conflict
500 Internal Server Error
503 Service Unavailable
```

Do not expose secrets or internal stack traces in API responses.

---

# 26. Django Connection

Django should use a dedicated scheduler API client.

```text
Django View
    |
    v
Django SchedulerAPIClient
    |
    | HTTP + JSON + Bearer token
    v
Scheduler API
    |
    v
Scheduler application
```

Django must not call the following scheduler internals directly when the scheduler is a separate process/service:

```text
JobControlRepository
Scheduler
ExecutionManager
OracleExecutor
```

Instead Django calls:

```text
GET  /api/v1/jobs
GET  /api/v1/ready
GET  /api/v1/staging
GET  /api/v1/executions

POST /api/v1/jobs/{id}/pause
POST /api/v1/jobs/{id}/resume
POST /api/v1/jobs/{id}/manual-run
POST /api/v1/jobs/{id}/override
DELETE /api/v1/jobs/{id}/override
```

---

# 27. Django Environment Configuration

```env
SCHEDULER_API_BASE_URL=http://127.0.0.1:8090
SCHEDULER_API_TOKEN=<strong-random-secret>
SCHEDULER_API_TIMEOUT=10
```

Django API requests should include:

```http
Authorization: Bearer <SCHEDULER_API_TOKEN>
Accept: application/json
Content-Type: application/json
```

The client should handle:

```text
connection errors
timeouts
non-2xx responses
malformed JSON
authentication errors
scheduler unavailable
```

---

# 28. Django API Client Interface

The future Django client should expose methods similar to:

```text
get_health()
get_status()

get_jobs(params=None)
get_job(job_id)

pause_job(job_id)
resume_job(job_id)
cancel_job(job_id)
activate_job(job_id)

manual_run(job_id, override_datetime=None)

set_override(job_id, override_datetime)
clear_override(job_id)

confirm_job(job_id)
clear_confirmation(job_id)

get_staging(params=None)
get_staging_job(staging_id)

get_ready(params=None)
get_ready_job(ready_id)

get_queue()
get_next_queue_job()

get_executions(params=None)
get_execution(execution_id)
get_running_executions()
```

These are Django client methods, not scheduler logic.

---

# 29. Django Dashboard

The initial dashboard should display:

```text
Total Jobs
Active Jobs
Paused Jobs
STAGING
READY
RUNNING
FAILED
SUCCESS
```

Job table fields:

```text
Job ID
Job Name
Status
Occurrence Date
Report Date
T Date
Target Date
Execution Date
Priority
Attempt
Pause/Resume
Manual Run
Override
Last Execution
```

The dashboard can poll the API periodically.

---

# 30. Recommended Production Deployment

```text
                 Client Browser
                       |
                       v
               Reverse Proxy / IIS
                       |
             ┌─────────┴─────────┐
             |                   |
             v                   v
       Django Web App       Scheduler API
             |                   |
             | HTTP/JSON         |
             └─────────┬─────────┘
                       |
                    SQLite
                       |
                   Scheduler
                       |
                    Oracle
```

Preferred security boundary:

```text
Internet/User Network
        |
        v
      Django
        |
    internal API
        |
        v
 Scheduler API
```

The scheduler API should not be publicly exposed unless there is a specific requirement and adequate security controls.

---

# 31. API Implementation Order

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

The scheduler API should be independently testable before the Django UI is completed.

---

# 32. Engineering Boundary Rules

These rules are mandatory:

```text
1. Django does not calculate scheduler eligibility.
2. Django does not calculate T+N dates.
3. Django does not manipulate the priority heap.
4. Django does not insert READY rows directly.
5. Django does not execute Oracle procedures directly.
6. Django uses the Scheduler API for control operations.
7. Scheduler API delegates to existing scheduler components.
8. READY remains the persistent source of truth.
9. report_date is never replaced by execution_date.
10. Manual run remains a one-shot control.
11. Scheduled duplicate protection remains inside ExecutionManager.
12. Scheduler/API/UI layers remain separate.
```

---

# 33. Current Project Structure

```text
scheduler_project/
│
├── main.py
├── Schedule_Master.json
├── Schedule_extg.json
├── requirements.txt
├── .env
│
├── database/
│   └── sqlite_db.py
│
├── models/
│   ├── staging_job.py
│   ├── ready_job.py
│   └── execution_job.py
│
├── repositories/
│   ├── staging_repository.py
│   ├── ready_repository.py
│   ├── execution_repository.py
│   ├── job_control_repository.py
│   ├── schedule_master_repository.py
│   └── schedule_extg_repository.py
│
├── scheduler/
│   ├── scheduler.py
│   ├── staging.py
│   ├── eligibility.py
│   ├── priority_queue.py
│   └── ...
│
├── execution/
│   ├── execution_manager.py
│   └── oracle_executor.py
│
├── api/
│   └── ...
│
├── logs/
│   └── scheduler.log
│
└── tests/
    └── ...
```

---

# 34. Testing

Run:

```powershell
python -m unittest discover -v
```

Existing regression areas include:

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

API tests should additionally cover:

```text
Health
Authentication
Job list/detail
Pause/resume
Cancel/activate
Manual run
Datetime override
Confirmation
STAGING
READY
Queue
Execution history
Error responses
```

---

# 35. Important Production Considerations

### Holiday source

The current scheduler wiring still uses an empty holiday-set placeholder in `main.py`. Production holiday data must be wired into the scheduler.

### Concurrent execution

If multiple execution workers are introduced later, duplicate protection should be atomic so that two workers cannot claim the same logical execution.

### Schedule_extg concurrency

Because `Schedule_extg.json` uses file-based persistence, concurrent read-modify-write operations need locking or another safe persistence mechanism when multiple writers are introduced.

### Dependencies

`requirements.txt` must contain all runtime dependencies actually required by the production application, including the Oracle driver and environment configuration package used by the code.

---

# 36. Development

Create/activate the virtual environment:

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

Run scheduler:

```powershell
python main.py
```

Run tests:

```powershell
python -m unittest discover -v
```

The API service will be started separately once the API layer is implemented.

---

# 37. Current Milestone

The scheduler core is now sufficiently tested to move to the API/integration layer.

Current pipeline:

```text
Schedule_Master
       ↓
Eligibility
       ↓
STAGING
       ↓
READY
       ↓
Priority Queue
       ↓
ExecutionManager
       ↓
Oracle
       ↓
Execution History
```

Next:

```text
Scheduler Core
      ↓
Scheduler API
      ↓
Django API Client
      ↓
Django Dashboard
      ↓
Django Job Control
```

The scheduler should continue operating correctly even when the Django application is unavailable.

The Django application should be replaceable without changing scheduler rules.

The API should be replaceable without changing scheduler rules.
