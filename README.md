# SBI Oracle Scheduler — start here

From this `django_app` folder, run **`run_portal.bat`**. It delegates to `scheduler_web/start_portal.bat` and starts both services. You can also run `scheduler_web/start_portal.bat` directly.

Open [the portal](http://127.0.0.1:8010/) and sign in. The worker API is on port 8091; the portal is on port 8010.

The combined launcher defaults to **monitoring mode**: it reads Oracle and evaluates schedules without calling report procedures. Schedule definition changes and controls are still saved. To enable real procedure execution, stop monitoring and run `run_portal.bat --execute`.

| Folder | Responsibility |
| --- | --- |
| `scheduler_web/` | Django UI, users, leave, confirmation assignments, guides and portal audit. |
| `scheduler_project/` | Oracle access, DATEMAST calendar, scheduling, live queue, execution and worker history. |
| `.scheduler-venv/` | Shared Python dependencies. |

**Full instructions:** [Running the project](scheduler_web/RUNNING_THE_PROJECT.md). This is the current reference for exact commands, configuration, which files to keep, optional imports, execution modes and shutdown.

On a new computer, run `setup_scheduler_environment.bat` once after configuring the two applications as described in that guide. Existing users and Oracle configuration are retained; setup is not needed on every launch.

Only one worker should run for this installation. The combined launcher manages both services, so you do not need to start `main.py`, `manage.py`, service batches or five-minute cycle tasks separately.
