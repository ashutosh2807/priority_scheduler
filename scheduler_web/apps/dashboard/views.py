import calendar
from collections import defaultdict
from datetime import date, datetime, timedelta

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView
from django.utils import timezone

from apps.leave.models import LeaveRequest, LeaveStatus
from apps.scheduler.file_adapter import SchedulerProjectReadAdapter, get_scheduler_read_adapter
from apps.scheduler.daybook import daybook_rows, daybook_counts


def _selected_month(value: str | None) -> date:
    """Return the first date of the requested ISO month, or this month."""
    if value:
        try:
            return datetime.strptime(value, "%Y-%m").date().replace(day=1)
        except ValueError:
            pass
    return timezone.localdate().replace(day=1)


def calendar_context(
    month: date,
    scheduler: SchedulerProjectReadAdapter | None = None,
    selected_date: date | None = None,
) -> dict:
    """Present worker-provided operating dates alongside approved leave."""
    last_day = calendar.monthrange(month.year, month.month)[1]
    month_end = month.replace(day=last_day)
    grid_dates = calendar.Calendar(firstweekday=0).monthdatescalendar(month.year, month.month)
    grid_start, grid_end = grid_dates[0][0], grid_dates[-1][-1]
    payload = scheduler.get_operations_calendar(grid_start, (grid_end-grid_start).days+1) if scheduler else {}
    # Calendar classification is supplied by the same worker that evaluates jobs.
    # DATEMAST gaps and bank Saturday rules are interpreted only by the worker.
    day_types = {item["date"]: item for item in payload.get("calendar_days", [])}
    holiday_meta = payload.get("calendar", {})
    events: dict[date, list[dict]] = defaultdict(list)

    leave_requests = LeaveRequest.objects.select_related("admin").filter(
        status=LeaveStatus.APPROVED, start_date__lte=month_end, end_date__gte=month
    )
    for leave_request in leave_requests:
        cursor = max(leave_request.start_date, month)
        end = min(leave_request.end_date, month_end)
        while cursor <= end:
            events[cursor].append({
                "label": f"{leave_request.admin.display_name} on leave", "kind": "leave", "url": f"/leave/{leave_request.pk}/",
            })
            cursor += timedelta(days=1)

    scheduler_days = {}
    for occurrence in payload.get("occurrences", []):
        operating_day = scheduler._to_date(occurrence.get("calendar_date")) if scheduler else None
        if operating_day is None:
            continue
        summary = scheduler_days.setdefault(operating_day, {"scheduled": 0, "success": 0, "failed": 0, "running": 0})
        summary["scheduled"] += 1
        state = str(occurrence.get("state", "")).upper()
        summary["success"] += state in {"SUCCESS", "COMPLETED"}
        summary["failed"] += state == "FAILED"
        summary["running"] += state == "RUNNING"
    selected_date = selected_date or (timezone.localdate() if timezone.localdate().month == month.month and timezone.localdate().year == month.year else month)
    selected_leave = []
    for leave_request in leave_requests:
        if leave_request.start_date <= selected_date <= leave_request.end_date:
            selected_leave.append(leave_request)

    weeks = []
    for week in grid_dates:
        weeks.append([{
            "date": day, "in_month": day.month == month.month,
            "is_today": day == timezone.localdate(), "is_selected": day == selected_date,
            "events": events.get(day, []), "scheduler_summary": scheduler_days.get(day),
            "calendar_info": day_types.get(day.isoformat(), {}),
            "is_holiday": day_types.get(day.isoformat(), {}).get("is_holiday",
                day_types.get(day.isoformat(), {}).get("kind") == "HOLIDAY"),
            "is_weekend": day_types.get(day.isoformat(), {}).get("kind") in {"SAT", "SUN"},
        } for day in week])
    previous_month = (month - timedelta(days=1)).replace(day=1)
    next_month = (month_end + timedelta(days=1)).replace(day=1)
    return {
        "calendar_weeks": weeks, "calendar_month": month,
        "previous_month": previous_month.strftime("%Y-%m"),
        "next_month": next_month.strftime("%Y-%m"),
        "selected_date": selected_date,
        "selected_summary": scheduler_days.get(selected_date, {}),
        "selected_execution_activity": scheduler.get_execution_day(selected_date) if scheduler is not None else [],
        "selected_leave": selected_leave,
        "selected_calendar_info": day_types.get(selected_date.isoformat(), {}),
        "selected_is_holiday": day_types.get(selected_date.isoformat(), {}).get("is_holiday",
            day_types.get(selected_date.isoformat(), {}).get("kind") == "HOLIDAY"),
        "calendar_payload": payload,
        "calendar_meta": holiday_meta,
        "calendar_range_start": grid_start,
        "calendar_range_end": grid_end,
        "holiday_markers_available": bool(day_types),
        "holiday_count": holiday_meta.get("holiday_count"),
        "holiday_rule_source": holiday_meta.get("holiday_rule_source"),
        "holiday_observed_from": holiday_meta.get("holiday_observed_from"),
        "holiday_observed_through": holiday_meta.get("holiday_observed_through"),
        "holiday_source_label": {"oracle": "Oracle", "local_snapshot": "Local holiday snapshot",
                                 "retained_snapshot": "Retained holiday snapshot", "unavailable": "Unavailable"}.get(
                                     holiday_meta.get("holiday_source"), "Unavailable"),
    }


class DashboardView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        scheduler = get_scheduler_read_adapter()
        schedules = scheduler.get_schedules()
        summary = scheduler.get_status()
        today_plan = scheduler.get_execution_plan(today)
        priority_queue = scheduler.get_ready()
        pre_queue = [
            schedule
            for schedule in today_plan
            if schedule["lifecycle_state"] != "READY"
            and schedule["operational_state"] not in {"CANCELLED", "DISABLED"}
        ]
        today_attempts = scheduler.get_execution_day(today)
        context.update(calendar_context(today.replace(day=1), scheduler))
        context.update({
            "scheduler_adapter": scheduler,
            "day_rows": daybook_rows(scheduler.get_operations_calendar(today, 1), scheduler, self.request.user, today),
            "today": today,
            "total_schedules": summary["total"],
            "active_schedules": summary["active"],
            "leave_today": LeaveRequest.objects.filter(status=LeaveStatus.APPROVED, start_date__lte=today, end_date__gte=today).count(),
            "staging_schedules": summary["staging"],
            "scheduler_summary": summary,
            "schedules": schedules[:8],
            "failed_executions": [record for record in scheduler.get_executions() if record["status"] == "FAILED"],
            "today_plan": today_plan,
            "priority_queue": priority_queue[:5],
            "pre_queue": pre_queue[:3],
            "pre_queue_count": len(pre_queue),
            "today_executions": today_attempts,
            "today_pending": sum(
                schedule["operational_state"] not in {"CANCELLED", "DISABLED"}
                for schedule in today_plan
            ),
            "today_successful": sum(record.get("status") == "SUCCESS" for record in today_attempts),
            "today_failed": sum(record.get("status") == "FAILED" for record in today_attempts),
        })
        return context


class CalendarView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/calendar.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        month = _selected_month(self.request.GET.get("month"))
        selected = _parse_selected_date(self.request.GET.get("date"), month)
        adapter = get_scheduler_read_adapter()
        context.update(calendar_context(month, adapter, selected))
        payload = context["calendar_payload"]
        rows = daybook_rows(payload, adapter, self.request.user)
        by_day = defaultdict(list)
        for row in rows:
            by_day[row["operating_day"]].append(row)
        context.update(day_rows=by_day.get(selected, []), scheduler=adapter.get_status(), scheduler_adapter=adapter,
                       calendar_meta=payload.get("calendar", {}), calendar_error=payload.get("error", ""),
                       projection_available=payload.get("projection_available", False))
        return context


class HelpView(LoginRequiredMixin, TemplateView):
    template_name = "dashboard/help.html"


def _parse_selected_date(value: str | None, month: date) -> date:
    if value:
        try:
            selected = date.fromisoformat(value)
            if selected.year == month.year and selected.month == month.month:
                return selected
        except ValueError:
            pass
    return timezone.localdate() if timezone.localdate().year == month.year and timezone.localdate().month == month.month else month
