from __future__ import annotations

from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.http import Http404
from django.shortcuts import redirect
from django.views import View
from django.views.generic import FormView, TemplateView
from django.utils import timezone
from django.db import transaction

from apps.accounts.models import AdminRole
from apps.audit.models import AuditLog
from apps.audit.services import record_external_action

from .control_client import SchedulerApiError, get_scheduler_control_client
from .file_adapter import get_scheduler_read_adapter
from .forms import (
    SCHEDULE_FREQUENCIES,
    ScheduleMasterConfigForm,
    ScheduleProfileForm,
    SchedulerControlForm,
)
from .models import ScheduleProfile, RunbookProgress
from .services import SchedulerService, decorate_schedule, is_operator, is_manager, may_edit_profile, record_runbook_step


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _can_manage_schedule_master(user) -> bool:
    """Only the portal's senior scheduler operators may alter Master fields."""
    return is_manager(user)


def _run_by_values(schedule: dict) -> dict:
    run_config = schedule.get("run_config") or {}
    run_by = run_config.get("RUN_BY") or {}
    return {
        "from_time": run_by.get("FROM_TIME") or run_by.get("from_time") or "",
        "to_time": run_by.get("TO_TIME") or run_by.get("to_time") or "",
    }


def _profile_snapshot(profile: ScheduleProfile | None) -> dict:
    return {
        "description": profile.description if profile else "",
        "operational_steps": profile.operational_steps if profile else [],
        "primary_operator_id": profile.primary_operator_id if profile else None,
        "backup_operator_id": profile.backup_operator_id if profile else None,
        "expected_minutes": profile.expected_minutes if profile else None,
        "primary_operator": (
            profile.primary_operator.display_name
            if profile and profile.primary_operator
            else ""
        ),
    }


def _save_profile(schedule_id: int, cleaned_data: dict, actor) -> tuple[dict, dict]:
    profile = ScheduleProfile.objects.filter(schedule_id=schedule_id).first()
    before = _profile_snapshot(profile)
    description = cleaned_data.get("description", "")
    operational_steps = cleaned_data.get("operational_steps", [])
    primary_operator = cleaned_data.get("responsible_operator")
    if description or operational_steps or primary_operator or cleaned_data.get("backup_operator") or cleaned_data.get("expected_minutes") or profile:
        profile, _ = ScheduleProfile.objects.update_or_create(
            schedule_id=schedule_id,
            defaults={
                "description": description,
                "operational_steps": operational_steps,
                "primary_operator": primary_operator,
                "backup_operator": cleaned_data.get("backup_operator"),
                "expected_minutes": cleaned_data.get("expected_minutes"),
                "updated_by": actor if getattr(actor, "is_authenticated", False) else None,
            },
        )
    return before, _profile_snapshot(profile)


class SchedulerSourceMixin:
    """Keep all reads and operator writes behind explicit scheduler adapters."""

    @property
    def adapter(self):
        if not hasattr(self, "_scheduler_adapter"):
            self._scheduler_adapter = get_scheduler_read_adapter()
        return self._scheduler_adapter

    @property
    def control_client(self):
        if not hasattr(self, "_scheduler_control_client"):
            self._scheduler_control_client = get_scheduler_control_client()
        return self._scheduler_control_client


class SchedulerDashboardView(LoginRequiredMixin, SchedulerSourceMixin, TemplateView):
    template_name = "scheduler/dashboard.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        today = timezone.localdate()
        month_data = self.adapter.get_calendar_days(today.replace(day=1))
        today_plan = self.adapter.get_execution_plan(today)
        priority_queue = self.adapter.get_ready()
        scheduler_status = self.adapter.get_status()
        service_control = scheduler_status.get("service_control") or {}
        pre_queue = [
            schedule
            for schedule in today_plan
            if schedule["lifecycle_state"] != "READY"
            and schedule["operational_state"] not in {"CANCELLED", "DISABLED"}
        ]
        today_attempts = self.adapter.get_execution_day(today)
        context.update({
            "scheduler": scheduler_status,
            "service_control": service_control,
            "operation_audit": (scheduler_status.get("operation_audit") or [])[:8],
            "service_controls_available": (
                scheduler_status.get("label") == "BACKGROUND SCHEDULER API"
                and bool(service_control.get("available"))
            ),
            "can_control_service": (
                self.request.user.is_superuser
                or getattr(self.request.user, "role", "") == AdminRole.SUPERUSER
            ),
            "schedules": self.adapter.get_schedules()[:10],
            "ready_schedules": priority_queue,
            "priority_queue": priority_queue[:6],
            "pre_queue": pre_queue[:3],
            "pre_queue_count": len(pre_queue),
            "today_summary": month_data.get(today, {}),
            "today_plan": today_plan,
            "today_executions": today_attempts,
            "today_pending": sum(
                schedule["operational_state"] not in {"CANCELLED", "DISABLED"}
                for schedule in today_plan
            ),
            "today_successful": sum(record.get("status") == "SUCCESS" for record in today_attempts),
            "today_failed": sum(record.get("status") == "FAILED" for record in today_attempts),
            "upcoming_plan": self.adapter.get_upcoming_execution_plan(after=today),
            "today": today,
        })
        return context


class SchedulerServiceActionView(LoginRequiredMixin, SchedulerSourceMixin, View):
    """Forward a SUPERUSER's start/stop request to the scheduler service API."""

    messages_for_action = {
        "stop": (
            "Future scheduler cycles were paused safely. Active Oracle work was not interrupted "
            "and is allowed to finish."
        ),
        "start": "Future scheduler cycles were activated. The worker will resume on its normal next cycle.",
    }

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not is_manager(request.user):
            raise PermissionDenied("Scheduler-wide service control is restricted to SUPERUSER users.")
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, action):
        action = str(action or "").strip().lower()
        if action not in self.messages_for_action:
            messages.error(request, "That scheduler service action is not supported.")
            return redirect("scheduler:dashboard")

        reason = str(request.POST.get("reason", "") or "").strip()
        before = dict((self.adapter.get_status().get("service_control") or {}))
        try:
            actor = request.user.get_username() if request.user.is_authenticated else ""
            after = self.control_client.service_control(
                action,
                actor=actor or None,
                reason=reason or None,
            )
        except SchedulerApiError as error:
            messages.error(request, str(error))
            return redirect("scheduler:dashboard")

        record_external_action(
            request.user,
            f"SCHEDULER_SERVICE_{action.upper()}",
            object_type="scheduler.SchedulerService",
            object_id="scheduler-service",
            object_label="Future scheduler cycles",
            reason=reason,
            changes={"before": before, "after": after},
        )
        messages.success(request, self.messages_for_action[action])
        return redirect("scheduler:dashboard")


class UpcomingProcessesView(LoginRequiredMixin, SchedulerSourceMixin, TemplateView):
    """Show the scheduler's own upcoming-occurrence snapshot.

    This is intentionally a presentation layer only: the adapter supplies
    concrete occurrences and the page never extrapolates Schedule Master
    frequencies in the browser or in Django.
    """

    template_name = "scheduler/upcoming.html"
    plan_limit = 60

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        selected_from = _parse_date(self.request.GET.get("from")) or timezone.localdate()
        plan = self.adapter.get_upcoming_execution_plan(after=selected_from, limit=self.plan_limit)
        groups_by_date: dict[str, dict] = {}

        for schedule in plan:
            timing = schedule.get("timing") or {}
            execution_date = timing.get("execution_date")
            if isinstance(execution_date, str):
                execution_date = _parse_date(execution_date)
            if not execution_date:
                # The adapter normally filters these entries.  Keeping this
                # guard here makes an incomplete remote snapshot safe to show.
                continue
            key = execution_date.isoformat()
            group = groups_by_date.setdefault(key, {
                "execution_date": execution_date,
                "schedules": [],
            })
            group["schedules"].append(schedule)

        context.update({
            "scheduler": self.adapter.get_status(),
            "selected_from": selected_from,
            "upcoming_plan": plan,
            "upcoming_groups": list(groups_by_date.values()),
            "plan_limit": self.plan_limit,
        })
        return context


class JobListView(LoginRequiredMixin, SchedulerSourceMixin, TemplateView):
    template_name = "scheduler/job_list.html"
    paginate_by = 20

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        state = self.kwargs.get("forced_status") or self.request.GET.get("state", "").upper()
        frequency = self.request.GET.get("frequency", "").upper()
        search = self.request.GET.get("q", "").strip().lower()
        schedules = self.adapter.get_schedules()
        if state:
            schedules = [schedule for schedule in schedules if schedule["operational_state"] == state]
        if frequency:
            schedules = [schedule for schedule in schedules if frequency in schedule["run_config"].get("RUNS_ON", [])]
        if search:
            schedules = [
                schedule for schedule in schedules
                if search in schedule["name"].lower() or search in str(schedule["id"])
            ]
        page_obj = Paginator(schedules, self.paginate_by).get_page(self.request.GET.get("page"))
        context.update({
            "schedules": page_obj,
            "page_obj": page_obj,
            "is_confirmation_queue": self.kwargs.get("forced_status") == "WAITING_CONFIRMATION",
            "selected_state": state,
            "selected_frequency": frequency,
            "search": self.request.GET.get("q", ""),
            "can_manage_schedules": is_manager(self.request.user),
            "states": ["READY", "WAITING_CONFIRMATION", "WAITING_TIME", "WAITING_DATEMAST", "WAITING_EXECUTION_DATE", "MANUAL_REQUIRED", "PAUSED", "CANCELLED", "DISABLED", "IDLE"],
            "frequencies": SCHEDULE_FREQUENCIES,
            "scheduler": self.adapter.get_status(),
        })
        return context


class JobDetailView(LoginRequiredMixin, SchedulerSourceMixin, TemplateView):
    template_name = "scheduler/job_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        schedule = self.adapter.get_schedule(self.kwargs["job_id"])
        if self.request.GET.get("occurrence_key"):
            schedule = self.adapter.get_schedule_occurrence(self.kwargs["job_id"], self.request.GET["occurrence_key"]) or schedule
        if schedule is None:
            raise Http404("Schedule not found")
        schedule = decorate_schedule(schedule, self.request.user)
        report_day = _parse_date(self.request.GET.get("report_date")) or self.adapter._to_date((schedule.get("occurrence") or {}).get("report_date"))
        profile = schedule["responsibility"]["profile"]
        progress = {row.step_index: row for row in RunbookProgress.objects.filter(
            schedule_id=schedule["id"], report_date=report_day
        ).select_related("actor")} if report_day else {}
        steps = []
        for index, step in enumerate(profile.operational_steps if profile else []):
            row = progress.get(index)
            steps.append({"index": index, "text": step, "progress": row if row and row.step_text == step else None})
        scheduler_status = self.adapter.get_status()
        context.update({
            "report_day": report_day, "runbook_steps": steps,
            "can_edit_profile": may_edit_profile(self.request.user, schedule["id"]),
            "is_manager": is_manager(self.request.user),
            "schedule": schedule,
            "scheduler": scheduler_status,
            "controls_available": scheduler_status.get("label") == "BACKGROUND SCHEDULER API",
            "can_edit_master_configuration": _can_manage_schedule_master(self.request.user),
            "master_configuration_available": scheduler_status.get("label") == "BACKGROUND SCHEDULER API",
            "execution_history": self.adapter.get_schedule_executions(schedule["id"]),
            "control_form": SchedulerControlForm(initial={"override_datetime": schedule["control"].get("override_datetime")}),
            "audit_entries": AuditLog.objects.filter(
                object_id=str(schedule["id"]), object_type__in=["scheduler.ScheduleMaster", "scheduler.ScheduleProfile", "scheduler.RunbookProgress", "scheduler.WorkNote"]
            ).select_related("actor")[:10],
            "profile": ScheduleProfile.objects.filter(schedule_id=schedule["id"]).select_related(
                "updated_by", "primary_operator"
            ).first(),
        })
        return context


class ScheduleMasterConfigView(LoginRequiredMixin, SchedulerSourceMixin, FormView):
    """Edit the permitted Schedule Master settings through the worker."""

    template_name = "scheduler/master_config_form.html"
    form_class = ScheduleMasterConfigForm

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not _can_manage_schedule_master(request.user):
            raise PermissionDenied("Schedule Master configuration is restricted to SUPERUSER users.")
        self.schedule = self.adapter.get_schedule(kwargs["job_id"])
        if self.schedule is None:
            raise Http404("Schedule not found")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        return {
            "is_active": self.schedule["is_active"],
            "max_attempts": self.schedule.get("max_attempts", 3),
            **_run_by_values(self.schedule),
        }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scheduler = self.adapter.get_status()
        context.update({
            "schedule": self.schedule,
            "scheduler": scheduler,
            "configuration_available": scheduler.get("label") == "BACKGROUND SCHEDULER API",
            "current_run_by": _run_by_values(self.schedule),
        })
        return context

    def form_valid(self, form):
        from_time = form.cleaned_data.get("from_time")
        to_time = form.cleaned_data.get("to_time")
        run_by = (
            {"from_time": from_time.strftime("%H:%M"), "to_time": to_time.strftime("%H:%M")}
            if from_time and to_time
            else None
        )
        reason = str(form.cleaned_data.get("reason") or "").strip()
        before = {
            "id": self.schedule["id"],
            "is_active": bool(self.schedule["is_active"]),
            "run_by": _run_by_values(self.schedule),
            "max_attempts": self.schedule.get("max_attempts", 3),
        }
        attempt_settings = {}
        if form.cleaned_data.get("max_attempts") is not None:
            attempt_settings["max_attempts"] = form.cleaned_data["max_attempts"]
        try:
            actor = self.request.user.get_username() if self.request.user.is_authenticated else ""
            configuration = self.control_client.update_schedule_configuration(
                self.schedule["id"],
                is_active=bool(form.cleaned_data["is_active"]),
                run_by=run_by,
                actor=actor or None,
                reason=reason or None,
                **attempt_settings,
            )
        except SchedulerApiError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)

        after = configuration.get("after") or configuration
        record_external_action(
            self.request.user,
            "SCHEDULE_MASTER_CONFIGURATION_UPDATED",
            object_type="scheduler.ScheduleMaster",
            object_id=str(self.schedule["id"]),
            object_label=self.schedule["name"],
            reason=reason,
            changes={"before": before, "after": after},
        )
        messages.success(
            self.request,
            "The worker accepted the permitted Schedule Master update. Frequency, margin, package, and Oracle procedure settings were not changed.",
        )
        if configuration.get("warning"):
            messages.warning(self.request, str(configuration["warning"]))
        return redirect("scheduler:detail", job_id=self.schedule["id"])


class ScheduleEditView(LoginRequiredMixin, SchedulerSourceMixin, FormView):
    template_name = "scheduler/schedule_form.html"
    form_class = ScheduleProfileForm

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not may_edit_profile(request.user, kwargs["job_id"]):
            raise PermissionDenied("Only the primary operator or a SUPERUSER can edit this runbook.")
        self.schedule = self.adapter.get_schedule(kwargs["job_id"])
        if self.schedule is None:
            raise Http404("Schedule not found")
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        initial = {}
        profile = ScheduleProfile.objects.filter(schedule_id=self.schedule["id"]).first()
        if profile:
            initial.update({
                "description": profile.description,
                "operational_steps": "\n".join(profile.operational_steps),
                "responsible_operator": profile.primary_operator_id,
                "backup_operator": profile.backup_operator_id,
                "expected_minutes": profile.expected_minutes,
            })
        return initial

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["confirmation_needed"] = self.schedule["confirmation_needed"]
        return kwargs

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        if not is_manager(self.request.user):
            form.fields["responsible_operator"].disabled = True
            form.fields["backup_operator"].disabled = True
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({"mode": "edit", "schedule": self.schedule, "scheduler": self.adapter.get_status()})
        return context

    @transaction.atomic
    def form_valid(self, form):
        profile_before, profile_after = _save_profile(self.schedule["id"], form.cleaned_data, self.request.user)
        record_external_action(
            self.request.user, "SCHEDULE_UI_CONTEXT_UPDATED", object_type="scheduler.ScheduleProfile",
            object_id=str(self.schedule["id"]), object_label=self.schedule["name"],
            reason="ITRP operator runbook and confirmation ownership updated.",
            changes={"before": profile_before, "after": profile_after},
        )
        messages.success(self.request, f"Operator context for {self.schedule['name']} was updated. Scheduler Master was not changed.")
        return redirect("scheduler:detail", job_id=self.schedule["id"])


class QueueView(LoginRequiredMixin, SchedulerSourceMixin, TemplateView):
    template_name = "scheduler/queue.html"
    paginate_by = 25

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        is_history = self.kwargs.get("history", False)
        selected_date = _parse_date(self.request.GET.get("date"))
        selected_status = self.kwargs.get("forced_status") or self.request.GET.get("status", "").upper()
        records = (
            self.adapter.get_executions(report_date=selected_date, status=selected_status)
            if is_history else self.adapter.get_ready(report_date=selected_date)
        )
        page_obj = Paginator(records, self.paginate_by).get_page(self.request.GET.get("page"))
        scheduler_status = self.adapter.get_status()
        live_queue = self.adapter.get_ready() if not is_history else []
        schedules = self.adapter.get_schedules() if not is_history else []
        context.update({
            "is_history": is_history,
            "live_queue": live_queue,
            "paused_schedules": [item for item in schedules if item["operational_state"] == "PAUSED"],
            "can_reorder": is_operator(self.request.user) and scheduler_status.get("label") == "BACKGROUND SCHEDULER API",
            "queue_revision": scheduler_status.get("queue_revision", ""),
            "records": page_obj,
            "page_obj": page_obj,
            "queue_size": len(live_queue),
            "scheduler": scheduler_status,
            "selected_date": selected_date.isoformat() if selected_date else "",
            "selected_status": selected_status,
            "statuses": ["SUCCESS", "FAILED", "RUNNING"],
        })
        if not is_history and not live_queue:
            context["queue_diagnostics"] = self._empty_queue_diagnostics(schedules, scheduler_status, context)
        return context

    def _empty_queue_diagnostics(self, schedules, status, context):
        """Explain scheduler-owned state without deriving or changing eligibility."""
        active_ids = {item["id"] for item in schedules if item["is_active"]}
        waiting = [
            item for item in self.adapter.get_staging()
            if item["id"] in active_ids and item["operational_state"] not in {"PAUSED", "CANCELLED", "DISABLED"}
        ]
        live = status.get("label") == "BACKGROUND SCHEDULER API" and status.get("available")
        diagnostics = {
            "title": "No tasks are ready to run.",
            "detail": "Only eligible occurrences receive a queue position. Review the recorded gates below or open the daily workspace.",
            "total": len(schedules), "active": len(active_ids), "inactive": len(schedules) - len(active_ids),
            "waiting_count": len(waiting), "waiting": waiting[:5], "more_waiting": len(waiting) > 5,
            "running": status.get("running", 0), "live": bool(live), "today": timezone.localdate(),
        }
        if not live:
            diagnostics.update(title="Live queue status is unavailable.",
                               detail="The portal cannot confirm the worker's current queue. Any configuration and waiting records below are the last saved state; reconnect the scheduler to refresh them.")
        elif status.get("cycle_status") == "ERROR":
            diagnostics.update(title="The last scheduler cycle failed.",
                               detail="The displayed queue may be out of date. Resolve the scheduler error before relying on these recorded waiting states.")
        elif (status.get("service_control") or {}).get("scheduler_enabled") is False:
            diagnostics.update(title="The scheduler is stopped.",
                               detail="Future cycles are paused by service control. Resume the scheduler from the dashboard to reevaluate eligible tasks.")
        elif not schedules:
            diagnostics.update(title="No schedules are configured.",
                               detail="Add a schedule in the schedules workspace before the worker can build its queue.")
        elif not active_ids:
            diagnostics.update(title="All configured schedules are inactive.",
                               detail="Activate the required schedule in its configuration to let the worker evaluate it.")

        if live:
            today = diagnostics["today"]
            payload = self.adapter.get_operations_calendar(today, 1)
            # The shared confirmation alert can reuse this exact one-day read.
            context.update(calendar_payload=payload, calendar_range_start=today, calendar_range_end=today)
            if not payload.get("error"):
                diagnostics["calendar_day"] = next(
                    (day for day in payload.get("calendar_days", []) if str(day.get("date")) == today.isoformat()), None,
                )
        return diagnostics


class JobActionView(LoginRequiredMixin, SchedulerSourceMixin, View):
    """Record operator intent in job_control; the scheduler performs all queue work."""

    messages_for_action = {
        "pause": "Pause requested. The schedule will stop becoming READY on the scheduler's next cycle.",
        "resume": "Resume requested. The scheduler will reevaluate this schedule on its next cycle.",
        "cancel": "Cancellation requested. The scheduler will remove this occurrence from eligibility on its next cycle.",
        "activate": "Schedule control was reactivated.",
        "manual_run": "Manual run requested. The scheduler will check eligibility on its next cycle.",
        "clear_manual_run": "Pending manual-run request cleared.",
        "confirm": "Confirmation recorded. The scheduler will reevaluate this schedule on its next cycle.",
        "clear_confirmation": "Confirmation cleared.",
        "reset": "Temporary scheduler controls were reset. Schedule Master was not changed.",
        "set_override": "Job-specific evaluation time recorded. The system clock and report-date marker were not changed.",
        "clear_override": "Job-specific evaluation time cleared.",
    }

    def post(self, request, job_id, action):
        schedule = self.adapter.get_schedule(job_id)
        if request.POST.get("occurrence_key"):
            schedule = self.adapter.get_schedule_occurrence(job_id, request.POST["occurrence_key"]) or schedule
        if schedule is None:
            raise Http404("Schedule not found")
        action = "manual_run" if action == "rerun" else action.lower()
        if action not in self.messages_for_action:
            messages.error(request, "That scheduler control is not supported.")
            return redirect("scheduler:detail", job_id=job_id)

        form = SchedulerControlForm(request.POST)
        if not form.is_valid():
            messages.error(request, "Enter a valid date, time, and reason (up to 500 characters).")
            return redirect("scheduler:detail", job_id=job_id)
        override_datetime = form.cleaned_data.get("override_datetime") if form.is_valid() else None
        reason = form.cleaned_data.get("reason", "") if form.is_valid() else request.POST.get("reason", "")
        reason = str(reason or "").strip()
        if action == "set_override" and not override_datetime:
            messages.error(request, "Choose an evaluation date and time before saving an override.")
            return redirect("scheduler:detail", job_id=job_id)
        try:
            SchedulerService(self.adapter, self.control_client).control(
                schedule, action, request.user, override_datetime=override_datetime,
                reason=reason, occurrence_key=request.POST.get("occurrence_key") or None,
            )
        except (SchedulerApiError, ValidationError) as error:
            messages.error(request, error.messages[0] if isinstance(error, ValidationError) else str(error))
            return redirect("scheduler:detail", job_id=job_id)
        messages.success(request, self.messages_for_action[action])
        return redirect("scheduler:detail", job_id=job_id)


class RunbookActionView(LoginRequiredMixin, SchedulerSourceMixin, View):
    def post(self, request, job_id):
        schedule = self.adapter.get_schedule(job_id)
        if schedule is None:
            raise Http404("Schedule not found")
        report_day = _parse_date(request.POST.get("report_date"))
        try:
            if not report_day:
                raise ValidationError("Choose a business report date for this work log.")
            if request.POST.get("kind") == "note":
                if not is_operator(request.user):
                    raise PermissionDenied("An active operator account is required.")
                note = request.POST.get("note", "").strip()
                if not note or len(note) > 2000:
                    raise ValidationError("Enter a note of 1 to 2,000 characters.")
                record_external_action(request.user, "WORK_NOTE_ADDED", object_type="scheduler.WorkNote",
                                       object_id=str(job_id), object_label=schedule["name"], reason=note,
                                       changes={"report_date": report_day.isoformat()})
            else:
                index = int(request.POST.get("step_index", "-1"))
                record_runbook_step(schedule, request.user, report_date=report_day, step_index=index,
                                    completed=request.POST.get("completed") == "1", expected_step=request.POST.get("expected_step"))
        except (ValidationError, ValueError) as error:
            messages.error(request, error.messages[0] if isinstance(error, ValidationError) else "Select a valid runbook step.")
        else:
            messages.success(request, "Work log saved with your name and timestamp.")
        response = redirect("scheduler:detail", job_id=job_id)
        if report_day:
            response["Location"] += f"?report_date={report_day.isoformat()}"
        return response


class QueueReorderView(LoginRequiredMixin, SchedulerSourceMixin, View):
    def post(self, request):
        if not is_operator(request.user):
            raise PermissionDenied("An active operations account is required.")
        keys = request.POST.getlist("occurrence_keys")
        try:
            result = self.control_client.reorder_queue(keys, request.POST.get("queue_revision", ""),
                                                       actor=request.user.get_username(), reason="Operator changed queue order")
        except SchedulerApiError as error:
            messages.error(request, str(error))
        else:
            record_external_action(request.user, "QUEUE_REORDERED", object_type="scheduler.Queue",
                                   object_id="priority-queue", object_label="Live priority queue",
                                   changes={"occurrence_keys": keys, "queue_revision": result.get("queue_revision")})
            messages.success(request, "Queue order saved. The worker will use it at its next selection.")
        return redirect("scheduler:queue")


class DailyTasksView(LoginRequiredMixin, SchedulerSourceMixin, TemplateView):
    template_name = "scheduler/day.html"

    def get_context_data(self, **kwargs):
        from datetime import timedelta
        from .daybook import daybook_rows, daybook_counts, pending_confirmation
        context = super().get_context_data(**kwargs)
        day = _parse_date(self.request.GET.get("date")) or timezone.localdate()
        payload = self.adapter.get_operations_calendar(day, 1)
        rows = daybook_rows(payload, self.adapter, self.request.user, day)
        counts = daybook_counts(rows)
        confirmations = self.kwargs.get("confirmations", False)
        selected_status = self.request.GET.get("status", "")
        if confirmations:
            rows = [row for row in rows if pending_confirmation(row)]
        if selected_status:
            rows = [row for row in rows if row["display_status"] == selected_status]
        if self.request.GET.get("mine") == "1":
            rows = [row for row in rows if row["responsibility"]["operator"] == self.request.user]
        query = self.request.GET.get("q", "").strip()
        if query:
            rows = [row for row in rows if query.lower() in row["name"].lower() or query in str(row["id"])]
        context.update(day=day, previous_day=day-timedelta(days=1), next_day=day+timedelta(days=1),
                       rows=rows, counts=counts, confirmations=confirmations, selected_status=selected_status,
                       query=query, mine=self.request.GET.get("mine") == "1", scheduler=self.adapter.get_status(),
                       calendar_meta=payload.get("calendar", {}), calendar_error=payload.get("error", ""),
                       projection_available=payload.get("projection_available", False),
                       statuses=["Pending", "Confirmation", "Ready", "Running", "Done", "Failed", "Paused", "Cancelled", "Manual required"])
        return context
