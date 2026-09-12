# ITRP operations workflow

The portal controls and monitors the separate scheduler. It never executes an
Oracle procedure or writes scheduler state directly.

## Start both services locally

Run `start_portal.bat` from this folder, or use the working Python environment:

```powershell
& '..\.scheduler-venv\Scripts\python.exe' run_portal.py
```

Open `http://127.0.0.1:8010/scheduler/day/` and sign in with your existing account.
Keep the launcher running. It starts the standalone scheduler on port 8091 and
the portal on port 8010, reusing already available services. Ctrl+C stops only
the processes it started. Launch errors are recorded under this folder's `logs/`.

The default is **monitoring mode with Oracle schedules and DATEMAST**. It evaluates the real
Schedule Master and persists eligibility, queue state and Oracle audit logs, without calling report
procedures. Existing execution history stays
visible. Controls and runbook changes are real and saved. The portal displays a
monitoring notice on operational pages. Calendar refreshes are read-only and held
in memory, leaving `datemaster.json` intact.

Oracle `SCHEDULE_EXTG` stores current task status. `SCHEDULE_EXTG_LOG` retains execution attempts,
status changes, queue/confirmation controls, schedule edits and portal work logs, including who acted,
when, the reason and details. Monitoring is identified as `MONITOR` in status events.
The Audit page shows portal-to-worker and worker-to-Oracle backlogs. Delivery retries automatically;
keep both local databases so pending logs survive outages and restarts. A successful checklist action
is an operator record; only an actual successful procedure run becomes execution status `SUCCESS`.

The portal shows the last published working report date and the T−1 cutoff.
Future DATEMAST entries are excluded from published report-date decisions.
If Oracle's holiday table is unavailable, the worker retains the local holiday
snapshot and displays a calendar-source warning. Verify that holiday data before
enabling execution.

To explicitly enable procedure execution, stop the monitoring launcher and run
`start_portal.bat --execute`. This uses the existing scheduler's eligibility,
confirmation, priority and duplicate-execution controls. `--calendar-source file`
together with `--master-source file` is available for deliberate local snapshot inspection; it is labelled as a file
source. Do not run both a service worker and recurring `--once` triggers for the
same installation. A one-cycle trigger cannot provide a continuous portal API.

## Manage schedules from the portal

SUPERUSER users can choose **Schedules → Add schedule**, **Edit schedule settings**, or
**Delete schedule**. The editor includes name, procedure, frequency, specific dates,
DATEMAST margin, execution window, holiday permissions, attempt limit, confirmation
owner/cover, expected duration and guide. The worker validates and commits definition
changes to Oracle `SCHEDULE_EXTRACT_MASTER` and refreshes its snapshot. No manual JSON
maintenance is required. Running definitions cannot be edited or deleted. Deletion
retains execution history and audit; existing IDs are not reused.

The live table's `CONFIRMATION` column maps to the per-schedule confirmation gate.
Configure `SCHEDULER_MASTER_OPTIONAL_COLUMNS=TIME_FLAG,CONFIRMATION` for that schema.
Tables using `CONFIRMATION_NEEDED` can configure that column instead.

The persistent confirmation alert links to actual unresolved confirmations for today,
with assigned-to-you and missing-cover counts. Leave routing is reapplied for today.

DATEMAST reads once on startup and once each local day. Failed reads retry with
`SCHEDULER_CALENDAR_RETRY_SECONDS` backoff. A SUPERUSER can use **Calendar → Refresh from
Oracle** after a same-day correction. Schedule definitions refresh independently, with a
one-hour interval (`SCHEDULER_MASTER_REFRESH_SECONDS=3600`). UI schedule saves refresh
immediately; edits made directly in Oracle wait until the next scheduled refresh.

The Oracle DATEMAST query loads the previous financial year and the current year to date,
including the preceding 31 March boundary: on 12 September 2026, **31 March 2025 through
12 September 2026**. The range rolls forward each 1 April. The calendar shows its loaded
range. Dates outside it are provisional and use bank-calendar defaults; missing rows
outside the loaded range are not evidence of a holiday. Within the range, publication
still ends at T−1. Historical task and execution records are retained.

API: `POST /v1/jobs`, `PATCH /v1/jobs/{id}/definition`, `DELETE /v1/jobs/{id}`,
`POST /v1/operations/calendar/refresh`. Every mutation includes actor/reason metadata.

## Daily use

- `/scheduler/day/`: select an operating day, status or assigned operator. One
  occurrence has one current status on a day; retries remain in execution history.
- `/calendar/`: month view supplied by the worker, with original report dates
  beside execution dates. Forecasts remain explicitly conditional on DATEMAST.
- `/scheduler/confirmation/`: day-specific confirmation tasks and named assignees.
- `/scheduler/queue/`: move READY occurrences up/down, save the order, pause tasks,
  or resume paused tasks. An outdated queue revision is rejected by the worker.
- Task detail: description, guide steps, expected duration, per-report checklist,
  work notes, execution history and scoped audit trail.
- `/help/`: operator instructions and fiscal-calendar explanations.

## Holiday markers and DATEMAST publication

The dashboard, operations calendar and leave page share the worker's bank
calendar. By default, Sundays and the second/fourth Saturdays are holidays;
first/third/fifth Saturdays are working days unless another holiday applies.
The same calendar drives scheduling, T+1 execution dates and weekly period ends.

DATEMAST contains working dates through T−1. For past dates, presence confirms a
working day and absence indicates a holiday. Today and future dates must never
be marked as holidays simply because their DATEMAST entry is absent. For example,
on the 10th, the 9th is present if it was a working day; the missing 10th is normal.
Today/future classifications remain provisional and use the bank calendar and
configured holidays. Historical DATEMAST entries can record special working days.
An unavailable or empty DATEMAST feed cannot confirm missing-date holidays.

Holiday cells explain whether the marker comes from DATEMAST or a default bank
closure. The legend shows the T−1 assessment cutoff. Working Saturdays are labelled
separately; tasks and approved leave stay visible. Selecting a holiday preserves
tasks which permit holiday execution.

## Confirmation ownership and leave

A SUPERUSER configures the primary operator and preferred backup. The primary operator
may edit the runbook but cannot reassign ownership. All active operators may use
ordinary queue controls; resets, time overrides and service controls require SUPERUSER.

Only the effective assignee may confirm. Resolution is: available primary,
available nominated cover during approved leave, preferred backup, then an
available active operator ordered by employee ID. No available operator means
the confirmation remains unassigned and blocked. Pending leave does not change
operational availability. Leave approval rechecks cover and fills gaps, recording
the assignments in the audit. Daily fallback is calculated without changing the
primary owner; the actual confirmer and assignment source are audited.

Confirmations carry the worker's exact occurrence key. The worker rejects stale
keys and scopes approval to that report rather than future reports. Runbook
checks are human records, not Oracle execution outcomes; edited steps invalidate
old step submissions and previous text is retained in the audit.

## Automatic attempts and manual recovery

A SUPERUSER can edit **Maximum automatic attempts** in a
schedule's **Edit schedule settings** page. The default remains 3 total attempts,
including the first run. Set 6 to allow the first run plus 5 retries; supported
limits are 1 to 100. The legacy limited-configuration page also accepts blank to preserve the current limit. The worker stores
this setting as `RUN_CONFIG.MAX_ATTEMPTS` and audits the change.

Failed runs can retry throughout the planned day by default (`SCHEDULER_RETRY_WINDOWS=ALL_DAY`), including after their initial RUN_BY window. Automatic execution and retries stop after the planned execution day. Overdue occurrences show **Manual required** and stay out of the automatic queue. A manual attempt retains the original planned date; it does not extend automatic retry eligibility. Any later
run must be requested manually from the task controls and still passes the
worker's applicable controls. The portal does not create retries or execute work.

## Calendar contract

Default period ends are 15th/month-end for fortnightly, month-end for monthly,
March/June/September/December for quarterly, March/September for half-yearly and
31 March for annually. The worker keeps configured per-schedule rules authoritative.
Oracle DATEMAST determines available business dates. SAME_DAY and T+1, holiday
permissions, execution windows, retries and eligibility remain worker decisions.

The portal reads `/v1/operations/calendar?start_date=YYYY-MM-DD&days=31` and
`/v1/operations/snapshot`. It forwards control requests through the existing
authenticated control API; it never calculates its own working dates or heap.
Read-only local fallback is opt-in and cannot provide full future projections.

## Office deployment

1. Install this portal's `requirements.txt` in the office Python environment.
2. Configure Django settings using `.env.example`; use an appropriate production
   secret, approved hosts, HTTPS cookie settings and the scheduler API token.
3. Apply `python manage.py migrate` and run `python manage.py check`.
4. On the **separate scheduler process**, set `SCHEDULER_CALENDAR_SOURCE=oracle`
   and the existing Oracle connection/table settings documented in its README.
   The portal must not receive Oracle credentials. Configure the real holiday
   source supported by the worker and verify its DATEMAST watermark before use.
5. Keep matching `SCHEDULER_API_TOKEN` values on both processes. Start the worker
   through the organization's execution/service process. Starting it can execute
   real package procedures; a portal preview alone does not start it.
6. A SUPERUSER assigns primary confirmation operators and documents the runbooks.

The worker's standalone default remains compatible with its configured source.
The combined portal launcher explicitly selects Oracle DATEMAST. Live procedure
execution must be validated in the office environment. `Schedule_Master.json`
and `datemaster.json` were not edited to populate the portal.

All CSS, JavaScript and icon fonts are local under `static/vendor`. Successful
operations use dismissible toast notifications; errors stay until dismissed.
Polling pauses while a form has unsaved input or a dialog is open.

## Validation

Run `python manage.py test` for portal workflow and authorization tests. Worker
calendar, stale queue revisions, scoped confirmations, retry and concurrency
tests live in `scheduler_project`. Tests use isolated fixtures; they do not run
real Oracle procedures.
