# Moving the ITRP scheduler to the office

The same application supports `oracledb` at home and `cx_Oracle` in the office. Select the driver and credentials in the worker's `.env`, then restart the project. Browser refresh does not reload credentials or change an already-running worker's driver.

For a persistent Windows worker with Django started separately using `python manage.py runserver`, use [WINDOWS_WORKER_SETUP.md](WINDOWS_WORKER_SETUP.md) after the office environment setup below.

## One-time office setup

Keep the two application folders beside each other at any approved location:

```text
django_app/
  scheduler_project/       Worker and Oracle access
  scheduler_web/           Portal and Django admin
  .scheduler-venv/         Recreated with the office Python
  run_portal.bat           Optional shortcut to scheduler_web/start_portal.bat
```

Copy source, templates, static assets, requirements, SQL, and migrations. Recreate the Python environment on the office machine: a copied home virtual environment contains machine-specific paths. For a new office installation, use the isolated paths in the office examples; copied home databases, snapshots and undelivered events will then remain unused. An intentional transfer of accounts or operational history is a separate data migration, not part of connecting to a different Oracle database.

The project requires Python 3.10 or newer. Use an interpreter compatible with the office's approved `cx_Oracle` build and Oracle Client, with matching architecture. Oracle documents cx_Oracle 8.3 testing through Python 3.10; this does not certify every newer interpreter/build combination. Django 5.2 supports Python 3.10 and newer supported versions. The office profile selects Django 5.2. See [Oracle's driver installation guide](https://cx-oracle.readthedocs.io/en/latest/user_guide/installation.html) and [Django's Python compatibility](https://docs.djangoproject.com/en/5.2/faq/install/#what-python-version-can-i-use-with-django).

From `django_app`, create the environment using the approved office Python and install from the approved package source or wheel directory:

```bat
python -m venv .scheduler-venv
.scheduler-venv\Scripts\python.exe -m pip install -r scheduler_web\requirements-office.txt
```

This profile installs `cx_Oracle`, not `oracledb`. If the office already maintains a compatible interpreter with approved `cx_Oracle`, keep that installation and ensure it also has the portal requirements, Django 5.2 and `python-dotenv`. To select it explicitly in the launcher, set `SCHEDULER_PYTHON` to its full `python.exe` path before starting. The launcher otherwise checks the sibling `.scheduler-venv`, the worker's `venv`, then an activated virtual environment. It never installs packages or replaces another program's environment automatically.

## Configure the office installation

In the office copy only, copy each `.env.office.example` to `.env` inside its own folder. Replace the placeholders with the office settings. Existing process/service environment variables take precedence over `.env`; remove stale home overrides from the office service configuration.

In `scheduler_project/.env`:

```dotenv
ORACLE_DRIVER=cx_oracle
ORACLE_CLIENT_LIB_DIR=C:/approved/path/to/instantclient
ORACLE_USER=office_user
ORACLE_PASSWORD=office_password
ORACLE_DSN=office_host:1521/office_service
SCHEDULER_DATA_DIR=runtime/office
SCHEDULER_MASTER_SOURCE=oracle
SCHEDULER_CALENDAR_SOURCE=oracle
SCHEDULER_MASTER_ALLOW_STALE_SNAPSHOT=0
```

Use the actual Oracle Client directory. Leave `ORACLE_CLIENT_LIB_DIR` blank when the office's installed client already loads through its normal system search. Alternatively, clear `ORACLE_DSN` and set `ORACLE_HOST`, `ORACLE_PORT`, and either `ORACLE_SERVICE` or `ORACLE_SID`. Credentials are plain `.env` values; no custom password encoding is required. Quote a value using normal dotenv syntax if it contains special characters.

Set the actual `SCHEDULER_MASTER_TABLE`, `SCHEDULER_DATEMAST_TABLE`, and `SCHEDULER_DATEMAST_DATE_COLUMN` when office names differ. A schema-qualified table name such as `BANK.DATEMAST` is supported. Optional future holiday tables remain opt-in. The default calendar retains its daily refresh and the Calendar page's manual refresh; Schedule Master refresh remains hourly, with UI edits reflected immediately.

In `scheduler_web/.env`:

```dotenv
SCHEDULER_PROJECT_PATH=../scheduler_project
DJANGO_DATABASE_PATH=runtime/office/portal.sqlite3
SCHEDULER_API_BASE_URL=http://127.0.0.1:8091
SCHEDULER_API_TOKEN=the-same-office-token-used-by-the-worker
```

Use the same `SCHEDULER_API_TOKEN` in both `.env` files and set an office-specific `DJANGO_SECRET_KEY`. The portal receives no Oracle credentials. Relative paths resolve against the application folder, so the parent folder can move. The office example is for the existing loopback development launcher; a shared network deployment needs the office's approved HTTP/HTTPS application-server configuration.

The isolated worker directory holds its own `scheduler.db`, `file_repository/` snapshots, and logs. The isolated portal database holds office users, leave, guides, cover and audit delivery. Keep these paths stable for subsequent office restarts; changing them creates a different local installation.

## Prepare Oracle once, then check

The office Oracle account needs access to its Schedule Master and DATEMAST tables, the schedule-edit permissions used by the UI, and EXECUTE permission for the configured report procedures. Office procedures must support the existing report-date IN and count OUT contract. A changed `.env` cannot create missing tables, procedures or grants.

For current status and full audit integration, the office must have `SCHEDULE_EXTG` and `SCHEDULE_EXTG_LOG` with the schema in `scheduler_project/sql/02_oracle_logging_schema.sql`. Have the DBA provision or migrate those tables while preserving any existing office data. `rebuild_oracle_logging.py` is a separate migration utility: normal startup and the setup check never run it, drop tables or import home schedules. Once the schema is ready, set `SCHEDULER_ORACLE_LOGGING=1` in the worker `.env` and keep `SCHEDULER_EXTG_MIRROR=0`.

From `scheduler_web`, check the selected interpreter, driver, connection and required readable columns:

```bat
start_portal.bat --check
```

Or run `python check_environment.py` using the selected project interpreter. This checks Oracle with SELECT-only probes. It does not execute report procedures, import schedules, write snapshots, alter tables, or test write/EXECUTE grants. When logging is enabled, both logging tables are checked too. Driver, credentials or client failures produce a readable error; selecting `cx_oracle` never silently falls back to `oracledb`.

Initialize the fresh portal database and create the office administrator once:

```bat
..\.scheduler-venv\Scripts\python.exe manage.py migrate
..\.scheduler-venv\Scripts\python.exe manage.py createsuperuser
```

No default password or automatic staff access is added. Existing office databases retain their users; they do not need another `createsuperuser` run.

## Start and refresh

First inspect the office schedules in monitoring mode:

```bat
start_portal.bat --master-source oracle --calendar-source oracle
```

Monitoring does not run report procedures, but UI changes and enabled operational logging are real writes. When ready to run reports, stop the monitoring launcher and start:

```bat
start_portal.bat --execute --master-source oracle --calendar-source oracle
```

Open [the portal](http://127.0.0.1:8010/) or [Django administration](http://127.0.0.1:8010/admin/). The worker reads the selected office Oracle source on startup, then keeps the existing refresh intervals. Normal use needs no manual JSON edits.

After later credential or driver changes, stop the launcher, update `.env`, run `--check`, and start it again. The launcher intentionally reuses a compatible already-running worker, so closing the old launcher matters: starting another copy or refreshing the browser does not change that worker's connection. If a worker was started separately, stop that worker as well after its current task finishes.

## Manage records in Django admin

Sign in with an active Django staff account and the required model permissions. A portal `SUPERUSER` role by itself does not grant Django staff access. A native Django superuser can manage all registered editable models.

| Admin section | Available operations |
| --- | --- |
| Users | Manage accounts, portal roles and authorized Django permissions. |
| Leave requests | Add or edit a pending request; select rows and use Approve, Reject or Cancel actions. Approval checks confirmation coverage. |
| Leave schedule coverages | Assign or change available cover for a pending leave request. Approver/assigner details are recorded automatically. |
| Tasks, assignments and delegations | Manage existing portal task records and their assignments. These are separate from Oracle Schedule Master. |
| Schedule profiles | Edit existing schedules' operator/backup, guide, description and expected duration. Use the supplied link for Oracle schedule definitions and execution controls. |
| Runbook progress, audit logs and audit deliveries | Inspect records. These history/delivery records are read-only. |

To approve leave: create the pending request, add any required schedule coverage, return to the leave list, select the request and choose **Approve selected pending requests**. Approved/rejected/cancelled requests preserve their original details; cancel and create a new request when dates change. Admin writes use the same durable audit delivery and leave validation as the main portal.
