"""Occurrence-scoped runtime for the Oracle-compatible scheduler.

The first Python implementation stored only one STAGING and READY row for a
master job.  That loses real ITRP configurations such as ``DAILY +
FORTNIGHTLY`` when both produce different business/report dates.  This module
uses the durable ``occurrence_key`` (master ID + report date) everywhere the
legacy Oracle package used ``NAME + REPORT_DATE``.

The module deliberately leaves the older :mod:`scheduler.scheduler` class in
place for callers which still use its one-row compatibility API.  The main
worker uses ``OccurrenceScheduler`` so new work is occurrence-scoped without
silently rewriting historic local state.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from datetime_compat import parse_iso_datetime

from models.ready_job import ReadyJob
from models.staging_job import StagingJob
from scheduler.eligibility import EligibilityResult
from scheduler.occurrence import ScheduledOccurrence
from repositories.worker_logging import atomic_logging, ensure_context_schema, capture_occurrence


logger = logging.getLogger("scheduler.occurrence_runtime")


class OccurrenceLifecycle:
    """Persist one planned occurrence without merging sibling frequencies."""

    def __init__(
        self,
        staging_repository,
        ready_repository,
        priority_calculator,
        schedule_extg_repository=None,
        event_logger=None,
    ):
        self.staging_repository = staging_repository
        self.ready_repository = ready_repository
        self.priority_calculator = priority_calculator
        self.schedule_extg_repository = schedule_extg_repository
        self.event_logger = event_logger
        self.logging_source = 'WORKER'

    def synchronize(self, job, result, current_datetime):
        if self.event_logger is None:
            return self._synchronize(job, result, current_datetime)
        connection = self.staging_repository.connection
        if self.ready_repository.connection is not connection:
            raise ValueError("Occurrence state and audit must share one SQLite connection.")
        ensure_context_schema(connection)
        key = getattr(result, 'occurrence_key', None)
        is_new = self.staging_repository.get_by_key(key) is None and self.ready_repository.get_by_key(key) is None
        with atomic_logging(connection):
            stored = self._synchronize(job, result, current_datetime, commit=False)
            capture_occurrence(self.event_logger, connection, {**vars(stored), 'run_config': job.run_config,
                               'same_day': job.same_day, 'package_name': job.package_name,
                               'time_flag': job.time_flag, 'confirmation_required': bool(result.confirmation_required),
                               'confirmation': str(result.confirmation_status or '').upper() == 'CONFIRMED'}, source=self.logging_source)
        if is_new:
            self._ensure_extg_pending(job, stored)
        return stored

    @staticmethod
    def _persist(method, *args, commit=True):
        return method(*args) if commit else method(*args, commit=False)

    def _synchronize(self, job, result, current_datetime, *, commit=True):
        """Store the result for exactly ``result.occurrence_key``.

        ``Schedule_extg`` is created as PENDING as soon as a new local
        occurrence is first observed, before a RUN_BY window opens.  This is
        the same separation used by the old package: staging is durable and
        later cycles may process a past pending report date.
        """

        key = getattr(result, "occurrence_key", None)
        if not key:
            raise ValueError("Occurrence result has no occurrence_key.")

        now = _to_datetime(current_datetime)
        existing_staging = self.staging_repository.get_by_key(key)
        existing_ready = self.ready_repository.get_by_key(key)

        if result.eligible and str(result.state).upper() == "READY":
            self._persist(self.staging_repository.delete_by_key, key, commit=commit)
            ready_job = self._build_ready(
                job=job,
                result=result,
                existing_ready=existing_ready,
                current_datetime=now,
            )
            self._persist(self.ready_repository.save, ready_job, commit=commit)
            if commit and existing_staging is None and existing_ready is None:
                self._ensure_extg_pending(job, ready_job)
            return ready_job

        # A non-ready result remains durable staging.  A stale READY row for
        # this same report-date occurrence must not coexist with it, but all
        # sibling occurrences belonging to the same master job are untouched.
        self._persist(self.ready_repository.delete_by_key, key, commit=commit)
        staging_job = self._build_staging(
            job=job,
            result=result,
            existing_staging=existing_staging,
            current_datetime=now,
        )
        self._persist(self.staging_repository.save, staging_job, commit=commit)
        if commit and existing_staging is None and existing_ready is None:
            self._ensure_extg_pending(job, staging_job)
        return staging_job

    def _build_staging(self, job, result, existing_staging, current_datetime):
        calculated_at = (
            getattr(existing_staging, "calculated_at", None)
            if existing_staging is not None
            else None
        ) or current_datetime.isoformat()
        return StagingJob(
            occurrence_key=result.occurrence_key,
            job_id=getattr(job, "id", None),
            job_name=getattr(job, "name", ""),
            state=result.state,
            occurrence_date=_date_text(result.occurrence_date),
            execution_date=_date_text(result.execution_date),
            t_date=_date_text(result.t_date),
            report_date=_date_text(result.report_date),
            target_date=_date_text(result.target_date),
            margin=getattr(job, "margin", None),
            confirmation_required=int(bool(result.confirmation_required)),
            confirmation_status=result.confirmation_status,
            time_flag=int(_as_bool(getattr(job, "time_flag", 0))),
            from_time=_time_text(result.from_time),
            to_time=_time_text(result.to_time),
            waiting_for=result.waiting_for,
            reason=result.reason,
            next_evaluation=current_datetime.isoformat(),
            calculated_at=calculated_at,
            updated_at=current_datetime.isoformat(),
        )

    def _build_ready(self, job, result, existing_ready, current_datetime):
        priority = self.priority_calculator.calculate(
            job=job,
            report_date=result.report_date,
            current_datetime=current_datetime,
        )
        return ReadyJob(
            occurrence_key=result.occurrence_key,
            job_id=getattr(job, "id", None),
            job_name=getattr(job, "name", ""),
            occurrence_date=_date_text(result.occurrence_date),
            execution_date=_date_text(result.execution_date),
            t_date=_date_text(result.t_date),
            report_date=_date_text(result.report_date),
            target_date=_date_text(result.target_date),
            ready_since=(
                getattr(existing_ready, "ready_since", None)
                if existing_ready is not None
                else current_datetime.isoformat()
            ),
            time_priority=priority.time_priority,
            date_priority=priority.date_priority,
            job_priority=priority.job_priority,
            priority_key=priority.priority_key,
            from_time=_time_text(priority.from_time),
            to_time=_time_text(priority.to_time),
            time_state=priority.time_state,
            updated_at=current_datetime.isoformat(),
        )

    def _ensure_extg_pending(self, job, occurrence):
        """Best-effort initial PENDING mirror; never reopen FAILED here."""

        repository = self.schedule_extg_repository
        if repository is None:
            return None
        name = getattr(job, "name", None)
        report_date = getattr(occurrence, "report_date", None)
        if not name or report_date is None:
            return None
        try:
            existing = repository.get_by_name_report_date(name, report_date)
            if existing is not None:
                return existing
            return repository.create_pending(
                name=name,
                report_date=report_date,
                same_day=getattr(job, "same_day", 0),
                time_flag=getattr(job, "time_flag", 0),
                run_config=getattr(job, "run_config", None),
                confirmation=getattr(job, "confirmation_needed", 0),
            )
        except Exception:
            # The local SQLite occurrence is authoritative for this worker
            # cycle.  A JSON/Oracle status-mirror fault must not block an
            # eligible procedure from being queued later.
            logger.exception(
                "Could not create Schedule_extg PENDING record for %s / %s.",
                name,
                report_date,
            )
            return None


class OccurrenceScheduler:
    """Run all due report-date occurrences without job-level overwrites."""

    ACTIVE = "ACTIVE"

    def __init__(
        self,
        schedule_master_repository,
        job_control_repository,
        occurrence_planner,
        eligibility_evaluator,
        staging_repository,
        ready_repository,
        priority_calculator,
        priority_queue,
        datemast=None,
        schedule_extg_repository=None,
        execution_repository=None,
    ):
        self.schedule_master_repository = schedule_master_repository
        self.job_control_repository = job_control_repository
        self.occurrence_planner = occurrence_planner
        self.eligibility_evaluator = eligibility_evaluator
        self.staging_repository = staging_repository
        self.ready_repository = ready_repository
        self.priority_calculator = priority_calculator
        self.priority_queue = priority_queue
        self.datemast = datemast
        self.execution_repository = execution_repository
        self.lifecycle = OccurrenceLifecycle(
            staging_repository=staging_repository,
            ready_repository=ready_repository,
            priority_calculator=priority_calculator,
            schedule_extg_repository=schedule_extg_repository,
        )

    def run_cycle(self, current_datetime=None):
        """Evaluate every new and persisted occurrence for one worker tick."""

        current_datetime = _to_datetime(current_datetime or datetime.now())
        jobs = list(self.schedule_master_repository.get_active())
        controls = {}
        results = {}
        pending_extg = self._pending_extg_contexts(
            jobs,
            current_datetime.date(),
        )

        for job in jobs:
            self.job_control_repository.ensure_job(job.id)
            controls[job.id] = self.job_control_repository.get(job.id)

        for job in jobs:
            control = controls.get(job.id)
            effective_datetime = self._get_effective_datetime(
                control,
                current_datetime,
            )
            persisted_staging = {
                value.occurrence_key: value
                for value in self.staging_repository.get_by_job_id(job.id)
                if getattr(value, "occurrence_key", None)
            }
            persisted_ready = {
                value.occurrence_key: value
                for value in self.ready_repository.get_by_job_id(job.id)
                if getattr(value, "occurrence_key", None)
            }

            contexts = {
                key: self._context_from_staging(value)
                for key, value in persisted_staging.items()
            }
            for context in pending_extg.get(job.id, []):
                # A legacy/external PENDING Schedule_extg row may predate the
                # local SQLite occurrence table. Bring it back into durable
                # staging so run_date <= today continues to receive RUN_BY
                # evaluation instead of disappearing on migration.
                contexts.setdefault(context.occurrence_key, context)
            for context in self.occurrence_planner.due_occurrences(
                job,
                effective_datetime.date(),
                self.datemast,
            ):
                existing = contexts.get(context.occurrence_key)
                if existing is None or existing.report_date is None or existing.execution_date is None:
                    contexts[context.occurrence_key] = context

            # A manual request executes an existing durable occurrence where
            # possible.  If none exists, construct one deterministic manual
            # context for this date; its report date remains explicit for the
            # Oracle procedure rather than being guessed from DATEMAST.
            if self._is_manual_run(control) and not contexts and not persisted_ready:
                context = self._manual_context(job, effective_datetime.date())
                contexts[context.occurrence_key] = context

            for occurrence_key, context in contexts.items():
                if (
                    not self._is_manual_run(control)
                    and self._has_completed_occurrence(job, context)
                ):
                    # An already successful report date must never re-enter
                    # READY just because the worker ticks again before the
                    # next DATEMAST publication.  This mirrors Oracle's
                    # NAME + REPORT_DATE duplicate protection at the planner
                    # boundary and keeps the queue honest for the UI.
                    self.staging_repository.delete_by_key(occurrence_key)
                    self.ready_repository.delete_by_key(occurrence_key)
                    results[occurrence_key] = EligibilityResult(
                        state="SUCCESS",
                        eligible=False,
                        occurrence_key=occurrence_key,
                        occurrence_date=context.occurrence_date,
                        execution_date=context.execution_date,
                        t_date=context.t_date,
                        report_date=context.report_date,
                        target_date=context.target_date,
                        reason="Scheduled report-date occurrence already completed.",
                        frequency_matched=True,
                        day_allowed=True,
                        datemast_ready=True,
                    )
                    continue

                if occurrence_key in persisted_ready:
                    # READY is durable.  Do not turn it back into STAGING on
                    # a later scheduler tick merely because the RUN_BY window
                    # has moved; ExecutionManager/retry policy owns it now.
                    if occurrence_key in persisted_staging:
                        self.staging_repository.delete_by_key(occurrence_key)
                    results[occurrence_key] = self._ready_result(
                        persisted_ready[occurrence_key]
                    )
                    continue

                result = self.eligibility_evaluator.evaluate_occurrence(
                    job=job,
                    current_datetime=effective_datetime,
                    occurrence=context,
                    control=self._occurrence_control(job.id, occurrence_key, control),
                    actual_datetime=current_datetime,
                )
                results[occurrence_key] = result
                self.lifecycle.synchronize(job, result, effective_datetime)

        queueable_ready = self._refresh_ready_priorities(
            jobs=jobs,
            controls=controls,
            current_datetime=current_datetime,
            results=results,
        )
        self.priority_queue.rebuild(queueable_ready)
        return self._build_summary(jobs, results, current_datetime)

    def _pending_extg_contexts(self, jobs, current_date):
        """Hydrate prior PENDING Schedule_extg rows without guessing dates.

        New executions already have occurrence-scoped SQLite state. This
        recovery path covers an upgrade/restart where a previous worker wrote
        Schedule_extg but stopped before local staging was rebuilt. Failed
        rows are deliberately left to the execution-history retry policy and
        its global retry windows.
        """

        repository = self.lifecycle.schedule_extg_repository
        if repository is None or not hasattr(repository, "get_pending"):
            return {}

        jobs_by_name = {
            str(getattr(job, "name", "")).strip().upper(): job
            for job in jobs
            if getattr(job, "name", None)
        }
        contexts = {}
        try:
            records = repository.get_pending()
        except Exception:
            logger.exception("Could not read PENDING Schedule_extg records.")
            return contexts

        for record in records:
            if not isinstance(record, dict):
                continue
            job = jobs_by_name.get(str(record.get("name", "")).strip().upper())
            if job is None:
                continue
            try:
                report_date = _to_date(record.get("report_date"))
                run_date = _to_date(record.get("run_date")) or report_date
            except (TypeError, ValueError):
                # A malformed status record remains visible to operators but
                # must never make the worker fabricate an Oracle parameter.
                logger.warning(
                    "Ignoring malformed PENDING Schedule_extg record: %r",
                    record.get("id"),
                )
                continue
            if report_date is None or run_date is None or run_date > current_date:
                continue
            key = self.occurrence_planner.occurrence_key(job.id, report_date)
            context = ScheduledOccurrence(
                occurrence_key=key,
                frequency="PERSISTED_EXTG",
                occurrence_date=report_date,
                t_date=report_date,
                report_date=report_date,
                target_date=report_date,
                execution_date=run_date,
                run_date=run_date,
            )
            contexts.setdefault(job.id, []).append(context)

        return contexts

    def _refresh_ready_priorities(
        self,
        jobs,
        controls,
        current_datetime,
        results=None,
    ):
        job_map = {job.id: job for job in jobs}
        queueable = []
        for ready_job in self.ready_repository.get_all():
            job = job_map.get(ready_job.job_id)
            if job is None:
                # Do not delete it merely because an administrator disabled
                # the master row.  Keeping the durable row visible supports
                # audit/re-activation; it is simply excluded from execution.
                continue
            control = controls.get(job.id)
            if self._control_status(control) != self.ACTIVE:
                continue
            effective_datetime = self._get_effective_datetime(
                control,
                current_datetime,
            )

            # A READY row is durable, but it is not a licence to execute
            # outside RUN_BY.  Reapply the occurrence gate every tick so a
            # queued job whose window has elapsed returns to PENDING/STAGING
            # and can be revisited in a later slot, exactly like legacy
            # process_pending_jobs.  This also honours a confirmation/control
            # change made after the row first became READY.
            refreshed = self.eligibility_evaluator.evaluate_occurrence(
                job=job,
                current_datetime=effective_datetime,
                occurrence=self._context_from_ready(ready_job),
                control=self._occurrence_control(job.id, ready_job.occurrence_key, control),
                actual_datetime=current_datetime,
            )
            if not refreshed.eligible:
                self.lifecycle.synchronize(job, refreshed, effective_datetime)
                if results is not None:
                    results[refreshed.occurrence_key] = refreshed
                continue

            report_date = _to_date(getattr(ready_job, "report_date", None))
            if report_date is None:
                continue
            priority = self.priority_calculator.calculate(
                job=job,
                report_date=report_date,
                current_datetime=effective_datetime,
            )
            ready_job.time_priority = priority.time_priority
            ready_job.date_priority = priority.date_priority
            ready_job.job_priority = priority.job_priority
            ready_job.priority_key = priority.priority_key
            ready_job.time_state = priority.time_state
            ready_job.from_time = _time_text(priority.from_time)
            ready_job.to_time = _time_text(priority.to_time)
            ready_job.updated_at = effective_datetime.isoformat()
            self.ready_repository.save(ready_job)
            queueable.append(ready_job)
        return queueable

    def _occurrence_control(self, job_id, occurrence_key, fallback):
        getter = getattr(self.job_control_repository, "get_for_occurrence", None)
        return getter(job_id, occurrence_key) if callable(getter) else fallback

    def _has_completed_occurrence(self, job, context):
        repository = self.execution_repository
        if repository is not None and hasattr(repository, "has_success"):
            try:
                if repository.has_success(job.id, context.report_date):
                    return True
            except Exception:
                logger.exception(
                    "Could not check execution history for occurrence %s.",
                    context.occurrence_key,
                )

        repository = self.lifecycle.schedule_extg_repository
        name = getattr(job, "name", None)
        if (
            name
            and repository is not None
            and hasattr(repository, "get_by_name_report_date")
        ):
            try:
                record = repository.get_by_name_report_date(
                    name,
                    context.report_date,
                )
                return str((record or {}).get("status", "")).upper() == "SUCCESS"
            except Exception:
                logger.exception(
                    "Could not check Schedule_extg status for occurrence %s.",
                    context.occurrence_key,
                )

        return False

    @staticmethod
    def _context_from_staging(staging):
        report_date = _to_date(staging.report_date)
        if report_date is None:
            # Legacy placeholders can have an evaluation/occurrence date but
            # no resolved Oracle report date. Preserve that uncertainty until
            # the authoritative planner supplies a real occurrence.
            return ScheduledOccurrence(
                occurrence_key=staging.occurrence_key, frequency="PERSISTED",
                occurrence_date=_to_date(staging.occurrence_date),
                t_date=_to_date(staging.t_date), report_date=None,
                target_date=_to_date(staging.target_date),
                execution_date=_to_date(staging.execution_date),
                run_date=_to_date(staging.execution_date),
            )
        occurrence_date = _to_date(staging.occurrence_date) or report_date
        t_date = _to_date(staging.t_date) or occurrence_date or report_date
        report_date = report_date or t_date
        target_date = _to_date(staging.target_date) or report_date
        execution_date = _to_date(staging.execution_date) or occurrence_date or report_date
        return ScheduledOccurrence(
            occurrence_key=staging.occurrence_key,
            frequency="PERSISTED",
            occurrence_date=occurrence_date,
            t_date=t_date,
            report_date=report_date,
            target_date=target_date,
            execution_date=execution_date,
            run_date=execution_date,
        )

    @staticmethod
    def _context_from_ready(ready):
        report_date = _to_date(ready.report_date)
        occurrence_date = _to_date(ready.occurrence_date) or report_date
        t_date = _to_date(ready.t_date) or occurrence_date or report_date
        report_date = report_date or t_date
        target_date = _to_date(ready.target_date) or report_date
        execution_date = _to_date(ready.execution_date) or occurrence_date or report_date
        return ScheduledOccurrence(
            occurrence_key=ready.occurrence_key,
            frequency="PERSISTED",
            occurrence_date=occurrence_date,
            t_date=t_date,
            report_date=report_date,
            target_date=target_date,
            execution_date=execution_date,
            run_date=execution_date,
        )

    @staticmethod
    def _manual_context(job, current_date):
        key = "manual:{0}:{1}".format(job.id, current_date.isoformat())
        return ScheduledOccurrence(
            occurrence_key=key,
            frequency="MANUAL",
            occurrence_date=current_date,
            t_date=current_date,
            report_date=current_date,
            target_date=current_date,
            execution_date=current_date,
            run_date=current_date,
        )

    @staticmethod
    def _ready_result(ready_job):
        return EligibilityResult(
            state="READY",
            eligible=True,
            occurrence_key=ready_job.occurrence_key,
            occurrence_date=_to_date(ready_job.occurrence_date),
            execution_date=_to_date(ready_job.execution_date),
            t_date=_to_date(ready_job.t_date),
            report_date=_to_date(ready_job.report_date),
            target_date=_to_date(ready_job.target_date),
            reason="Existing READY occurrence retained.",
            frequency_matched=True,
            day_allowed=True,
            datemast_ready=True,
        )

    def _build_summary(self, jobs, results, current_datetime):
        states = {}
        for result in results.values():
            states[result.state] = states.get(result.state, 0) + 1
        return {
            "timestamp": current_datetime.isoformat(),
            "total_jobs": len(jobs),
            "occurrence_count": len(results),
            "staging_count": self.staging_repository.count(),
            "ready_count": self.ready_repository.count(),
            "queue_size": self.priority_queue.size(),
            "states": states,
        }

    @staticmethod
    def _is_manual_run(control):
        if not control:
            return False
        return _as_bool(control.get("manual_run", 0))

    @staticmethod
    def _control_status(control):
        if not control:
            return "ACTIVE"
        return str(control.get("control_status", "ACTIVE")).strip().upper()

    @staticmethod
    def _get_effective_datetime(control, fallback_datetime):
        fallback_datetime = _to_datetime(fallback_datetime)
        if not control:
            return fallback_datetime
        override = control.get("override_datetime")
        if override is None or (isinstance(override, str) and not override.strip()):
            return fallback_datetime
        try:
            return _to_datetime(override)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid job override_datetime: %r", override)
            return fallback_datetime


def _as_bool(value):
    if isinstance(value, str):
        return value.strip().upper() in {"1", "Y", "YES", "TRUE", "T"}
    return bool(value)


def _date_text(value):
    value = _to_date(value)
    return value.isoformat() if value is not None else None


def _time_text(value):
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return value.strftime("%H:%M")
    return str(value)


def _to_date(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return datetime.fromisoformat(value).date()
    raise TypeError("Unsupported date value: {0!r}".format(value))


def _to_datetime(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return parse_iso_datetime(value.strip())
    raise TypeError("current_datetime must be datetime or ISO datetime string.")
