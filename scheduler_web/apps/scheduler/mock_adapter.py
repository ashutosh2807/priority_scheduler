from copy import deepcopy
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from .adapter import SchedulerAdapter


class MockSchedulerAdapter(SchedulerAdapter):
    """Deterministic in-process data for UI work before the scheduler API exists."""
    def __init__(self):
        today = date.today()
        self.jobs = {
            104: {"id": 104, "name": "Treasury Liquidity Report", "status": "READY", "report_date": today, "run_date": today, "scheduled_time": time(9, 30), "attempt": 1, "priority": 1, "confirmation": False, "paused": False, "same_day": True, "records_loaded": None, "error": "", "last_run": None, "override_datetime": None, "run_config": '{"RUNS_ON": ["DAILY"], "BY_TIME": {"FROM": "09:00", "TO": "10:00"}}'},
            108: {"id": 108, "name": "Regulatory Exception Extract", "status": "WAITING_CONFIRMATION", "report_date": today, "run_date": today, "scheduled_time": time(10, 0), "attempt": 0, "priority": 2, "confirmation": True, "paused": False, "same_day": True, "records_loaded": None, "error": "", "last_run": None, "override_datetime": None, "run_config": '{"RUNS_ON": ["DAILY"], "CONFIRMATION": true}'},
            115: {"id": 115, "name": "GL Reconciliation", "status": "RUNNING", "report_date": today, "run_date": today, "scheduled_time": time(10, 15), "attempt": 1, "priority": 3, "confirmation": False, "paused": False, "same_day": False, "records_loaded": None, "error": "", "last_run": timezone.now() - timedelta(minutes=4), "override_datetime": None, "run_config": '{"RUNS_ON": ["DAILY"], "MARGIN": "T+1"}'},
            121: {"id": 121, "name": "Nostro Position Extract", "status": "FAILED", "report_date": today - timedelta(days=1), "run_date": today, "scheduled_time": time(8, 45), "attempt": 3, "priority": 4, "confirmation": False, "paused": False, "same_day": False, "records_loaded": 0, "error": "Oracle execution failed: source file unavailable.", "last_run": timezone.now() - timedelta(hours=1), "override_datetime": None, "run_config": '{"RUNS_ON": ["WEEKLY"], "RUN_BY": {"FROM_TIME": "08:30", "TO_TIME": "09:30"}}'},
            134: {"id": 134, "name": "Branch MIS Consolidation", "status": "PAUSED", "report_date": today, "run_date": today + timedelta(days=1), "scheduled_time": time(11, 0), "attempt": 0, "priority": 5, "confirmation": False, "paused": True, "same_day": False, "records_loaded": None, "error": "", "last_run": timezone.now() - timedelta(days=1), "override_datetime": None, "run_config": '{"RUNS_ON": ["MONTHLY"], "SAME_DAY": 0}'},
            142: {"id": 142, "name": "Cash Forecast Summary", "status": "SUCCESS", "report_date": today, "run_date": today, "scheduled_time": time(7, 30), "attempt": 1, "priority": 6, "confirmation": False, "paused": False, "same_day": True, "records_loaded": 2841, "error": "", "last_run": timezone.now() - timedelta(hours=2), "override_datetime": None, "run_config": '{"RUNS_ON": ["DAILY"]}'},
        }

    def get_status(self):
        values = list(self.jobs.values())
        return {"label": "ONLINE", "running": sum(j["status"] == "RUNNING" for j in values), "ready": sum(j["status"] == "READY" for j in values), "failed": sum(j["status"] == "FAILED" for j in values), "paused": sum(j["status"] == "PAUSED" for j in values), "last_checked": timezone.localtime()}

    def get_jobs(self):
        return sorted(deepcopy(list(self.jobs.values())), key=lambda job: job["id"])

    def get_job(self, job_id):
        job = self.jobs.get(int(job_id))
        return deepcopy(job) if job else None

    def get_queue(self):
        return sorted([job for job in self.get_jobs() if job["status"] == "READY"], key=lambda job: job["priority"])

    def get_executions(self):
        records = []
        for job in self.get_jobs():
            if job["last_run"]:
                records.append({"id": job["id"] * 10 + job["attempt"], "job_id": job["id"], "job_name": job["name"], "status": job["status"] if job["status"] in {"SUCCESS", "FAILED", "RUNNING"} else "SUCCESS", "attempt": max(job["attempt"], 1), "report_date": job["report_date"], "started_at": job["last_run"], "finished_at": None if job["status"] == "RUNNING" else job["last_run"] + timedelta(seconds=38), "records_loaded": job["records_loaded"], "error": job["error"]})
        return sorted(records, key=lambda record: record["started_at"], reverse=True)

    def control(self, job_id, action, override_datetime=None):
        job = self.jobs.get(int(job_id))
        if job is None:
            return None
        if action == "pause": job.update(status="PAUSED", paused=True)
        elif action == "resume": job.update(status="READY", paused=False)
        elif action == "confirm": job.update(status="READY", confirmation=False)
        elif action == "clear_confirmation": job.update(status="WAITING_CONFIRMATION", confirmation=True)
        elif action == "cancel": job.update(status="CANCELLED", paused=False)
        elif action == "reset": job.update(status="READY", attempt=0, error="", records_loaded=None)
        elif action == "manual_run": job.update(status="READY", override_datetime=override_datetime or job["override_datetime"])
        elif action == "set_override": job["override_datetime"] = override_datetime
        elif action == "clear_override": job["override_datetime"] = None
        else: return None
        return deepcopy(job)


_adapter = MockSchedulerAdapter()


def get_mock_adapter() -> MockSchedulerAdapter:
    return _adapter
