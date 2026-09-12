"""Authenticated, scheduler-owned control and monitoring API.

The Django portal must not open the scheduler database or mutate derived
state.  This module is hosted by the scheduler process and is the only
control-plane surface exposed to an operations UI.  It deliberately exposes
observation, validated schedule definitions and ``job_control`` intents.
DATEMAST remains a read-only Oracle input, with an explicit refresh action.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import threading
import uuid
from contextlib import contextmanager, nullcontext
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from config.settings import SCHEDULER_INTERVAL_SECONDS
from scheduler.calendar_projection import build_calendar
from scheduler.holiday import HolidayEvaluator
from repositories.master_configuration_repository import (
    MasterConfigurationError,
    MasterConfigurationNotFound,
)


logger = logging.getLogger("scheduler.control_api")

_CONFIG_UNSET = object()


class StaleOperationError(ValueError):
    """The browser's occurrence or queue view no longer matches the worker."""


class SchedulerControlApi:
    """Thread-safe scheduler control plane backed by the live application."""

    ACTIONS = {
        "pause",
        "resume",
        "cancel",
        "activate",
        "manual_run",
        "clear_manual_run",
        "confirm",
        "clear_confirmation",
        "reset",
        "set_override",
        "clear_override",
    }

    def __init__(self, application, lock=None):
        self.application = application
        self.lock = lock or threading.RLock()
        self.started_at = datetime.now()
        self.last_cycle = None

    @property
    def operations_repository(self):
        """Optional persistent service control/audit repository.

        Kept optional so the API remains compatible with small test harnesses
        and old local databases while normal application construction supplies
        it.
        """
        return self.application.get("operations_repository")

    def record_cycle(self, summary, next_cycle_at=None):
        with self.lock:
            self.last_cycle = summary
            self.next_cycle_at = next_cycle_at

    def logging_status(self):
        with self.lock:
            repository = self.application.get("oracle_logging")
            return repository.stats() if repository is not None else {"enabled": False, "pending": 0}

    def accept_logging_events(self, events):
        """Acknowledge only durably accepted portal events; Oracle replays separately."""
        if not isinstance(events, list) or not 1 <= len(events) <= 200:
            raise ValueError("Provide between 1 and 200 logging events.")
        for event in events:
            if not isinstance(event, dict) or not event.get("event_id"):
                raise ValueError("Each logging event needs its original event_id.")
            uuid.UUID(str(event["event_id"]))
            if event.get("source") != "PORTAL" or event.get("record_key"):
                raise ValueError("This endpoint accepts portal audit events only.")
        with self.lock:
            repository = self.application.get("oracle_logging")
            if repository is None:
                raise RuntimeError("Durable Oracle logging is unavailable.")
            if repository.connection.in_transaction:
                raise RuntimeError("Finish the active worker transaction before accepting audit events.")
            from repositories.worker_logging import atomic_logging
            with atomic_logging(repository.connection):
                accepted = [repository.enqueue(event, commit=False) for event in events]
            return {"accepted": accepted}

    @contextmanager
    def _journal_write(self, action, job_id, changes, actor, reason):
        """Persist intent before the independent Oracle definition transaction."""
        repository = self.application.get("oracle_logging")
        journal = {}
        event = {
            "event_type": action + "_REQUESTED", "source": "CONTROL_API",
            "job_id": job_id, "actor": actor, "reason": reason,
            "correlation_id": uuid.uuid4().hex, "payload": {"requested": changes},
        }
        if repository is not None:
            repository.enqueue(event)
        try:
            yield journal
        except Exception as error:
            if repository is not None:
                repository.enqueue({**event, "event_type": action + "_FAILED",
                    "payload": {"requested": changes, "error_type": type(error).__name__}})
            raise
        else:
            if repository is not None:
                repository.enqueue({**event, "event_type": action + "_COMMITTED",
                    "payload": journal.get("result") or {"requested": changes}})

    def record_cycle_failure(self, error):
        summary = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "mode": self.application.get("mode", "worker"),
            "execution_enabled": self.application.get("execution_enabled", True),
                    "master_source": getattr(self.application.get("schedule_master_repository"), "source", "file"),
            "scheduler": {"status": "ERROR"},
            "error": {"type": type(error).__name__, "detail": "Scheduler cycle failed; see scheduler.log for details."},
        }
        with self.lock:
            repository = self.application.get("oracle_logging")
            if repository is not None:
                repository.enqueue({"event_type": "SCHEDULER_CYCLE_FAILED",
                    "source": "MONITOR" if self.application.get("execution_enabled") is False else "WORKER",
                    "status": "ERROR", "payload": summary})
            self.record_cycle(summary)
        return summary

    def snapshot(self, include_upcoming=True):
        """Return a JSON-safe read of the scheduler's authoritative state."""
        with self.lock:
            # Keep scheduler models for the forecast engine.  Serialising
            # first turns ScheduleMaster objects into dictionaries, and the
            # occurrence planner intentionally reads model attributes such as
            # ``is_active`` and ``run_config``.  The response remains JSON
            # safe below; only the internal planner receives model objects.
            schedule_models = self.application["schedule_master_repository"].get_all()
            schedules = [
                self._schedule_payload(job)
                for job in schedule_models
            ]
            controls = self.application["job_control_repository"].get_all()
            staging = [
                self._model_payload(job)
                for job in self.application["staging_repository"].get_all()
            ]
            ready = [
                self._model_payload(job)
                for job in self.application["ready_repository"].get_all()
            ]
            approval_getter = getattr(self.application["job_control_repository"], "get_occurrence_confirmations", None)
            approvals = approval_getter() if callable(approval_getter) else []
            approval_map = {row["occurrence_key"]: bool(row["confirmed"]) for row in approvals}
            confirmation_jobs = {str(job.id) for job in schedule_models if bool(job.confirmation_needed)}
            for row in staging + ready:
                row["confirmation"] = approval_map.get(row.get("occurrence_key"), False)
                row["confirmation_confirmed"] = row["confirmation"]
                row["confirmation_status"] = (
                    "CONFIRMED" if row["confirmation"] else "WAITING_CONFIRMATION"
                ) if str(row["job_id"]) in confirmation_jobs else "NOT_REQUIRED"
            active_controls = {str(row["job_id"]): row for row in controls}
            active_jobs = {str(job.id) for job in schedule_models if bool(job.is_active)}
            queue = [
                self._model_payload(item)
                for item in self.application["priority_queue"].get_all()
                if str(item.job_id) in active_jobs
                and active_controls.get(str(item.job_id), {}).get("control_status", "ACTIVE") == "ACTIVE"
                and (str(item.job_id) not in confirmation_jobs or approval_map.get(item.occurrence_key, False))
            ]
            executions = [
                self._model_payload(job)
                for job in self.application["execution_repository"].get_all()
            ]
            upcoming_planner = self.application.get("upcoming_planner")
            upcoming = (
                upcoming_planner.build(schedule_models, start_date=date.today())
                if upcoming_planner is not None and include_upcoming
                else []
            )
            operations_repository = self.operations_repository
            service_control = (
                operations_repository.get_scheduler_control()
                if operations_repository is not None
                else {"scheduler_enabled": 1}
            )
            operation_audit = (
                operations_repository.get_recent_audit(limit=100)
                if operations_repository is not None
                else []
            )
            return {
                "meta": {
                    "service": "scheduler-control-api",
                    "service_started_at": self.started_at.isoformat(timespec="seconds"),
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "scheduler_interval_seconds": self.application.get("cycle_interval_seconds", SCHEDULER_INTERVAL_SECONDS),
                    "mode": self.application.get("mode", "worker"),
                    "execution_enabled": self.application.get("execution_enabled", True),
                    "master_source": getattr(self.application.get("schedule_master_repository"), "source", "file"),
                    "last_cycle": self.last_cycle,
                    "cycle_status": "ERROR" if (self.last_cycle or {}).get("error") else (
                        (self.last_cycle or {}).get("scheduler", {}).get("status", "OK") if self.last_cycle else "STARTING"
                    ),
                    "last_cycle_error": (self.last_cycle or {}).get("error"),
                    "next_cycle_at": self._json_value(getattr(self, "next_cycle_at", None)),
                    "queue_size": len(queue),
                    "oracle_logging": self.logging_status(),
                    "queue_revision": self._queue_revision(queue),
                    "scheduler_enabled": bool(service_control.get("scheduler_enabled", 1)),
                },
                "service_control": service_control,
                "schedule_master": schedules,
                "controls": controls,
                "occurrence_confirmations": approvals,
                "staging": staging,
                "ready": ready,
                "priority_queue": queue,
                "executions": executions,
                "upcoming": upcoming,
                "operation_audit": operation_audit,
                "calendar": self.calendar_metadata(),
            }

    def calendar_metadata(self):
        datemast = self.application.get("datemast")
        dates = datemast.get_report_dates() if datemast is not None else []
        published_dates = [value for value in dates if value < date.today()]
        latest = max(published_dates) if published_dates else None
        holiday_evaluator = self._holiday_evaluator()
        holiday_evaluator.reload()
        holiday_count = len(holiday_evaluator.get_holidays())
        has_holiday_snapshot = getattr(holiday_evaluator, "_file_signature", None) is not None or holiday_count > 0
        holiday_source = self.application.get("holiday_source") or (
            "local_snapshot" if has_holiday_snapshot else "unavailable"
        )
        if holiday_source == "retained_snapshot" and not has_holiday_snapshot:
            holiday_source = "unavailable"
        coverage_start = getattr(datemast, "coverage_start", None)
        coverage_end = getattr(datemast, "coverage_end", None)
        observed_through = date.today() - timedelta(days=1)
        if coverage_end is not None:
            observed_through = min(observed_through, coverage_end)
        inference_available = bool(published_dates) and bool(getattr(datemast, "available", True))
        if coverage_start is not None and coverage_start > observed_through:
            inference_available = False
        return {
            "source": "oracle" if self.application.get("calendar_snapshot_repository") is not None else "file",
            "authority": "DATEMAST",
            "latest_report_date": self._json_value(latest),
            "source_latest_report_date": self._json_value(max(dates) if dates else None),
            "effective_report_date": self._json_value(datemast.get_previous_report_date(date.today())) if datemast is not None else None,
            "report_dates": self._json_value(dates),
            "coverage_start": self._json_value(coverage_start),
            "coverage_end": self._json_value(coverage_end),
            "available": bool(published_dates),
            "daily_rule": "Previous DATEMAST report date, strictly before the execution day (T+1).",
            "financial_year_end": "03-31",
            "half_year_ends": ["03-31", "09-30"],
            "quarter_ends": ["03-31", "06-30", "09-30", "12-31"],
            "last_refresh_at": self.application.get("calendar_last_refresh_at"),
            "refresh_policy": "daily",
            "next_refresh_at": self.application.get("calendar_next_refresh_at"),
            "refresh_error": self.application.get("calendar_refresh_error"),
            "snapshot_storage": "memory" if self.application.get("execution_enabled") is False else "file",
            "holiday_source": holiday_source,
            "holiday_count": holiday_count,
            "oracle_holiday_source_configured": bool(getattr(self.application.get("calendar_snapshot_repository"), "holiday_table", None)),
            "holiday_rules": "DATEMAST within the loaded range through T-1; Sunday and 2nd/4th Saturday defaults",
            "holiday_observed_from": self._json_value(coverage_start) if inference_available else None,
            "holiday_observed_through": observed_through.isoformat() if inference_available else None,
            "holiday_inference_available": inference_available,
            "holiday_rule_source": "datemast_and_bank_calendar" if inference_available else "bank_calendar",
        }

    def _holiday_evaluator(self):
        evaluator = self.application.get("holiday_evaluator")
        if evaluator is None:
            evaluator = getattr(self.application.get("upcoming_planner"), "holiday_evaluator", None)
        evaluator = evaluator if evaluator is not None else HolidayEvaluator()
        if self.application.get("datemast") is not None:
            evaluator.bind_datemast(self.application["datemast"])
        return evaluator

    def calendar(self, start_date=None, days=31):
        start = date.fromisoformat(str(start_date)) if start_date else date.today()
        days = int(days)
        if not 1 <= days <= 62:
            raise ValueError("Choose a calendar range between 1 and 62 days.")
        with self.lock:
            snapshot = self.snapshot(include_upcoming=False)
            planner = self.application.get("upcoming_planner")
            jobs = self.application["schedule_master_repository"].get_all()
            projections = planner.build(jobs, start_date=start, days=days, limit=None) if planner else []
            result = build_calendar(snapshot, projections, start, days, snapshot["calendar"])
            evaluator = self._holiday_evaluator()
            calendar_days = []
            for offset in range(days):
                day = start + timedelta(days=offset)
                calendar_days.append(evaluator.classify_date(day))
            result["calendar_days"] = calendar_days
            return result

    @staticmethod
    def _queue_revision(queue):
        return hashlib.sha256(json.dumps(queue, sort_keys=True, default=str).encode()).hexdigest()[:24]

    def reorder_queue(self, occurrence_keys, queue_revision, actor=None, reason=None):
        if not isinstance(occurrence_keys, list) or any(not isinstance(key, str) for key in occurrence_keys):
            raise ValueError("occurrence_keys must be a list of queue occurrence keys.")
        if len(set(occurrence_keys)) != len(occurrence_keys):
            raise ValueError("Include each queue occurrence exactly once.")
        with self.lock:
            snapshot = self.snapshot(include_upcoming=False)
            current = snapshot["priority_queue"]
            if not queue_revision or queue_revision != snapshot["meta"]["queue_revision"]:
                raise StaleOperationError("The live queue changed. Refresh before changing its order.")
            if set(occurrence_keys) != {row["occurrence_key"] for row in current}:
                raise StaleOperationError("Include every current queue occurrence exactly once.")
            if self.operations_repository is None:
                raise RuntimeError("Persistent queue controls are unavailable.")
            self.operations_repository.set_queue_order(occurrence_keys, actor, reason)
            queue = self.application["priority_queue"]
            queue.apply_operator_order()
            updated = self.snapshot(include_upcoming=False)
            return {"priority_queue": updated["priority_queue"], "queue_revision": updated["meta"]["queue_revision"]}

    def control(self, job_id, action, override_datetime=None, actor=None, reason=None, occurrence_key=None):
        """Record one validated job-control intent; never manipulate READY/heap."""
        action = str(action).lower()
        if action not in self.ACTIONS:
            raise ValueError("That scheduler control action is not supported.")

        with self.lock:
            repository = self.application["job_control_repository"]
            job_id = int(job_id)
            schedules = self.application["schedule_master_repository"].get_all()
            schedule = next(
                (candidate for candidate in schedules if str(getattr(candidate, "id", "")) == str(job_id)),
                None,
            )
            if schedule is None:
                raise LookupError("The requested schedule does not exist in Scheduler Master.")
            if action == "manual_run" and any(
                int(row.job_id) == job_id and str(row.status).upper() == "RUNNING"
                for row in self.application["execution_repository"].get_all()
            ):
                raise StaleOperationError("This job is running. Wait for completion before requesting another manual run.")
            confirmation_action = action in {"confirm", "clear_confirmation"}
            if occurrence_key is not None or (confirmation_action and bool(schedule.confirmation_needed)):
                pending = [row for name in ("staging_repository", "ready_repository")
                           for row in self.application[name].get_all()
                           if int(row.job_id) == job_id and getattr(row, "occurrence_key", None)]
                if occurrence_key is None:
                    keys = {row.occurrence_key for row in pending}
                    if len(keys) != 1:
                        raise StaleOperationError("Choose the exact pending occurrence to confirm.")
                    occurrence_key = next(iter(keys))
                occurrence = next((row for row in pending if row.occurrence_key == occurrence_key), None)
                if occurrence is None:
                    raise StaleOperationError("This occurrence is no longer pending. Refresh the task list.")
                if action not in {"pause", "resume", "cancel", "activate"} and any(int(row.job_id) == job_id and str(row.report_date)[:10] == str(occurrence.report_date)[:10]
                       and str(row.status).upper() in {"RUNNING", "SUCCESS"}
                       for row in self.application["execution_repository"].get_all()):
                    raise StaleOperationError("This occurrence is already running or completed.")
            get_control = getattr(repository, "get", None)
            before = get_control(job_id) if callable(get_control) else None
            if confirmation_action and occurrence_key:
                before = repository.get_for_occurrence(job_id, occurrence_key)
            transaction_factory = getattr(repository, "transaction", None)
            transactional = callable(transaction_factory)
            with transaction_factory() if transactional else nullcontext():
                if action == "pause":
                    after = repository.pause(job_id)
                elif action == "resume":
                    after = repository.resume(job_id)
                elif action == "cancel":
                    after = repository.cancel(job_id)
                elif action == "activate":
                    after = repository.activate(job_id)
                elif action == "manual_run":
                    if override_datetime:
                        repository.set_override_datetime(job_id, override_datetime)
                    after = repository.request_manual_run(job_id)
                elif action == "clear_manual_run":
                    after = repository.clear_manual_run(job_id)
                elif action == "confirm":
                    after = (repository.set_occurrence_confirmation(job_id, occurrence_key, True)
                             if occurrence_key else repository.confirm(job_id))
                elif action == "clear_confirmation":
                    after = (repository.set_occurrence_confirmation(job_id, occurrence_key, False)
                             if occurrence_key else repository.clear_confirmation(job_id))
                elif action == "reset":
                    after = repository.reset(job_id)
                elif action == "set_override":
                    if not override_datetime:
                        raise ValueError("Choose the scheduler evaluation date and time.")
                    after = repository.set_override_datetime(job_id, override_datetime)
                else:
                    after = repository.clear_override_datetime(job_id)

                if self.operations_repository is not None:
                    self.operations_repository.record_audit(
                        action=f"JOB_{action.upper()}",
                        target_type="occurrence" if occurrence_key else "schedule_master",
                        target_id=occurrence_key or job_id,
                        target_label=getattr(schedule, "name", f"Job {job_id}"),
                        actor=actor,
                        reason=reason,
                        before_state=before,
                        after_state=after,
                        source="CONTROL_API",
                        **({"commit": False} if transactional else {}),
                    )
            self._refresh_monitor_state()
            return after

    def _refresh_monitor_state(self):
        evaluate = self.application.get("monitor_evaluate")
        if callable(evaluate):
            # This callback is configured only by --monitor. It runs the
            # scheduler eligibility pipeline and cannot dispatch Oracle work.
            try:
                self.record_cycle(evaluate())
            except Exception as error:
                self.record_cycle_failure(error)
                raise
        observer = self.application.get("logging_observer")
        if observer is not None:
            observer.capture(self.application, source="CONTROL_API")

    def service_control(self, action, actor=None, reason=None):
        """Stop/start future cycles without terminating active Oracle work."""
        action = str(action).strip().lower()
        if action not in {"stop", "start"}:
            raise ValueError("Service control supports only start or stop.")
        with self.lock:
            if self.operations_repository is None:
                raise RuntimeError("The scheduler service-control store is unavailable.")
            result = self.operations_repository.set_scheduler_enabled(
                enabled=(action == "start"), actor=actor, reason=reason
            )
            self._refresh_monitor_state()
            return result

    def refresh_calendar(self, actor=None, reason=None):
        with self.lock:
            refresher = self.application.get("refresh_calendar")
            if not callable(refresher):
                raise ValueError("Oracle calendar refresh is unavailable in this service.")
            before = {"last_refresh_at": self.application.get("calendar_last_refresh_at")}
            result = refresher()
            if self.operations_repository is not None:
                self.operations_repository.record_audit(
                    action="CALENDAR_REFRESHED" if result.get("refreshed") else "CALENDAR_REFRESH_FAILED",
                    target_type="calendar", target_id="DATEMAST", target_label="Bank calendar",
                    actor=actor, reason=reason, before_state=before,
                    after_state=result, source="CONTROL_API",
                )
            return result

    def _definition_repository(self):
        from repositories.schedule_definition_repository import ScheduleDefinitionRepository

        existing = self.application.get("master_configuration_repository")
        if existing is None:
            raise RuntimeError("Schedule editing is not available in this service.")
        return ScheduleDefinitionRepository(
            existing.schedule_master_repository, source=existing.source,
            connection_factory=existing.connection_factory, table_name=existing.table_name,
        )

    def save_definition(self, job_id, payload, actor=None, reason=None):
        if not isinstance(payload, dict):
            raise MasterConfigurationError("Schedule definition must be a JSON object.")
        changes = {key: value for key, value in payload.items() if key not in {"actor", "reason"}}
        with self.lock:
            repository = self._definition_repository()
            creating = job_id is None
            target_id = repository._normalise_job_id(changes.get("id") if creating else job_id)
            self._assert_definition_idle(target_id, creating=creating)
            with self._journal_write("SCHEDULE_CREATE" if creating else "SCHEDULE_UPDATE", target_id, changes, actor, reason) as journal:
                result = repository.create(changes) if creating else repository.update_definition(target_id, changes)
                journal["result"] = result
            if not creating:
                # Preserve durable planned dates; discarding them could make
                # yesterday's failed report look eligible for today.
                queue = self.application.get("priority_queue")
                if queue is not None:
                    queue.remove(job_id=target_id)
            self._audit_definition("SCHEDULE_CREATED" if creating else "SCHEDULE_UPDATED", result, actor, reason)
            self._refresh_monitor_state()
            return result

    def delete_definition(self, job_id, actor=None, reason=None):
        with self.lock:
            repository = self._definition_repository()
            job_id = repository._normalise_job_id(job_id)
            self._assert_definition_idle(job_id)
            with self._journal_write("SCHEDULE_DELETE", job_id, {}, actor, reason) as journal:
                result = repository.delete_definition(job_id)
                journal["result"] = result
            observer = self.application.get("logging_observer")
            if observer is not None:
                observer.before_delete(job_id, actor=actor, reason=reason)
            self._clear_derived_schedule(job_id)
            self._audit_definition("SCHEDULE_DELETED", result, actor, reason)
            self._refresh_monitor_state()
            return result

    def _assert_definition_idle(self, job_id, creating=False):
        # Load the authoritative source/cache before a write, so a successful
        # Oracle commit can be reflected in memory if snapshot refresh fails.
        self.application["schedule_master_repository"].get_all()
        repository = self.application.get("execution_repository")
        records = repository.get_all() if repository is not None else []
        for row in records:
            if str(getattr(row, "job_id", "")) != str(job_id):
                continue
            if creating:
                raise MasterConfigurationError("This schedule ID has execution history. Choose a new ID to preserve that history.")
            if str(getattr(row, "status", "")).upper() == "RUNNING":
                raise StaleOperationError("This schedule is running. Wait for completion before changing or deleting its definition.")

    def _clear_derived_schedule(self, job_id):
        # Only derived, pending work is invalidated. The next cycle evaluates
        # it against the new master. Execution, controls, confirmations and
        # Schedule_extg/audit records retain their history.
        for name in ("staging_repository", "ready_repository"):
            repository = self.application.get(name)
            if repository is not None:
                repository.delete(job_id)
        queue = self.application.get("priority_queue")
        if queue is not None:
            queue.remove(job_id=job_id)

    def _audit_definition(self, action, result, actor, reason):
        if self.operations_repository is not None:
            record = result.get("after") or result.get("before") or {}
            self.operations_repository.record_audit(
                action=action, target_type="schedule_master", target_id=result["id"],
                target_label=record.get("name", record.get("NAME", f"Job {result['id']}")),
                actor=actor, reason=reason, before_state=result.get("before"),
                after_state=result.get("after"), source="CONTROL_API",
            )

    def configure_master(self, job_id, payload, actor=None, reason=None):
        """Update the approved Scheduler Master operational configuration.

        This is intentionally separate from temporary job controls. A pause,
        cancel, or manual run belongs in ``job_control``; an active flag or a
        RUN_BY or attempt-limit change belongs in Scheduler Master and is persisted to the
        selected file/Oracle source by the bounded repository.
        """
        if not isinstance(payload, dict):
            raise MasterConfigurationError(
                "The configuration request body must be a JSON object."
            )
        allowed = {"is_active", "run_by", "max_attempts", "actor", "reason"}
        unknown = set(payload).difference(allowed)
        if unknown:
            raise MasterConfigurationError(
                "Only is_active, run_by, and max_attempts can be changed through this endpoint."
            )
        is_active = payload.get("is_active", _CONFIG_UNSET)
        run_by = payload.get("run_by", _CONFIG_UNSET)
        max_attempts = payload.get("max_attempts", _CONFIG_UNSET)
        if is_active is _CONFIG_UNSET and run_by is _CONFIG_UNSET and max_attempts is _CONFIG_UNSET:
            raise MasterConfigurationError(
                "Choose an active state, execution window, or automatic attempt limit to update."
            )

        with self.lock:
            repository = self.application.get("master_configuration_repository")
            if repository is None:
                raise RuntimeError(
                    "Scheduler Master configuration is not available in this service."
                )
            # Do not pass this module's sentinel into the repository: the
            # repository owns its own sentinel so omitted fields must simply
            # be absent from the call.
            changes = {}
            if is_active is not _CONFIG_UNSET:
                changes["is_active"] = is_active
            if run_by is not _CONFIG_UNSET:
                changes["run_by"] = run_by
            if max_attempts is not _CONFIG_UNSET:
                changes["max_attempts"] = max_attempts
            with self._journal_write("MASTER_CONFIGURATION_UPDATE", int(job_id), changes, actor, reason) as journal:
                result = repository.update(job_id, **changes)
                journal["result"] = result
            if self.operations_repository is not None:
                self.operations_repository.record_audit(
                    action="MASTER_CONFIGURATION_UPDATED",
                    target_type="schedule_master",
                    target_id=result["id"],
                    target_label=self._master_job_label(result["id"]),
                    actor=actor,
                    reason=reason,
                    before_state=result.get("before"),
                    after_state=result.get("after"),
                    source="CONTROL_API",
                )
            self._refresh_monitor_state()
            return result

    def _master_job_label(self, job_id):
        try:
            schedules = self.application["schedule_master_repository"].get_all()
        except Exception:
            return f"Job {job_id}"
        schedule = next(
            (
                candidate
                for candidate in schedules
                if str(getattr(candidate, "id", "")) == str(job_id)
            ),
            None,
        )
        return getattr(schedule, "name", None) or f"Job {job_id}"

    @staticmethod
    def _schedule_payload(job):
        return {
            "id": job.id,
            "name": job.name,
            "package_name": job.package_name,
            "run_config": job.run_config or {},
            "margin": job.margin,
            "same_day": job.same_day,
            "time_flag": job.time_flag,
            "is_active": job.is_active,
            "created_date": job.created_date,
            "confirmation_needed": job.confirmation_needed,
        }

    @classmethod
    def _model_payload(cls, model):
        return {
            key: cls._json_value(value)
            for key, value in vars(model).items()
            if not key.startswith("_")
        }

    @staticmethod
    def _json_value(value):
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, tuple):
            return [SchedulerControlApi._json_value(item) for item in value]
        if isinstance(value, list):
            return [SchedulerControlApi._json_value(item) for item in value]
        if isinstance(value, dict):
            return {key: SchedulerControlApi._json_value(item) for key, item in value.items()}
        return value


def start_control_api(application, control_plane, host=None, port=None):
    """Start a local HTTP server in a daemon thread and return it.

    The API binds only to loopback unless a scheduler administrator explicitly
    configures another host.  Set ``SCHEDULER_API_TOKEN`` before binding a
    non-loopback address.
    """
    host = host or os.environ.get("SCHEDULER_API_HOST", "127.0.0.1")
    port = int(os.environ.get("SCHEDULER_API_PORT", "8091") if port is None else port)
    token = os.environ.get("SCHEDULER_API_TOKEN", "")
    if host not in {"127.0.0.1", "localhost", "::1"} and not token:
        raise RuntimeError("SCHEDULER_API_TOKEN is required for a non-loopback control API.")

    class Handler(BaseHTTPRequestHandler):
        server_version = "SchedulerControlAPI/1.0"

        def log_message(self, format, *args):  # pragma: no cover - stdlib hook
            logger.info("Control API %s - %s", self.client_address[0], format % args)

        def _authorized(self):
            return not token or self.headers.get("X-Scheduler-Token") == token

        def _write(self, status, payload):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlparse(self.path).path.rstrip("/") or "/"
            if path == "/health":
                self._write(HTTPStatus.OK, {
                    "status": "degraded" if (control_plane.last_cycle or {}).get("error") else "ok",
                    "service": "scheduler-control-api",
                    "mode": control_plane.application.get("mode", "worker"),
                    "execution_enabled": control_plane.application.get("execution_enabled", True),
                })
                return
            if not self._authorized():
                self._write(HTTPStatus.UNAUTHORIZED, {"detail": "Invalid scheduler API token."})
                return
            if path == "/v1/operations/snapshot":
                self._write(HTTPStatus.OK, control_plane.snapshot())
                return
            if path == "/v1/logging/status":
                self._write(HTTPStatus.OK, control_plane.logging_status())
                return
            if path == "/v1/operations/calendar":
                try:
                    query = parse_qs(urlparse(self.path).query)
                    result = control_plane.calendar(query.get("start_date", [None])[0], query.get("days", [31])[0])
                    self._write(HTTPStatus.OK, result)
                except (TypeError, ValueError) as error:
                    self._write(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
                return
            self._write(HTTPStatus.NOT_FOUND, {"detail": "Unknown control API route."})

        def do_POST(self):
            path = urlparse(self.path).path.rstrip("/")
            if not self._authorized():
                self._write(HTTPStatus.UNAUTHORIZED, {"detail": "Invalid scheduler API token."})
                return
            parts = path.split("/")
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 4 * 1024 * 1024:
                    raise ValueError("The request exceeds the supported size.")
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("The control request body must be a JSON object.")
                actor = body.get("actor")
                reason = body.get("reason")
                if path == "/v1/logging/events":
                    self._write(HTTPStatus.OK, control_plane.accept_logging_events(body.get("events")))
                    return
                if path == "/v1/jobs":
                    result = control_plane.save_definition(None, body, actor=actor, reason=reason)
                    self._write(HTTPStatus.CREATED, {"schedule": result, "message": "Schedule created."})
                    return
                if path == "/v1/operations/calendar/refresh":
                    result = control_plane.refresh_calendar(actor=actor, reason=reason)
                    self._write(HTTPStatus.OK, {"calendar": result, "message": "Calendar refresh completed."})
                    return
                if path == "/v1/queue/reorder":
                    result = control_plane.reorder_queue(body.get("occurrence_keys"), body.get("queue_revision"), actor, reason)
                    self._write(HTTPStatus.OK, result)
                    return
                if len(parts) == 6 and parts[:3] == ["", "v1", "jobs"] and parts[4] == "controls":
                    job_id = int(parts[3])
                    action = parts[5]
                    result = control_plane.control(
                        job_id,
                        action,
                        body.get("override_datetime"),
                        actor=actor,
                        reason=reason,
                        occurrence_key=body.get("occurrence_key"),
                    )
                    self._write(HTTPStatus.OK, {"control": result, "message": "Control intent recorded."})
                    return
                if len(parts) == 5 and parts[:3] == ["", "v1", "scheduler"] and parts[3] == "controls":
                    result = control_plane.service_control(parts[4], actor=actor, reason=reason)
                    self._write(
                        HTTPStatus.OK,
                        {
                            "service_control": result,
                            "message": "Future scheduler cycles were updated safely.",
                        },
                    )
                    return
                self._write(HTTPStatus.NOT_FOUND, {"detail": "Unknown control API route."})
            except StaleOperationError as error:
                self._write(HTTPStatus.CONFLICT, {"detail": str(error)})
            except LookupError as error:
                self._write(HTTPStatus.NOT_FOUND, {"detail": str(error)})
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                self._write(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
            except Exception:
                logger.exception("Control API action failed")
                self._write(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "Scheduler could not record the control intent."})

        def do_PATCH(self):
            """Apply the narrowly-scoped Scheduler Master configuration update."""
            path = urlparse(self.path).path.rstrip("/")
            if not self._authorized():
                self._write(HTTPStatus.UNAUTHORIZED, {"detail": "Invalid scheduler API token."})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict):
                    raise MasterConfigurationError(
                        "The configuration request body must be a JSON object."
                    )
                parts = path.split("/")
                if len(parts) == 5 and parts[:3] == ["", "v1", "jobs"] and parts[4] == "definition":
                    result = control_plane.save_definition(int(parts[3]), body, actor=body.get("actor"), reason=body.get("reason"))
                    self._write(HTTPStatus.OK, {"schedule": result, "message": "Schedule updated."})
                    return
                if (
                    len(parts) != 5
                    or parts[:3] != ["", "v1", "jobs"]
                    or parts[4] != "configuration"
                ):
                    self._write(HTTPStatus.NOT_FOUND, {"detail": "Unknown control API route."})
                    return
                result = control_plane.configure_master(
                    int(parts[3]),
                    body,
                    actor=body.get("actor"),
                    reason=body.get("reason"),
                )
                self._write(
                    HTTPStatus.OK,
                    {
                        "configuration": result,
                        "message": "Scheduler Master configuration was updated.",
                    },
                )
            except StaleOperationError as error:
                self._write(HTTPStatus.CONFLICT, {"detail": str(error)})
            except (MasterConfigurationNotFound, LookupError) as error:
                self._write(HTTPStatus.NOT_FOUND, {"detail": str(error)})
            except (MasterConfigurationError, TypeError, ValueError, json.JSONDecodeError) as error:
                self._write(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
            except Exception:
                logger.exception("Scheduler Master configuration action failed")
                self._write(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"detail": "Scheduler could not update the Scheduler Master configuration."},
                )

        def do_DELETE(self):
            if not self._authorized():
                self._write(HTTPStatus.UNAUTHORIZED, {"detail": "Invalid scheduler API token."})
                return
            try:
                parts = urlparse(self.path).path.rstrip("/").split("/")
                if len(parts) != 4 or parts[:3] != ["", "v1", "jobs"]:
                    self._write(HTTPStatus.NOT_FOUND, {"detail": "Unknown control API route."})
                    return
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(body, dict) or set(body) - {"actor", "reason"}:
                    raise MasterConfigurationError("Deletion accepts only an actor and reason.")
                result = control_plane.delete_definition(int(parts[3]), actor=body.get("actor"), reason=body.get("reason"))
                self._write(HTTPStatus.OK, {"schedule": result, "message": "Schedule deleted. Execution history is retained."})
            except StaleOperationError as error:
                self._write(HTTPStatus.CONFLICT, {"detail": str(error)})
            except LookupError as error:
                self._write(HTTPStatus.NOT_FOUND, {"detail": str(error)})
            except (TypeError, ValueError) as error:
                self._write(HTTPStatus.BAD_REQUEST, {"detail": str(error)})
            except Exception:
                logger.exception("Schedule deletion failed")
                self._write(HTTPStatus.INTERNAL_SERVER_ERROR, {"detail": "Scheduler could not delete the schedule."})

    server = ThreadingHTTPServer((host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, name="scheduler-control-api", daemon=True)
    thread.start()
    logger.info("Scheduler control API listening on http://%s:%s", host, server.server_port)
    return server
