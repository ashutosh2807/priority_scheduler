"""Schedule management through the worker; the portal never writes Oracle directly."""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect
from django.views import View
from django.views.generic import FormView
from django.db import transaction

from apps.audit.services import record_external_action
from .control_client import SchedulerApiError
from .forms import ScheduleDefinitionForm, ScheduleDeleteForm
from .models import ScheduleProfile
from .services import is_manager
from .views import SchedulerSourceMixin, _run_by_values, _save_profile


class ScheduleManagementMixin(LoginRequiredMixin, SchedulerSourceMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not is_manager(request.user):
            raise PermissionDenied("Schedule management is restricted to SUPERUSER users.")
        self.schedule = None
        if kwargs.get("job_id"):
            self.schedule = self.adapter.get_schedule(kwargs["job_id"])
            if self.schedule is None:
                raise Http404("Schedule not found")
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        scheduler = self.adapter.get_status()
        context.update(schedule=self.schedule, scheduler=scheduler,
                       configuration_available=scheduler.get("label") == "BACKGROUND SCHEDULER API")
        return context

    def warn_if_pending(self, result):
        if result.get("warning"):
            messages.warning(self.request, result["warning"])


class ScheduleDefinitionView(ScheduleManagementMixin, FormView):
    template_name = "scheduler/definition_form.html"
    form_class = ScheduleDefinitionForm

    def get_initial(self):
        if not self.schedule:
            return {"is_active": True, "margin": "T+1", "frequencies": ["DAILY"], "max_attempts": 3}
        schedule = self.schedule
        config = {str(key).upper(): value for key, value in schedule.get("run_config", {}).items()}
        dates = config.get("SPECIFIC_DATES", config.get("SPECIFIC_DATE", [])) or []
        if isinstance(dates, str):
            dates = [dates]
        frequencies = config.get("RUNS_ON", [])
        if isinstance(frequencies, str):
            frequencies = [frequencies]
        aliases = {"BI_ANNUALLY": "HALF-YEARLY", "BI-ANNUALLY": "HALF-YEARLY", "HALF_YEARLY": "HALF-YEARLY",
                   "ON_SPECIFIC_DATE": "SPECIFIC_DATE", "ON A SPECIFIC DATE": "SPECIFIC_DATE"}
        initial = {**{key: schedule.get(key) for key in ("name", "package_name", "margin", "same_day", "is_active", "confirmation_needed")},
                   "schedule_id": schedule["id"], "frequencies": [aliases.get(value, value) for value in frequencies],
                   "holiday_run": config.get("HOLIDAY_RUN", []), "specific_dates": "\n".join(dates),
                   "max_attempts": config.get("MAX_ATTEMPTS", 3), **_run_by_values(schedule)}
        profile = ScheduleProfile.objects.filter(schedule_id=schedule["id"]).first()
        if profile:
            initial.update(description=profile.description, operational_steps="\n".join(profile.operational_steps),
                           responsible_operator=profile.primary_operator_id, backup_operator=profile.backup_operator_id,
                           expected_minutes=profile.expected_minutes)
        return initial

    def get_form_kwargs(self):
        return {**super().get_form_kwargs(), "editing": bool(self.schedule)}

    def form_valid(self, form):
        creating = self.schedule is None
        schedule_id = form.cleaned_data["schedule_id"]
        if creating and (self.adapter.get_schedule(schedule_id) or ScheduleProfile.objects.filter(schedule_id=schedule_id).exists()):
            form.add_error("schedule_id", "This ID is already used. Choose a new schedule ID.")
            return self.form_invalid(form)
        definition = form.definition()
        metadata = {"actor": self.request.user.get_username(), "reason": form.cleaned_data["reason"]}
        try:
            if creating:
                result = self.control_client.create_schedule({"id": schedule_id, **definition}, **metadata)
            else:
                result = self.control_client.update_schedule(schedule_id, definition, **metadata)
        except SchedulerApiError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        with transaction.atomic():
            profile_before, profile_after = _save_profile(schedule_id, form.cleaned_data, self.request.user)
            record_external_action(self.request.user, "SCHEDULE_CREATED" if creating else "SCHEDULE_DEFINITION_UPDATED",
                                   object_type="scheduler.ScheduleMaster", object_id=str(schedule_id),
                                   object_label=definition["name"], reason=metadata["reason"],
                                   changes={"before": result.get("before"), "after": result.get("after"),
                                            "profile_before": profile_before, "profile_after": profile_after, "source": result.get("source")})
        destination = "Oracle" if result.get("source") == "oracle" else "the scheduler"
        messages.success(self.request, f"Schedule {'created' if creating else 'updated'} in {destination}.")
        self.warn_if_pending(result)
        return redirect("scheduler:detail", job_id=schedule_id)


class ScheduleDeleteView(ScheduleManagementMixin, FormView):
    template_name = "scheduler/delete_form.html"
    form_class = ScheduleDeleteForm

    def form_valid(self, form):
        if form.cleaned_data["confirm_name"] != self.schedule["name"]:
            form.add_error("confirm_name", "The name does not match this schedule.")
            return self.form_invalid(form)
        try:
            result = self.control_client.delete_schedule(self.schedule["id"], actor=self.request.user.get_username(),
                                                         reason=form.cleaned_data["reason"])
        except SchedulerApiError as error:
            form.add_error(None, str(error))
            return self.form_invalid(form)
        record_external_action(self.request.user, "SCHEDULE_DELETED", object_type="scheduler.ScheduleMaster",
                               object_id=str(self.schedule["id"]), object_label=self.schedule["name"],
                               reason=form.cleaned_data["reason"], changes={"before": result.get("before"), "source": result.get("source")})
        messages.success(self.request, "Schedule deleted. Execution history, work logs and audit records are retained.")
        self.warn_if_pending(result)
        return redirect("scheduler:jobs")


class CalendarRefreshView(ScheduleManagementMixin, View):
    def post(self, request):
        reason = "Operator refreshed the daily DATEMAST snapshot from the calendar"
        try:
            result = self.control_client.refresh_calendar(actor=request.user.get_username(), reason=reason)
        except SchedulerApiError as error:
            messages.error(request, str(error))
        else:
            record_external_action(request.user, "CALENDAR_REFRESHED", object_type="scheduler.Calendar",
                                   object_id="datemast", object_label="DATEMAST", reason=reason, changes=result)
            if result.get("refreshed"):
                messages.success(request, "Calendar refresh completed. The current Oracle DATEMAST entries are now used.")
            else:
                messages.warning(request, result.get("error") or "The calendar could not be refreshed. Review the source status and try again.")
        return redirect("dashboard:calendar")
