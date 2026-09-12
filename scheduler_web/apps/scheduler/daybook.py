"""Display one outcome per worker occurrence, without calculating schedule dates."""
from django.utils import timezone
from .services import decorate_schedule, ResponsibilityResolver


def pending_confirmation(row):
    """Actual, unresolved confirmation gates, scoped to their occurrence."""
    occurrence = row.get("occurrence") or row
    confirmed = row.get("confirmed", occurrence.get("confirmation_confirmed", occurrence.get("confirmation", False)))
    return bool(
        row.get("confirmation_needed") and not confirmed
        and not occurrence.get("is_projection") and occurrence.get("occurrence_key")
        and str(row.get("state", "")).upper() not in {
            "SUCCESS", "COMPLETED", "RUNNING", "FAILED", "CANCELLED", "DISABLED",
            "MANUAL_REQUIRED", "RETRY_EXHAUSTED", "EXPIRED",
        }
    )


def daybook_rows(payload, adapter, user, selected_day=None):
    schedules = {row["id"]: row for row in adapter.get_schedules()}
    controls_available = adapter.get_status().get("label") == "BACKGROUND SCHEDULER API"
    rows = []
    resolver = ResponsibilityResolver({int(entry["job_id"]) for entry in payload.get("occurrences", [])})
    for entry in payload.get("occurrences", []):
        operating_day = adapter._to_date(entry.get("calendar_date") or entry.get("execution_date"))
        if selected_day and operating_day != selected_day:
            continue
        schedule_id = int(entry["job_id"])
        base = schedules.get(schedule_id, {})
        state = str(entry.get("state") or "PLANNED").upper()
        row = {**base, **entry, "id": schedule_id, "name": entry.get("job_name") or base.get("name", str(schedule_id)),
               "occurrence": entry, "state": state, "operational_state": state, "operating_day": operating_day}
        row["display_status"] = {
            "SUCCESS": "Done", "COMPLETED": "Done", "FAILED": "Failed", "RUNNING": "Running",
            "READY": "Ready", "PAUSED": "Paused", "CANCELLED": "Cancelled", "DISABLED": "Cancelled",
            "WAITING_CONFIRMATION": "Confirmation",
            "MANUAL_REQUIRED": "Manual required",
        }.get(state, "Pending")
        row["tone"] = {"Done": "success", "Failed": "danger", "Cancelled": "secondary",
                       "Running": "primary", "Ready": "primary"}.get(row["display_status"], "warning")
        row = decorate_schedule(row, user, operating_day, resolver=resolver)
        row["can_confirm"] = bool(controls_available and row.get("confirmation_needed") and row["can_confirm"] and not entry.get("is_projection") and entry.get("occurrence_key")
                                  and operating_day == timezone.localdate()
                                  and state not in {"SUCCESS", "FAILED", "RUNNING", "CANCELLED", "DISABLED"})
        row["confirmed"] = bool(entry.get("confirmation_confirmed", entry.get("confirmation", False)))
        row["timing"] = adapter._timing_for(row, now=timezone.localtime(), in_queue=state == "READY")
        labels = {"RUNNING": "Execution in progress", "SUCCESS": "Execution completed", "FAILED": "Review failure and retry history",
                  "MANUAL_REQUIRED": "Execution day passed · request a manual run",
                  "CANCELLED": "Cancelled by operator", "PAUSED": "Paused until resumed", "DISABLED": "Schedule is inactive"}
        if state in labels:
            row["timing"]["label"] = labels[state]
        if row["confirmed"] and state == "WAITING_CONFIRMATION":
            row["display_status"] = "Pending"
            row["timing"]["label"] = "Confirmed · awaiting worker reevaluation"
        row["overdue"] = False
        started = adapter._to_datetime(entry.get("started_at"))
        if state == "RUNNING" and started and row["expected_minutes"]:
            if timezone.is_naive(started):
                started = timezone.make_aware(started)
            row["elapsed_minutes"] = max(0, int((timezone.now()-started).total_seconds() // 60))
            row["overdue"] = row["elapsed_minutes"] > row["expected_minutes"]
        rows.append(row)
    return rows


def daybook_counts(rows):
    return {label: sum(row["display_status"] == label for row in rows)
            for label in ["Pending", "Confirmation", "Ready", "Running", "Done", "Failed", "Paused", "Cancelled", "Manual required"]}
