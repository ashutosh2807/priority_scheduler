"""Local worker audit events; Oracle delivery is owned by the outbox flusher."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def as_dict(value):
    if value is None:
        return {}
    return dict(value) if hasattr(value, "keys") else dict(vars(value))


def json_value(value):
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def table_exists(connection, table):
    return connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


@contextmanager
def atomic_logging(connection):
    """Keep caller transactions intact; rollback business state with its event."""
    nested = connection.in_transaction
    connection.execute("SAVEPOINT worker_logging")
    try:
        yield
    except Exception:
        connection.execute("ROLLBACK TO worker_logging")
        connection.execute("RELEASE worker_logging")
        raise
    else:
        connection.execute("RELEASE worker_logging")
        if not nested:
            connection.commit()


def ensure_context_schema(connection):
    connection.execute("""CREATE TABLE IF NOT EXISTS worker_execution_context (
        execution_id INTEGER PRIMARY KEY, payload TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS worker_observed_occurrences (
        occurrence_key TEXT PRIMARY KEY, payload TEXT NOT NULL
    )""")
    connection.execute("""CREATE TABLE IF NOT EXISTS worker_logging_backfill (
        source_type TEXT PRIMARY KEY, last_source_id INTEGER NOT NULL DEFAULT 0
    )""")


def execution_context(connection, job_id, report_date):
    context = {}
    for table in ("ready_occurrences", "staging_occurrences", "ready_jobs", "staging_jobs"):
        if not table_exists(connection, table):
            continue
        row = connection.execute(
            f"SELECT * FROM {table} WHERE job_id=? AND report_date IS ? LIMIT 1", (job_id, report_date),
        ).fetchone()
        if row is not None:
            context = as_dict(row)
            break
    context["planned_execution_date"] = context.get("execution_date")
    context["report_date"] = report_date
    context["job_id"] = job_id
    if not context.get("occurrence_key") and report_date is not None:
        context["occurrence_key"] = f"{job_id}:{report_date}"
    if context.get("occurrence_key") and table_exists(connection, "worker_observed_occurrences"):
        previous = connection.execute("SELECT payload FROM worker_observed_occurrences WHERE occurrence_key=?", (context["occurrence_key"],)).fetchone()
        if previous:
            context = {**json.loads(previous[0]), **{key: value for key, value in context.items() if value is not None}}
            for field in ("_logging_signature", "execution", "execution_id", "attempt_no", "executed_at", "records_loaded", "error_info", "last_run"):
                context.pop(field, None)
    context["planned_execution_date"] = context.get("planned_execution_date") or context.get("execution_date")
    if table_exists(connection, "job_control"):
        control = connection.execute("SELECT * FROM job_control WHERE job_id=?", (job_id,)).fetchone()
        context["control"] = as_dict(control)
        context["manual_run"] = bool(context["control"].get("manual_run"))
        context["confirmation"] = bool(context["control"].get("confirmation"))
        if context.get("occurrence_key") and table_exists(connection, "occurrence_confirmation"):
            approval = connection.execute("SELECT confirmed FROM occurrence_confirmation WHERE occurrence_key=? AND job_id=?",
                                          (context["occurrence_key"], job_id)).fetchone()
            context["confirmation"] = bool(approval[0]) if approval else False
    if context.get("manual_run") and table_exists(connection, "scheduler_operation_audit"):
        row = connection.execute("""SELECT id, actor, reason FROM scheduler_operation_audit
            WHERE action='JOB_MANUAL_RUN' AND target_id IN (?, ?) ORDER BY id DESC LIMIT 1""",
            (str(job_id), context.get("occurrence_key", "")),
        ).fetchone()
        if row:
            context.update(request_audit_id=row["id"], actor=row["actor"], reason=row["reason"])
    return json_value(context)


def saved_execution_context(connection, execution):
    row = connection.execute("SELECT payload FROM worker_execution_context WHERE execution_id=?", (execution["id"],)).fetchone()
    if row:
        return json.loads(row[0])
    context = execution_context(connection, execution["job_id"], execution.get("report_date"))
    connection.execute("INSERT INTO worker_execution_context VALUES (?, ?)", (execution["id"], json.dumps(context)))
    return context


def occurrence_event(connection, record, *, status=None, source="WORKER", actor=None, reason=None, event_type="OCCURRENCE_STATE_CHANGED"):
    record = json_value(as_dict(record))
    key = record.get("occurrence_key")
    if not key:
        return None
    effective_status = status or record.get("state") or "READY"
    # The newest real attempt wins over stale queue rows. Administrative
    # controls affect pending work; they cannot undo success or stop an
    # already running procedure.
    if table_exists(connection, "execution_jobs"):
        latest = connection.execute("""SELECT * FROM execution_jobs WHERE job_id=?
            AND report_date IS ? ORDER BY id DESC LIMIT 1""", (record["job_id"], record.get("report_date"))).fetchone()
        if latest:
            execution = as_dict(latest)
            record.update(execution_fields(execution))
            if execution["status"] in {"RUNNING", "SUCCESS"}:
                effective_status = execution["status"]
            elif status in {"RUNNING", "SUCCESS", "FAILED"} or (
                not status and execution["status"] == "FAILED"
                and effective_status not in {"MANUAL_REQUIRED", "PAUSED", "CANCELLED", "DISABLED", "DELETED"}
            ):
                effective_status = execution["status"]
                if not status and effective_status == "FAILED" and record.get("run_config"):
                    from scheduler.retry_policy import RetryPolicy
                    if execution["attempt_no"] >= RetryPolicy.max_attempts_for(record):
                        effective_status = "RETRY_EXHAUSTED"
    payload = {key: value for key, value in record.items() if key not in {
        "updated_at", "next_evaluation", "calculated_at", "priority_key", "date_priority", "time_priority", "ready_since", "_logging_signature",
    }}
    return {
        "event_type": event_type, "occurred_at": utc_now(), "source": source,
        "record_key": f"occurrence:{key}", "job_id": record.get("job_id"),
        "name": record.get("job_name", record.get("name")), "report_date": record.get("report_date"),
        "planned_execution_date": record.get("execution_date", record.get("planned_execution_date")),
        "status": effective_status, "actor": actor, "reason": reason or record.get("reason"), "payload": payload,
    }


def capture_occurrence(logger, connection, record, **kwargs):
    record = json_value(as_dict(record))
    old = connection.execute("SELECT payload FROM worker_observed_occurrences WHERE occurrence_key=?", (record.get("occurrence_key"),)).fetchone()
    if old:
        record = {**json.loads(old[0]), "state": "READY", "waiting_for": None, "confirmation_status": None, **record}
    event = occurrence_event(connection, record, **kwargs)
    if event is None:
        return None
    # Persist context even after the READY/STAGING row is consumed or deleted.
    value = {**json_value(as_dict(record)), **event["payload"]}
    terminal = event["status"] in {"RUNNING", "SUCCESS", "FAILED", "MANUAL_REQUIRED", "RETRY_EXHAUSTED", "DELETED", "DISABLED", "PAUSED", "CANCELLED"}
    signature = json.dumps({
        "status": event["status"], "report_date": event.get("report_date"),
        "planned_execution_date": event.get("planned_execution_date"),
        "waiting_for": None if terminal else value.get("waiting_for"),
        "confirmation_status": None if terminal else value.get("confirmation_status"),
        "confirmation": value.get("confirmation"),
        "definition": {field: value.get(field) for field in (
            "run_config", "same_day", "package_name", "time_flag", "confirmation_required",
        )},
        "execution_id": value.get("execution_id"), "attempt_no": value.get("attempt_no"),
        "execution_status": (value.get("execution") or {}).get("status"),
    }, sort_keys=True)
    unchanged = bool(old and json.loads(old[0]).get("_logging_signature") == signature)
    value["_logging_signature"] = signature
    connection.execute("""INSERT INTO worker_observed_occurrences VALUES (?, ?)
        ON CONFLICT(occurrence_key) DO UPDATE SET payload=excluded.payload""",
        (value["occurrence_key"], json.dumps(value, sort_keys=True)))
    return None if unchanged else logger.capture_state(event["record_key"], event, connection=connection, commit=False)


def execution_fields(execution):
    return json_value({
        "execution": execution, "execution_id": execution["id"], "attempt_no": execution["attempt_no"],
        "package_name": execution.get("procedure_name"), "executed_at": execution.get("started_at"),
        "last_run": execution.get("started_at"), "records_loaded": execution.get("count"),
        "error_info": execution.get("error"),
    })


def log_execution(logger, connection, execution_id, *, recovered=False):
    execution = as_dict(connection.execute("SELECT * FROM execution_jobs WHERE id=?", (execution_id,)).fetchone())
    dedupe_key = f"worker-execution:{execution_id}:{execution['status']}"
    previous = connection.execute("SELECT event_id FROM scheduler_oracle_log_outbox WHERE dedupe_key=?", (dedupe_key,)).fetchone()
    if previous:
        return previous[0]  # Restart backfill must not replay old current states.
    context = saved_execution_context(connection, execution)
    status = execution["status"]
    event = {
        "event_type": "EXECUTION_RECOVERED" if recovered else f"EXECUTION_{'STARTED' if status == 'RUNNING' else status}",
        "occurred_at": execution.get("finished_at") or execution.get("started_at") or utc_now(),
        "source": "WORKER", "job_id": execution["job_id"],
        "name": execution.get("job_name"), "report_date": execution.get("report_date"),
        "planned_execution_date": context.get("planned_execution_date"), "status": status,
        "actor": context.get("actor"), "reason": execution.get("error") or context.get("reason"),
        "payload": {"execution": execution, "context": context},
    }
    result = logger.enqueue(event, connection=connection, commit=False,
                            dedupe_key=dedupe_key)
    if context.get("occurrence_key"):
        capture_occurrence(logger, connection, {**context, **execution_fields(execution), "job_name": execution.get("job_name")},
                           status=status, actor=context.get("actor"), reason=execution.get("error"))
    else:
        manual_key = f"manual-attempt:{execution_id}"
        logger.capture_state(manual_key, {**event, "event_type": "OCCURRENCE_STATE_CHANGED", "record_key": manual_key,
                                         "payload": {**context, **execution_fields(execution)}},
                             connection=connection, commit=False)
    return result


def log_operation(logger, connection, audit_id, correlation_id=None):
    previous = connection.execute("SELECT event_id FROM scheduler_oracle_log_outbox WHERE dedupe_key=?", (f"worker-operation:{audit_id}",)).fetchone()
    if previous:
        return previous[0]
    row = as_dict(connection.execute("SELECT * FROM scheduler_operation_audit WHERE id=?", (audit_id,)).fetchone())
    payload = dict(row)
    for field in ("before_state", "after_state"):
        try:
            payload[field] = json.loads(payload[field]) if payload.get(field) else None
        except (TypeError, ValueError):
            pass
    job_id, report_date = None, None
    if row.get("target_type") in {"schedule_master", "occurrence"}:
        target = str(row.get("target_id") or "")
        if row["target_type"] == "schedule_master" and target.isdigit():
            job_id = int(target)
        elif row["target_type"] == "occurrence":
            for state in (payload.get("after_state"), payload.get("before_state")):
                if isinstance(state, dict) and state.get("job_id"):
                    job_id = int(state["job_id"])
                    break
            parts = target.split(":", 1)
            if len(parts) == 2 and parts[0].isdigit():
                job_id = job_id or int(parts[0])
                try:
                    report_date = datetime.fromisoformat(parts[1]).date().isoformat()
                except ValueError:
                    pass
            payload["occurrence_key"] = target
    return logger.enqueue({
        "event_type": row["action"], "occurred_at": row["occurred_at"], "source": row["source"],
        "actor": row.get("actor"), "reason": row.get("reason"), "correlation_id": correlation_id,
        "name": row.get("target_label"), "job_id": job_id, "report_date": report_date, "payload": payload,
    }, connection=connection, commit=False, dedupe_key=f"worker-operation:{audit_id}")


class WorkerLoggingObserver:
    """Backfill preserved history and capture effective controls idempotently."""
    def __init__(self, logger, connection):
        self.logger = logger
        self.connection = connection
        ensure_context_schema(connection)

    def backfill(self):
        with atomic_logging(self.connection):
            for source_type, table in (("execution", "execution_jobs"), ("operation", "scheduler_operation_audit")):
                if not table_exists(self.connection, table):
                    continue
                marker = self.connection.execute("SELECT last_source_id FROM worker_logging_backfill WHERE source_type=?", (source_type,)).fetchone()
                last_id = marker[0] if marker else 0
                for row in self.connection.execute(f"SELECT * FROM {table} WHERE id>? ORDER BY id", (last_id,)).fetchall():
                    # Backfill records the history actually retained; do not
                    # fabricate lost intermediate states of legacy attempts.
                    if source_type == "execution":
                        log_execution(self.logger, self.connection, row["id"], recovered=row["error_type"] == "SchedulerRestart")
                    else:
                        log_operation(self.logger, self.connection, row["id"])
                    last_id = row["id"]
                self.connection.execute("""INSERT INTO worker_logging_backfill VALUES (?, ?)
                    ON CONFLICT(source_type) DO UPDATE SET last_source_id=excluded.last_source_id""", (source_type, last_id))

    def capture(self, application, *, source=None, actor=None, reason=None):
        source = source or ("MONITOR" if application.get("execution_enabled") is False else "WORKER")
        jobs = {str(job.id): job for job in application["schedule_master_repository"].get_all()}
        records = {}
        for repository_name in ("staging_repository", "ready_repository"):
            for row in application[repository_name].get_all():
                value = as_dict(row)
                if value.get("occurrence_key"):
                    records[value["occurrence_key"]] = value
        controls = application.get("job_control_repository")
        with atomic_logging(self.connection):
            for row in self.connection.execute("SELECT payload FROM worker_observed_occurrences").fetchall():
                old = json.loads(row[0])
                last_status = json.loads(old.get("_logging_signature") or "{}").get("status")
                if str(old.get("job_id")) not in jobs and last_status != "SUCCESS":
                    records.setdefault(old["occurrence_key"], old)
            for key, record in records.items():
                job = jobs.get(str(record["job_id"]))
                if job is not None:
                    record.update(run_config=job.run_config, same_day=job.same_day, package_name=job.package_name,
                                  time_flag=job.time_flag, confirmation_required=bool(job.confirmation_needed))
                scoped = getattr(controls, "get_for_occurrence", None) if controls is not None else None
                control = (scoped(record["job_id"], key) if callable(scoped) else controls.get(record["job_id"])) if controls is not None else None
                control = control or {}
                if job is not None or control:
                    record["confirmation"] = bool(control.get("confirmation"))
                status = ("DELETED" if job is None else "DISABLED" if not bool(job.is_active) else
                          control.get("control_status") if control.get("control_status") in {"PAUSED", "CANCELLED"} else None)
                capture_occurrence(self.logger, self.connection, record, status=status, source=source, actor=actor, reason=reason)

    def capture_decision(self, result, *, source="WORKER"):
        if not result or result.get("executed") or not result.get("status"):
            return
        status = "RETRY_EXHAUSTED" if result.get("retry_exhausted") else result["status"]
        if status in {"SUCCESS", "RUNNING"}:  # duplicate-protection reads are not transitions
            return
        with atomic_logging(self.connection):
            context = execution_context(self.connection, result.get("job_id"), str(result["report_date"]) if result.get("report_date") is not None else None)
            context.update({key: value for key, value in result.items() if key in {"occurrence_key", "job_name", "execution_date", "reason"}})
            capture_occurrence(self.logger, self.connection, context, status=status, source=source)

    def before_delete(self, job_id, *, actor=None, reason=None):
        """Capture pending removal before the API deletes its local context."""
        records = {}
        for table in ("staging_occurrences", "ready_occurrences"):
            if table_exists(self.connection, table):
                for row in self.connection.execute(f"SELECT * FROM {table} WHERE job_id=?", (job_id,)).fetchall():
                    record = as_dict(row)
                    latest = self.connection.execute("SELECT status FROM execution_jobs WHERE job_id=? AND report_date IS ? ORDER BY id DESC LIMIT 1", (job_id, record.get("report_date"))).fetchone()
                    if latest and latest["status"] in {"RUNNING", "SUCCESS"}:
                        continue
                    records[record["occurrence_key"]] = record
        with atomic_logging(self.connection):
            for record in records.values():
                capture_occurrence(self.logger, self.connection, record, status="DELETED", source="CONTROL_API",
                                   actor=actor, reason=reason or "Schedule definition deleted.")


def consume_manual_request(logger, connection, job_id):
    """Consume both one-shot controls and their system audit atomically."""
    from repositories.operations_repository import OperationsRepository
    with atomic_logging(connection):
        row = connection.execute("SELECT * FROM job_control WHERE job_id=?", (job_id,)).fetchone()
        if row is None or not row["manual_run"]:
            return
        before = as_dict(row)
        connection.execute("UPDATE job_control SET manual_run=0, override_datetime=NULL, updated_at=? WHERE job_id=?", (utc_now(), job_id))
        after = as_dict(connection.execute("SELECT * FROM job_control WHERE job_id=?", (job_id,)).fetchone())
        OperationsRepository(connection, event_logger=logger).record_audit(
            action="MANUAL_REQUEST_CONSUMED", target_type="schedule_master", target_id=job_id,
            actor="SYSTEM", reason="Manual execution attempt completed; one-shot controls consumed.",
            before_state=before, after_state=after, source="WORKER", connection=connection, commit=False,
        )
