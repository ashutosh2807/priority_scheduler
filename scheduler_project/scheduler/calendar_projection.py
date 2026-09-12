"""Read-only day view built from worker forecasts and durable lifecycle rows."""

from collections import Counter
from datetime import date, timedelta


def build_calendar(snapshot, projections, start_date, days, calendar):
    """Overlay current/history state without changing eligibility or dates."""
    end_date = start_date + timedelta(days=days - 1)
    first, last = start_date.isoformat(), end_date.isoformat()
    schedules = {str(row["id"]): row for row in snapshot["schedule_master"]}
    controls = {str(row["job_id"]): row for row in snapshot["controls"]}
    queue = {row.get("occurrence_key"): index for index, row in enumerate(snapshot["priority_queue"], 1)}
    approvals = {row["occurrence_key"]: bool(row["confirmed"]) for row in snapshot.get("occurrence_confirmations", [])}
    records = {}

    def key_for(row):
        return row.get("occurrence_key") or (
            f'{row.get("job_id")}:{str(row["report_date"])[:10]}' if row.get("report_date")
            else f'forecast:{row.get("job_id")}:{row.get("execution_date")}:{row.get("frequency", "")}')

    for row in projections:
        row = dict(row, source="projection", is_projection=True)
        records[key_for(row)] = row
    for collection, state in (("staging", None), ("ready", "READY")):
        for original in snapshot[collection]:
            key = key_for(original)
            merged = {**records.get(key, {}), **original}
            merged.update(source=collection, is_projection=False)
            merged["state"] = state or original.get("state", "STAGING")
            records[key] = merged

    # Histories contain multiple attempts. Keep the latest outcome separately
    # from the current runnable lifecycle, and retain the original planned day.
    latest = {}
    for original in snapshot["executions"]:
        key = key_for(original)
        sort_key = (str(original.get("started_at") or ""), int(original.get("id") or 0))
        if key not in latest or sort_key > latest[key][0]:
            latest[key] = (sort_key, original)
    for key, (_, execution) in latest.items():
        row = records.get(key, {})
        actual = str(execution.get("started_at") or "")[:10] or None
        planned_date = row.get("execution_date")
        execution_date = row.get("execution_date") or actual
        latest_status = str(execution.get("status") or "PENDING").upper()
        retry_pending = latest_status == "FAILED" and key in queue
        records[key] = {
            **row,
            "occurrence_key": row.get("occurrence_key") or key,
            "job_id": execution.get("job_id"),
            "job_name": execution.get("job_name") or row.get("job_name"),
            "report_date": execution.get("report_date"),
            "execution_date": execution_date,
            "planned_execution_date": planned_date,
            "actual_execution_date": actual,
            "started_at": execution.get("started_at"),
            "finished_at": execution.get("finished_at"),
            "duration_seconds": execution.get("duration_seconds"),
            "attempt_no": execution.get("attempt_no"),
            "error": execution.get("error"),
            "latest_attempt_status": latest_status,
            "latest_attempt_id": execution.get("id"),
            "retry_pending": retry_pending,
            "state": "MANUAL_REQUIRED" if row.get("state") == "MANUAL_REQUIRED" and latest_status not in {"SUCCESS", "RUNNING"} else "READY" if retry_pending else latest_status,
            "source": "ready" if retry_pending else "execution", "is_projection": False,
        }

    result = []
    for key, row in records.items():
        scheduled_day = str(row.get("execution_date") or "")[:10]
        if "planned_execution_date" not in row:
            row["planned_execution_date"] = row.get("execution_date")
        schedule = schedules.get(str(row.get("job_id")), {})
        control = controls.get(str(row.get("job_id")), {})
        state = str(row.get("state") or "PENDING").upper()
        if state not in {"SUCCESS", "RUNNING"}:
            control_state = str(control.get("control_status") or "ACTIVE").upper()
            if control_state in {"PAUSED", "CANCELLED"}:
                state = control_state
        normalized = state.lower() if state in {
            "WAITING_CONFIRMATION", "READY", "RUNNING", "SUCCESS", "FAILED", "PAUSED", "CANCELLED"
        } else "pending"
        awaiting = row.get("report_date") is None or state == "WAITING_DATEMAST"
        row.update(
            state=state,
            status=normalized,
            queue_position=queue.get(row.get("occurrence_key")),
            confirmation_needed=bool(schedule.get("confirmation_needed")),
            confirmation=approvals.get(row.get("occurrence_key"), False),
            confirmation_confirmed=approvals.get(row.get("occurrence_key"), False),
            confirmation_status=(
                "CONFIRMED" if approvals.get(row.get("occurrence_key"), False) else "WAITING_CONFIRMATION"
            ) if bool(schedule.get("confirmation_needed")) else "NOT_REQUIRED",
            availability="awaiting_datemast" if awaiting else "forecast" if row["is_projection"] else "authoritative",
            job_name=row.get("job_name") or schedule.get("name", ""),
        )
        # A delayed execution belongs in the original schedule as well as the
        # day on which operators actually worked on it. Spanning-midnight work
        # remains visible on every active day. Sets prevent double counting
        # when scheduled, started and finished dates coincide.
        activity_days = set()
        actual = row.get("actual_execution_date")
        finished = str(row.get("finished_at") or "")[:10]
        if actual:
            activity_days.add(actual)
            activity_end = finished or (date.today().isoformat() if state == "RUNNING" else actual)
            first_active = max(start_date, date.fromisoformat(actual))
            last_active = min(end_date, date.fromisoformat(activity_end))
            for offset in range(max(0, (last_active - first_active).days + 1)):
                activity_days.add((first_active + timedelta(days=offset)).isoformat())
        live_day = date.today().isoformat() if state in {"READY", "RUNNING"} and (
            row.get("occurrence_key") in queue or state == "RUNNING"
        ) else None
        calendar_days = {scheduled_day, *activity_days}
        if live_day:
            calendar_days.add(live_day)
        for operating_day in sorted(calendar_days):
            if not (first <= operating_day <= last):
                continue
            day_context = (
                "scheduled_activity" if operating_day == scheduled_day and operating_day in activity_days
                else "scheduled" if operating_day == scheduled_day
                else "activity" if operating_day in activity_days
                else "live_carryover"
            )
            result.append({**row, "calendar_date": operating_day, "day_context": day_context})
    result.sort(key=lambda row: (row["calendar_date"], row.get("from_time") or "23:59", int(row["job_id"]), row.get("occurrence_key") or ""))
    daily = []
    for offset in range(days):
        day = (start_date + timedelta(days=offset)).isoformat()
        counts = Counter(row["status"] for row in result if row["calendar_date"] == day)
        daily.append({"date": day, "total": sum(counts.values()), "counts": dict(counts)})
    return {
        "start_date": first, "end_date": last, "calendar": calendar,
        "occurrences": result, "days": daily,
        "generated_at": snapshot["meta"]["generated_at"],
    }
