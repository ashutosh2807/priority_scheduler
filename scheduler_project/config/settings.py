import os
from pathlib import Path


# Project root
BASE_DIR = Path(__file__).resolve().parent.parent

# Load before computing paths: standalone main.py must honor .env just as the
# combined launcher does. Explicit process/launcher values retain precedence.
try:
    from dotenv import load_dotenv
except ImportError:
    pass
else:
    load_dotenv(BASE_DIR / ".env")

# Keep each deployment's queue, outbox and source snapshots together. The
# default retains the existing layout; a relative value follows the engine.
_data_directory = os.getenv("SCHEDULER_DATA_DIR", "").strip()
DATA_DIR = Path(os.path.expandvars(os.path.expanduser(_data_directory))) if _data_directory else BASE_DIR
if not DATA_DIR.is_absolute():
    DATA_DIR = BASE_DIR / DATA_DIR
DATA_DIR = DATA_DIR.resolve()


# ---------------------------------------------------------
# SQLite
# ---------------------------------------------------------

SQLITE_DB = DATA_DIR / "scheduler.db"


# ---------------------------------------------------------
# File repository
# ---------------------------------------------------------

FILE_REPOSITORY = DATA_DIR / "file_repository"

SCHEDULE_MASTER_FILE = FILE_REPOSITORY / "Schedule_Master.json"
SCHEDULE_EXTG_FILE = FILE_REPOSITORY / "Schedule_extg.json"
DATEMASTER_FILE = FILE_REPOSITORY / "datemaster.json"
HOLIDAY_MASTER_FILE = FILE_REPOSITORY / "holiday_master.json"


# ---------------------------------------------------------
# Scheduler
# ---------------------------------------------------------

# Scheduler cycles every five minutes by default, matching the Windows Task
# Scheduler deployment.  A controlled environment variable is useful for
# test environments and does not change the single-worker safety rules.
SCHEDULER_INTERVAL_SECONDS = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "300"))
# Successful DATEMAST reads are cached through the local calendar day. This
# interval applies only to recovery after a failed read, never to normal polls.
# The old environment name remains a fallback for existing deployments.
CALENDAR_RETRY_SECONDS = max(30, int(os.getenv(
    "SCHEDULER_CALENDAR_RETRY_SECONDS", os.getenv("SCHEDULER_CALENDAR_REFRESH_SECONDS", "300"),
)))

# The worker selects at most one highest-priority READY occurrence per tick.
# Other READY occurrences remain persistent and visible to the monitoring UI
# with their real priority position until the next selection cycle.
MAX_EXECUTIONS_PER_CYCLE = 1


# Default total automatic attempts for one scheduled occurrence.
# Each task can override this with RUN_CONFIG.MAX_ATTEMPTS (1..100).
#
# Example:
#
#     attempt 1 -> FAILED -> retry
#     attempt 2 -> FAILED -> retry
#     attempt 3 -> FAILED -> retry exhausted
#
# The limit applies to scheduled executions only.
# Manual executions are not limited by this setting.
MAX_SCHEDULED_ATTEMPTS = 3


# Failed occurrences can retry throughout their planned execution day,
# but only on their original planned execution day. The lookback bounds
# report-date candidate reads; it never authorizes a run after execution day.
# Past execution days require an explicit manual request.
RETRY_LOOKBACK_DAYS = max(1, int(os.getenv("SCHEDULER_RETRY_LOOKBACK_DAYS", "15")))
MAX_RETRY_EXECUTIONS_PER_CYCLE = max(1, int(os.getenv("SCHEDULER_MAX_RETRY_EXECUTIONS_PER_CYCLE", "1")))
RETRY_WINDOWS = os.getenv("SCHEDULER_RETRY_WINDOWS", "ALL_DAY")


# Normal working hours
WORK_START_TIME = "10:00"
WORK_END_TIME = "20:00"


# ---------------------------------------------------------
# Logging
# ---------------------------------------------------------

LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "scheduler.log"


# Create directories if they don't exist
FILE_REPOSITORY.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
