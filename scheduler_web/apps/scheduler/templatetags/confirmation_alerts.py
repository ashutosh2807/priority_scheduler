"""Occurrence-scoped alerts shared by the authenticated operations pages."""
from django import template
from django.utils import timezone

from apps.scheduler.daybook import daybook_rows, pending_confirmation
from apps.scheduler.file_adapter import get_scheduler_read_adapter
from apps.scheduler.services import is_operator

register = template.Library()


@register.inclusion_tag("components/confirmation_alert.html", takes_context=True)
def confirmation_alert(context):
    user = context.get("user")
    if user is None or not is_operator(user):
        return {}
    adapter = context.get("scheduler_adapter")
    if adapter is None:
        view = context.get("view")
        adapter = getattr(view, "adapter", None) or get_scheduler_read_adapter()
    status = adapter.get_status()
    if status.get("label") != "BACKGROUND SCHEDULER API":
        return {}
    today = timezone.localdate()
    range_start, range_end = context.get("calendar_range_start"), context.get("calendar_range_end")
    payload = context.get("calendar_payload") if range_start and range_end and range_start <= today <= range_end else None
    if payload is None:
        payload = adapter.get_operations_calendar(today, 1)
    # A report may produce multiple independent occurrences on the same day.
    # Deduplicate by identity, never by schedule ID or the old global flag.
    pending = {
        row["occurrence_key"]: row
        for row in daybook_rows(payload, adapter, user, today)
        if pending_confirmation(row)
    }
    rows = pending.values()
    return {
        "confirmation_alert_active": True,
        "confirmation_alert_count": len(pending),
        "confirmation_alert_mine": sum(row["responsibility"]["operator"] == user for row in rows),
        "confirmation_alert_unassigned": sum(row["responsibility"]["operator"] is None for row in rows),
    }
