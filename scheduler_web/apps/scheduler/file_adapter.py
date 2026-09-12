"""Read-side adapters for the standalone scheduler.

The production adapter reads one snapshot from the scheduler-owned control
API.  A read-only local adapter remains available only for development when
the background worker has not been launched yet; it never performs controls,
eligibility calculation, priority-queue work, or Oracle execution.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, time, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .control_client import SchedulerApiClient, SchedulerApiError


class SchedulerProjectReadAdapter:
    def __init__(self, master_path: Path | None = None, state_db_path: Path | None = None):
        self.master_path = Path(master_path or settings.SCHEDULER_MASTER_PATH)
        self.state_db_path = Path(state_db_path or settings.SCHEDULER_STATE_DB_PATH)
        self.errors: list[str] = []

    def get_schedules(self) -> list[dict]:
        master_records = self._read_master()
        state = self._read_state()
        schedules = []
        for record in master_records:
            schedule_id = int(record["id"])
            staging = state["staging"].get(schedule_id)
            ready = state["ready"].get(schedule_id)
            control = dict(state["controls"].get(schedule_id, self._empty_control()))
            current_occurrence = ready or staging or {}
            if "confirmation_confirmed" in current_occurrence:
                control["confirmation"] = bool(current_occurrence["confirmation_confirmed"])
            if control.get("override_datetime") is None:
                control["override_datetime"] = ""
            last_execution = state["last_execution"].get(schedule_id)
            lifecycle_state = "READY" if ready else (staging.get("state") if staging else "IDLE")
            control_status = str(control.get("control_status", "ACTIVE")).upper()
            operational_state = (
                "DISABLED" if not bool(record.get("is_active", 0))
                else control_status if control_status != "ACTIVE"
                else lifecycle_state
            )
            schedules.append({
                "id": schedule_id,
                "name": record.get("name", f"Schedule {schedule_id}"),
                "package_name": record.get("package_name", ""),
                "run_config": record.get("run_config") or {},
                "run_config_display": json.dumps(record.get("run_config") or {}, indent=2),
                "frequencies": ", ".join((record.get("run_config") or {}).get("RUNS_ON", [])) or "Not configured",
                "run_by": self._run_window(record.get("run_config") or {}),
                "max_attempts": (record.get("run_config") or {}).get("MAX_ATTEMPTS", 3),
                "margin": record.get("margin", "T"),
                "same_day": bool(record.get("same_day", 0)),
                "time_flag": bool(record.get("time_flag", 0)),
                "is_active": bool(record.get("is_active", 0)),
                "confirmation_needed": bool(record.get("confirmation_needed", 0)),
                "created_date": self._to_datetime(record.get("created_date")),
                "control": control,
                "lifecycle_state": lifecycle_state,
                "operational_state": operational_state,
                "occurrence": ready or staging,
                "last_execution": last_execution,
            })
        return sorted(schedules, key=lambda schedule: schedule["id"])

    def get_schedule(self, schedule_id: int) -> dict | None:
        return next((schedule for schedule in self.get_schedules() if schedule["id"] == int(schedule_id)), None)

    def get_schedule_occurrence(self, schedule_id, occurrence_key):
        return next((schedule for schedule in self._persisted_schedules()
                     if schedule["id"] == int(schedule_id)
                     and schedule["occurrence"].get("occurrence_key") == occurrence_key), None)

    @staticmethod
    def _state_rows(state, group):
        """Keep complete occurrence lists; maps are only catalog summaries."""
        return state.get(f"{group}_rows", list(state[group].values()))

    def _persisted_schedules(self):
        schedules = {schedule["id"]: schedule for schedule in self.get_schedules()}
        state = self._read_state()
        occurrences = {}
        for group in ("staging", "ready"):
            for row in self._state_rows(state, group):
                schedule_id = int(row["job_id"])
                base = schedules.get(schedule_id)
                if base is None:
                    continue
                lifecycle = "READY" if group == "ready" else row.get("state", "STAGING")
                control = dict(base["control"])
                if "confirmation_confirmed" in row:
                    control["confirmation"] = bool(row["confirmation_confirmed"])
                operational = ("DISABLED" if not base["is_active"] else
                               control["control_status"] if control["control_status"] != "ACTIVE" else lifecycle)
                key = row.get("occurrence_key") or f"{schedule_id}:{row.get('report_date')}"
                occurrences[key] = {**base, "occurrence": row, "control": control,
                                    "lifecycle_state": lifecycle, "operational_state": operational}
        return list(occurrences.values())

    def get_ready(self, report_date: date | None = None) -> list[dict]:
        records = [schedule for schedule in self._persisted_schedules() if schedule["lifecycle_state"] == "READY"]
        if report_date:
            records = [
                schedule for schedule in records
                if self._to_date((schedule.get("occurrence") or {}).get("report_date")) == report_date
            ]
        records = sorted(
            records,
            key=lambda schedule: self._priority_key(schedule["occurrence"].get("priority_key")),
        )
        now = timezone.localtime()
        for position, schedule in enumerate(records, start=1):
            schedule["queue_position"] = position
            schedule["timing"] = self._timing_for(schedule, now=now, in_queue=True)
        return records

    def get_staging(self) -> list[dict]:
        return [schedule for schedule in self._persisted_schedules() if schedule["lifecycle_state"] not in {"READY", "IDLE"}]

    def get_executions(self, *, report_date: date | None = None, status: str | None = None) -> list[dict]:
        records = self._read_state()["executions"]
        if report_date:
            records = [record for record in records if self._to_date(record.get("report_date")) == report_date]
        if status:
            records = [record for record in records if str(record.get("status", "")).upper() == status.upper()]
        return records

    def get_schedule_executions(self, schedule_id: int) -> list[dict]:
        return [record for record in self.get_executions() if int(record.get("job_id", -1)) == int(schedule_id)]

    def get_execution_plan(self, day: date | None = None) -> list[dict]:
        """Return persisted occurrences expected to execute on one operating day.

        This deliberately uses the scheduler-provided ``execution_date``.  It
        does not infer new occurrences from Schedule Master because only the
        standalone scheduler owns eligibility calculation.
        """
        day = day or timezone.localdate()
        now = timezone.localtime()
        plan = []
        for schedule in self._persisted_schedules():
            occurrence = schedule.get("occurrence") or {}
            if self._to_date(occurrence.get("execution_date")) != day:
                continue
            schedule["timing"] = self._timing_for(schedule, now=now)
            plan.append(schedule)
        return sorted(plan, key=lambda schedule: schedule["timing"]["sort_key"])

    def get_upcoming_execution_plan(self, *, after: date | None = None, limit: int = 6) -> list[dict]:
        """Surface the next persisted execution dates without reimplementing scheduling rules."""
        after = after or timezone.localdate()
        now = timezone.localtime()
        plan = []
        for schedule in self._persisted_schedules():
            occurrence = schedule.get("occurrence") or {}
            execution_date = self._to_date(occurrence.get("execution_date"))
            if not execution_date or execution_date <= after:
                continue
            schedule["timing"] = self._timing_for(schedule, now=now)
            plan.append(schedule)
        return sorted(plan, key=lambda schedule: schedule["timing"]["sort_key"])[:limit]

    def get_status(self) -> dict:
        schedules = self.get_schedules()
        executions = self.get_executions()
        return {
            "label": "READ-ONLY DEVELOPMENT FALLBACK" if not self.errors else "UNAVAILABLE",
            "available": not self.errors,
            "source": "Schedule Master + scheduler state (read-only development fallback)",
            "total": len(schedules),
            "active": sum(schedule["is_active"] for schedule in schedules),
            "staging": len(self.get_staging()),
            "ready": len(self.get_ready()),
            "running": sum(execution["status"] == "RUNNING" for execution in executions),
            "failed": sum(execution["status"] == "FAILED" for execution in executions),
            "last_checked": timezone.localtime(),
            "error": self.errors[-1] if self.errors else "",
        }

    def get_calendar_days(self, month: date) -> dict[date, dict]:
        """Return persisted activity grouped by business report date.

        An execution date can differ from report date.  The month grid is
        intentionally anchored on report date so an operator never mistakes a
        later execution date for the report the schedule produced.
        """
        days: dict[date, dict] = {}

        def bucket(value: date) -> dict:
            return days.setdefault(value, {
                "date": value, "occurrences": [], "executions": [],
                "scheduled": 0, "ready": 0, "waiting": 0, "paused": 0,
                "cancelled": 0, "success": 0, "failed": 0, "running": 0,
            })

        for schedule in self._persisted_schedules():
            occurrence = schedule["occurrence"]
            if not occurrence:
                continue
            report_date = self._to_date(occurrence.get("report_date"))
            if not report_date or report_date.year != month.year or report_date.month != month.month:
                continue
            day = bucket(report_date)
            day["occurrences"].append(schedule)
            day["scheduled"] += 1
            state = schedule["operational_state"]
            if state == "READY":
                day["ready"] += 1
            elif state == "PAUSED":
                day["paused"] += 1
            elif state == "CANCELLED":
                day["cancelled"] += 1
            else:
                day["waiting"] += 1

        for execution in self.get_executions():
            report_date = self._to_date(execution.get("report_date"))
            if not report_date or report_date.year != month.year or report_date.month != month.month:
                continue
            day = bucket(report_date)
            day["executions"].append(execution)
            status = str(execution.get("status", "")).upper()
            if status == "SUCCESS":
                day["success"] += 1
            elif status == "FAILED":
                day["failed"] += 1
            elif status == "RUNNING":
                day["running"] += 1
        return days

    def get_calendar_events(self, month: date) -> dict[date, list[dict]]:
        """Compact report-date summaries for smaller dashboard calendar cells."""
        events: dict[date, list[dict]] = {}
        for event_date, summary in self.get_calendar_days(month).items():
            labels = []
            if summary["scheduled"]:
                labels.append(f"{summary['scheduled']} scheduled")
            if summary["success"]:
                labels.append(f"{summary['success']} successful")
            if summary["failed"]:
                labels.append(f"{summary['failed']} failed")
            if summary["running"]:
                labels.append(f"{summary['running']} running")
            kind = "failed" if summary["failed"] else "scheduler"
            events[event_date] = [{
                "label": " · ".join(labels),
                "kind": kind,
                "url": f"/calendar/?month={month:%Y-%m}&date={event_date:%Y-%m-%d}",
            }]
        return events

    def get_execution_day(self, value: date) -> list[dict]:
        """Execution attempts that actually ran on a calendar day, for the day panel."""
        return [
            record for record in self.get_executions()
            if self._to_date(record.get("started_at")) == value
        ]

    def get_operations_calendar(self, start_date, days=31):
        """Persisted-only fallback. Never manufacture future occurrences in Django."""
        end_date = start_date + timedelta(days=days)
        rows = {}
        schedules = {schedule["id"]: schedule for schedule in self.get_schedules()}
        for schedule in self._persisted_schedules():
            occurrence = dict(schedule.get("occurrence") or {})
            key = occurrence.get("occurrence_key") or f"{schedule['id']}:{occurrence.get('report_date')}"
            rows[key] = {**occurrence, "job_id": schedule["id"], "job_name": schedule["name"],
                         "state": schedule["operational_state"],
                         "confirmation_needed": schedule["confirmation_needed"], "source": "persisted",
                         "is_projection": False, "occurrence_key": occurrence.get("occurrence_key", "")}
        # Overlay the latest attempt before selecting the operating day, so a
        # late execution retains its persisted planned date and distinct actual date.
        for execution in reversed(self.get_executions()):
            key = execution.get("occurrence_key") or f"{execution['job_id']}:{execution.get('report_date')}"
            previous = rows.get(key, {})
            schedule = schedules.get(int(execution["job_id"]), {})
            actual = self._to_date(execution.get("started_at"))
            state = str(execution["status"]).upper()
            retry_pending = state == "FAILED" and previous.get("state") == "READY"
            if retry_pending:
                state = "READY"
            elif state not in {"SUCCESS", "RUNNING"} and previous.get("state") in {"PAUSED", "CANCELLED", "DISABLED"}:
                state = previous["state"]
            rows[key] = {**previous, **execution, "state": state, "source": "execution",
                         "occurrence_key": key, "is_projection": False, "retry_pending": retry_pending,
                         "job_name": execution.get("job_name") or schedule.get("name", ""),
                         "confirmation_needed": schedule.get("confirmation_needed", False),
                         "actual_execution_date": actual.isoformat() if actual else None,
                         "execution_date": previous.get("execution_date") or (actual.isoformat() if actual else None)}
        result = []
        for row in rows.values():
            day = self._to_date(row.get("execution_date"))
            if day and start_date <= day < end_date:
                result.append({**row, "calendar_date": day.isoformat()})
        return {"occurrences": result, "calendar": {}, "projection_available": False}

    def _timing_for(self, schedule: dict, *, now: datetime, in_queue: bool = False) -> dict:
        """Presentation-only timing derived from a persisted occurrence.

        The execution manager may choose the highest-priority ready item at a
        scheduler tick, so an exact *start* after entering the queue cannot be
        predicted.  The UI therefore counts down to the eligible window, then
        clearly describes the priority-queue wait.
        """
        occurrence = schedule.get("occurrence") or {}
        execution_date = self._to_date(occurrence.get("execution_date"))
        start_time = self._to_time(occurrence.get("from_time"))
        end_time = self._to_time(occurrence.get("to_time"))
        if start_time is None or end_time is None:
            window = (schedule.get("run_config") or {}).get("RUN_BY") or {}
            start_time = start_time or self._to_time(window.get("FROM_TIME") or window.get("FROM"))
            end_time = end_time or self._to_time(window.get("TO_TIME") or window.get("TO"))

        zone = timezone.get_current_timezone()
        start_at = timezone.make_aware(datetime.combine(execution_date, start_time), zone) if execution_date and start_time else None
        end_at = timezone.make_aware(datetime.combine(execution_date, end_time), zone) if execution_date and end_time else None
        if start_at and end_at and end_at <= start_at:
            end_at += timedelta(days=1)

        window_label = (
            f"{start_time.strftime('%H:%M')} – {end_time.strftime('%H:%M')}"
            if start_time and end_time else "No time restriction"
        )
        info = {
            "execution_date": execution_date,
            "window_label": window_label,
            "start_at": start_at.isoformat() if start_at else "",
            "end_at": end_at.isoformat() if end_at else "",
            "countdown_target": "",
            "countdown_prefix": "",
            "label": "Execution date has not been calculated yet.",
            "tone": "muted",
            "sort_key": start_at or (timezone.make_aware(datetime.combine(execution_date, time.max), zone) if execution_date else now + timedelta(days=36500)),
        }
        if not execution_date:
            return info

        if start_at and now < start_at:
            info.update({
                "countdown_target": start_at.isoformat(),
                "countdown_prefix": "Eligible in",
                "label": f"Eligible from {start_time.strftime('%H:%M')}",
                "tone": "upcoming",
            })
            return info

        if end_at and start_at and start_at <= now < end_at:
            if in_queue:
                info.update({
                    "countdown_target": end_at.isoformat(),
                    "countdown_prefix": "Window closes in",
                    "label": "Ready — waiting for priority slot",
                    "tone": "ready",
                })
            else:
                info.update({
                    "countdown_target": end_at.isoformat(),
                    "countdown_prefix": "Window closes in",
                    "label": "Execution window is open",
                    "tone": "active",
                })
            return info

        if end_at and now >= end_at:
            info.update({
                "label": f"Window ended at {end_time.strftime('%H:%M')}",
                "tone": "elapsed",
            })
            return info

        if in_queue:
            info.update({"label": "Ready — waiting for priority slot", "tone": "ready"})
        elif execution_date == now.date():
            info.update({"label": "Eligible today — awaiting scheduler cycle", "tone": "active"})
        else:
            info.update({"label": f"Planned for {execution_date:%d/%m/%Y}", "tone": "upcoming"})
        return info

    def _read_master(self) -> list[dict]:
        try:
            with self.master_path.open("r", encoding="utf-8") as source:
                records = json.load(source)
            return records if isinstance(records, list) else []
        except (OSError, json.JSONDecodeError) as error:
            self.errors.append(f"Schedule Master is unavailable: {error}")
            return []

    def _read_state(self) -> dict:
        result = {"controls": {}, "staging": {}, "ready": {}, "staging_rows": [], "ready_rows": [],
                  "executions": [], "last_execution": {}}
        connection = None
        try:
            connection = sqlite3.connect(f"{self.state_db_path.resolve().as_uri()}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            result["controls"] = {row["job_id"]: dict(row) for row in connection.execute("SELECT * FROM job_control")}
            approvals = {row["occurrence_key"]: bool(row["confirmed"])
                         for row in connection.execute("SELECT occurrence_key, confirmed FROM occurrence_confirmation")} if "occurrence_confirmation" in tables else {}
            for group in ("staging", "ready"):
                # Once migrated, old tables are archives, including when the
                # authoritative occurrence table is currently empty.
                table = f"{group}_occurrences" if f"{group}_occurrences" in tables else f"{group}_jobs"
                rows = [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY report_date, job_id")]
                for row in rows:
                    if table.endswith("_occurrences"):
                        confirmed = approvals.get(row.get("occurrence_key"), False)
                        row.update(confirmation=confirmed, confirmation_confirmed=confirmed)
                    result[group].setdefault(row["job_id"], row)
                result[f"{group}_rows"] = rows
            result["executions"] = [dict(row) for row in connection.execute("SELECT * FROM execution_jobs ORDER BY started_at DESC, id DESC")]
            for execution in result["executions"]:
                if "occurrence_confirmation" in tables:
                    key = execution.get("occurrence_key") or f"{execution['job_id']}:{execution.get('report_date')}"
                    execution["confirmation_confirmed"] = approvals.get(key, False)
                result["last_execution"].setdefault(execution["job_id"], execution)
        except (OSError, sqlite3.Error) as error:
            self.errors.append(f"Scheduler state is unavailable: {error}")
        finally:
            if connection is not None:
                connection.close()
        return result

    @staticmethod
    def _empty_control() -> dict:
        return {"control_status": "ACTIVE", "manual_run": 0, "confirmation": 0, "override_datetime": None}

    @staticmethod
    def _run_window(run_config: dict) -> str:
        window = run_config.get("RUN_BY") or run_config.get("BY_TIME") or {}
        start = window.get("FROM_TIME") or window.get("FROM")
        end = window.get("TO_TIME") or window.get("TO")
        return f"{start} – {end}" if start and end else "No time restriction"

    @staticmethod
    def _priority_key(value) -> tuple:
        try:
            return tuple(json.loads(value)) if isinstance(value, str) else tuple(value or ())
        except (TypeError, ValueError, json.JSONDecodeError):
            return (999999,)

    @staticmethod
    def _to_date(value) -> date | None:
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
        except ValueError:
            try:
                return date.fromisoformat(str(value))
            except ValueError:
                return None

    @staticmethod
    def _to_datetime(value) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _to_time(value) -> time | None:
        if isinstance(value, time):
            return value
        if not value:
            return None
        text = str(value).strip()
        for pattern in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(text, pattern).time()
            except ValueError:
                continue
        return None


class SchedulerApiReadAdapter(SchedulerProjectReadAdapter):
    """Normalize a scheduler-owned API snapshot into the existing UI shape."""

    def __init__(self, client: SchedulerApiClient | None = None, local_fallback=None):
        # The base adapter methods provide all presentation-only date and
        # countdown logic.  These placeholder paths are never used remotely.
        super().__init__()
        self.client = client or SchedulerApiClient()
        self.local_fallback = local_fallback
        self._snapshot: dict | None = None
        self._snapshot_attempted = False
        self._fallback_active = False

    def _load_snapshot(self) -> dict | None:
        if self._snapshot_attempted:
            return self._snapshot
        self._snapshot_attempted = True
        try:
            self._snapshot = self.client.snapshot()
            return self._snapshot
        except SchedulerApiError as error:
            self.errors.append(str(error))
            self._fallback_active = self.local_fallback is not None
            return None

    def get_operations_calendar(self, start_date, days=31):
        if self._load_snapshot() is None:
            return super().get_operations_calendar(start_date, days)
        if not hasattr(self, "_calendar_cache"):
            self._calendar_cache = {}
        key = (start_date, days)
        if key not in self._calendar_cache:
            try:
                self._calendar_cache[key] = {**self.client.calendar(start_date, days), "projection_available": True}
            except SchedulerApiError as error:
                self._calendar_cache[key] = {**super().get_operations_calendar(start_date, days), "error": str(error)}
        return self._calendar_cache[key]

    def get_schedule_occurrence(self, schedule_id, occurrence_key):
        snapshot = self._load_snapshot()
        if snapshot is None:
            return super().get_schedule_occurrence(schedule_id, occurrence_key)
        for row in snapshot.get("staging", []) + snapshot.get("ready", []):
            if row.get("occurrence_key") == occurrence_key and int(row["job_id"]) == int(schedule_id):
                schedule = self.get_schedule(schedule_id)
                if schedule is None:
                    return None
                schedule["occurrence"] = row
                schedule["lifecycle_state"] = row.get("state", "READY")
                schedule["operational_state"] = schedule["lifecycle_state"] if schedule["control"]["control_status"] == "ACTIVE" else schedule["control"]["control_status"]
                schedule["control"]["confirmation"] = bool(row.get("confirmation_confirmed", row.get("confirmation", False)))
                return schedule
        return None

    def get_ready(self, report_date=None):
        snapshot = self._load_snapshot()
        if snapshot is None or "priority_queue" not in snapshot:
            return super().get_ready(report_date)
        schedules = {row["id"]: row for row in self.get_schedules()}
        result = []
        for position, row in enumerate(snapshot.get("priority_queue", []), 1):
            if report_date and self._to_date(row.get("report_date")) != report_date:
                continue
            schedule = dict(schedules.get(int(row["job_id"]), {}))
            schedule.update(id=int(row["job_id"]), name=row.get("job_name") or schedule.get("name"),
                            occurrence=row, queue_position=position, lifecycle_state="READY", operational_state="READY")
            schedule["timing"] = self._timing_for(schedule, now=timezone.localtime(), in_queue=True)
            result.append(schedule)
        return result

    def _read_master(self) -> list[dict]:
        snapshot = self._load_snapshot()
        if snapshot is not None:
            records = snapshot.get("schedule_master", [])
            return records if isinstance(records, list) else []
        return self.local_fallback._read_master() if self.local_fallback else []

    def _read_state(self) -> dict:
        snapshot = self._load_snapshot()
        if snapshot is None:
            return self.local_fallback._read_state() if self.local_fallback else {
                "controls": {}, "staging": {}, "ready": {}, "executions": [], "last_execution": {},
            }
        controls = {
            int(item["job_id"]): item
            for item in snapshot.get("controls", [])
            if item.get("job_id") is not None
        }
        staging = {
            int(item["job_id"]): item
            for item in snapshot.get("staging", [])
            if item.get("job_id") is not None
        }
        ready = {
            int(item["job_id"]): item
            for item in snapshot.get("ready", [])
            if item.get("job_id") is not None
        }
        executions = snapshot.get("executions", [])
        latest = {}
        for execution in executions:
            latest.setdefault(execution.get("job_id"), execution)
        return {
            "controls": controls,
            "staging": staging,
            "ready": ready,
            "staging_rows": snapshot.get("staging", []),
            "ready_rows": snapshot.get("ready", []),
            "executions": executions,
            "last_execution": latest,
        }

    def _service_control_from_snapshot(self, snapshot: dict) -> dict:
        """Normalize scheduler-wide control state without inventing one."""
        raw = snapshot.get("service_control")
        if not isinstance(raw, dict):
            return {
                "available": False,
                "scheduler_enabled": None,
                "updated_at": None,
                "updated_by": "",
                "reason": "",
            }
        enabled = raw.get("scheduler_enabled")
        return {
            "available": enabled is not None,
            "scheduler_enabled": bool(enabled) if enabled is not None else None,
            "updated_at": self._to_datetime(raw.get("updated_at")) or raw.get("updated_at"),
            "updated_by": raw.get("updated_by") or "",
            "reason": raw.get("reason") or "",
        }

    def _operation_audit_from_snapshot(self, snapshot: dict) -> list[dict]:
        raw_entries = snapshot.get("operation_audit")
        if not isinstance(raw_entries, list):
            return []
        entries = []
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            entry = dict(raw)
            entry["occurred_at"] = self._to_datetime(raw.get("occurred_at")) or raw.get("occurred_at")
            entries.append(entry)
        return entries

    def get_upcoming_execution_plan(self, *, after: date | None = None, limit: int = 6) -> list[dict]:
        """Use an optional scheduler-owned future-occurrence list when offered.

        Older scheduler versions expose only current STAGING/READY rows.  In
        that case the inherited persisted-occurrence view remains the safe
        fallback; Django never derives dates from frequency rules itself.
        """
        snapshot = self._load_snapshot()
        if snapshot is None or "upcoming" not in snapshot:
            return super().get_upcoming_execution_plan(after=after, limit=limit)

        entries = snapshot.get("upcoming")
        if not isinstance(entries, list):
            return super().get_upcoming_execution_plan(after=after, limit=limit)

        after = after or timezone.localdate()
        schedules_by_id = {schedule["id"]: schedule for schedule in self.get_schedules()}
        now = timezone.localtime()
        plan = []

        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                schedule_id = int(entry.get("job_id", entry.get("id")))
            except (TypeError, ValueError):
                continue

            execution_date = self._to_date(entry.get("execution_date"))
            if not execution_date or execution_date <= after:
                continue

            base = dict(schedules_by_id.get(schedule_id, {}))
            occurrence = dict(base.get("occurrence") or {})
            occurrence.update({
                "job_id": schedule_id,
                "job_name": entry.get("job_name") or base.get("name") or f"Schedule {schedule_id}",
                "occurrence_date": entry.get("occurrence_date"),
                "report_date": entry.get("report_date"),
                "execution_date": entry.get("execution_date"),
                "from_time": entry.get("from_time"),
                "to_time": entry.get("to_time"),
                "state": entry.get("state"),
            })
            lifecycle_state = str(entry.get("state") or base.get("lifecycle_state") or "PLANNED").upper()
            # A forecast's state is newer than an incidental current READY or
            # STAGING row. Explicit operator/master controls remain visible.
            base_operational_state = str(base.get("operational_state") or "").upper()
            operational_state = (
                base_operational_state
                if base_operational_state in {"PAUSED", "CANCELLED", "DISABLED"}
                else lifecycle_state
            )
            schedule = {
                **base,
                "id": schedule_id,
                "name": entry.get("job_name") or base.get("name") or f"Schedule {schedule_id}",
                "occurrence": occurrence,
                "lifecycle_state": lifecycle_state,
                "operational_state": operational_state,
                "upcoming_source": "SCHEDULER_FORECAST",
            }
            schedule["timing"] = self._timing_for(schedule, now=now)
            plan.append(schedule)

        return sorted(plan, key=lambda schedule: schedule["timing"]["sort_key"])[:limit]

    def get_status(self) -> dict:
        status = super().get_status()
        snapshot = self._load_snapshot()
        if snapshot is not None:
            meta = snapshot.get("meta", {})
            status.update({
                "label": "BACKGROUND SCHEDULER API",
                "available": True,
                "source": "Scheduler-owned control API · monitoring snapshot",
                "last_checked": self._to_datetime(meta.get("generated_at")) or timezone.localtime(),
                "queue_size": meta.get("queue_size", status["ready"]),
                "queue_revision": meta.get("queue_revision", ""),
                "calendar": snapshot.get("calendar", {}),
                "mode": meta.get("mode", "service"),
                "master_source": meta.get("master_source", ""),
                "oracle_logging": meta.get("oracle_logging") or {},
                "execution_enabled": meta.get("execution_enabled", True),
                "cycle_status": meta.get("cycle_status", "UNKNOWN"),
                "last_cycle_error": meta.get("last_cycle_error"),
                "scheduler_interval_seconds": meta.get("scheduler_interval_seconds"),
                "last_cycle": meta.get("last_cycle"),
                "next_cycle_at": meta.get("next_cycle_at") or "",
                "service_control": self._service_control_from_snapshot(snapshot),
                "operation_audit": self._operation_audit_from_snapshot(snapshot),
                "error": "",
            })
        elif self._fallback_active:
            status.update({
                "label": "READ-ONLY DEVELOPMENT FALLBACK",
                "available": True,
                "source": "Local scheduler files · controls remain disabled until the background API starts",
                "error": self.errors[-1] if self.errors else "",
            })
        return status


def get_scheduler_read_adapter():
    """Use the scheduler API whenever configured; local reads are development-only."""
    client = SchedulerApiClient()
    fallback = SchedulerProjectReadAdapter() if settings.SCHEDULER_ALLOW_LOCAL_READ_FALLBACK else None
    if client.configured:
        return SchedulerApiReadAdapter(client=client, local_fallback=fallback)
    if fallback:
        return fallback
    return SchedulerApiReadAdapter(client=client, local_fallback=None)
