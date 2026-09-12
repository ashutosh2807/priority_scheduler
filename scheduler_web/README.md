# ITRP Scheduler Operations

Start **`start_portal.bat`** in this folder. It starts both the scheduler worker and the Django portal. Open [the portal](http://127.0.0.1:8010/).

Read [RUNNING_THE_PROJECT.md](RUNNING_THE_PROJECT.md) for exact Windows commands, folder separation, Oracle configuration, saved data and shutdown. [OPERATIONS.md](OPERATIONS.md) explains daily operations and calendar rules.

For office migration, driver selection, isolated office data and Django admin management, use [OFFICE_DEPLOYMENT.md](OFFICE_DEPLOYMENT.md). Run `start_portal.bat --check` to verify Oracle connectivity without running tasks or changing Oracle data.

To keep only the worker running through Windows Task Scheduler and start Django separately with `python manage.py runserver`, follow [WINDOWS_WORKER_SETUP.md](WINDOWS_WORKER_SETUP.md). The scheduled entry point is `scheduler_project/run_scheduler_service.bat`.

## What is included

- Calendar-first dashboard and paginated day-wise tasks, queue and history.
- Schedule creation, editing and deletion through the UI, saved by the worker to Oracle in Oracle mode.
- Named confirmation owners, leave coverage, runbooks and actor audit.
- Oracle DATEMAST working-day calendar with T−1 publication, daily refresh and an operator refresh action.
- Configurable per-task attempts throughout the intended execution day; later recovery requires a manual request.
- Live priority order, pause/resume controls and explanations for an empty queue.
- Oracle `SCHEDULE_EXTG` current status and `SCHEDULE_EXTG_LOG` audit history, with durable delivery and visible backlog.
- Black-and-white night theme for the portal and Django admin.
- Empty SULOG, ELOADLOG and MAILER applications, each with its own URL, view and template.

## Application placeholders

The sidebar's **Applications** section, above **Scheduler**, opens `/sulog/`, `/eloadlog/` and `/mailer/`.
They run through the same `run_portal.bat` / `start_portal.bat` launcher as the dashboard.
Their code lives in `apps/sulog/`, `apps/eloadlog/` and `apps/mailer/`; the respective
templates are `templates/sulog/index.html`, `templates/eloadlog/index.html` and
`templates/mailer/index.html`. These are authenticated starting pages with no models,
database integrations or email-sending behavior yet. Add each application's future
features inside its own folder.

The illustrated guide is [ITRP setup and operations](output/pdf/ITRP_Scheduler_Setup_and_Operations_Guide.pdf).

The combined launcher defaults to **monitoring mode**. Schedule edits and controls are saved, but Oracle procedures are not executed. Stop the monitoring launcher before starting `start_portal.bat --execute` when actual execution is intended.

## Project boundary

This folder contains the Django application: `apps/`, `templates/`, `static/`, `config/`, `manage.py` and the combined launcher. The sibling `scheduler_project/` contains Oracle access, calendar evaluation, the worker and its local queue/history. Keep both folders next to each other under `django_app/`.

Do not delete either application's database, the worker's `file_repository/`, `.env` files, virtual environment, source, migrations or tests. Import backups are stored under `scheduler_project/backups/`.

## Regression checks

From this folder, using the shared environment:

```powershell
& '..\.scheduler-venv\Scripts\python.exe' -B manage.py test apps.scheduler apps.dashboard
```

The bank's existing report procedures are unchanged. The operating guide explains the upgraded Oracle logging schema, local delivery queues and retained snapshots.
