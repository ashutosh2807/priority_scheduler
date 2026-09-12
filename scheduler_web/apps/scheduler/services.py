"""Human responsibility and audited controls; scheduling remains in the worker."""
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import AdminRole, AdminUser
from apps.audit.services import record_external_action
from .models import ScheduleProfile, RunbookProgress


def is_operator(user):
    return bool(user.is_authenticated and user.is_active and user.is_active_admin)


def is_manager(user):
    return is_operator(user) and (user.is_superuser or user.role == AdminRole.SUPERUSER)


def available_operators(on_date):
    from apps.leave.models import LeaveRequest, LeaveStatus
    absent = LeaveRequest.objects.filter(
        status=LeaveStatus.APPROVED, start_date__lte=on_date, end_date__gte=on_date,
    ).values("admin_id")
    return AdminUser.objects.filter(is_active=True, is_active_admin=True).exclude(
        pk__in=absent
    ).order_by("employee_id", "pk")


class ResponsibilityResolver:
    """Batch-read people and coverage once for a daily or monthly workspace."""
    def __init__(self, schedule_ids=None):
        from apps.leave.models import LeaveRequest, LeaveScheduleCoverage, LeaveStatus
        profiles = ScheduleProfile.objects.select_related("primary_operator", "backup_operator")
        if schedule_ids is not None:
            profiles = profiles.filter(schedule_id__in=schedule_ids)
        self.profiles = {profile.schedule_id: profile for profile in profiles}
        self.people = list(AdminUser.objects.filter(is_active=True, is_active_admin=True).order_by("employee_id", "pk"))
        self.leaves = list(LeaveRequest.objects.filter(status=LeaveStatus.APPROVED))
        self.covers = list(LeaveScheduleCoverage.objects.filter(
            schedule_id__in=self.profiles, leave_request__status=LeaveStatus.APPROVED
        ).select_related("leave_request").order_by("-created_at", "-pk"))
        self.available_by_day = {}

    def resolve(self, schedule_id, on_date=None):
        on_date = on_date or timezone.localdate()
        profile = self.profiles.get(schedule_id)
        result = {"operator": None, "source": "Unassigned", "date": on_date,
                  "reason": "A SUPERUSER must assign a primary confirmation operator.", "profile": profile}
        if not profile or not profile.primary_operator_id:
            return result
        if on_date not in self.available_by_day:
            absent = {leave.admin_id for leave in self.leaves if leave.start_date <= on_date <= leave.end_date}
            self.available_by_day[on_date] = {person.pk: person for person in self.people if person.pk not in absent}
        candidates = self.available_by_day[on_date]
        if profile.primary_operator_id in candidates:
            result.update(operator=candidates[profile.primary_operator_id], source="Primary operator",
                          reason="Primary operator is available on this operating day.")
            return result
        for cover in self.covers:
            leave = cover.leave_request
            if cover.schedule_id == schedule_id and leave.admin_id == profile.primary_operator_id and leave.start_date <= on_date <= leave.end_date and cover.covering_admin_id in candidates:
                result.update(operator=candidates[cover.covering_admin_id], source="Nominated leave cover",
                              reason="Cover selected for the primary operator's approved leave.")
                return result
        if profile.backup_operator_id in candidates:
            result.update(operator=candidates[profile.backup_operator_id], source="Preferred cover",
                          reason="Primary operator is unavailable; preferred cover is available.")
        elif candidates:
            result.update(operator=next(iter(candidates.values())), source="Available operator",
                          reason="Primary and nominated cover are unavailable. Cover is selected by employee ID.")
        else:
            result.update(reason="No active operator is available. Arrange cover before confirmation.")
        return result


def confirmation_responsibility(schedule_id, on_date=None):
    return ResponsibilityResolver([schedule_id]).resolve(schedule_id, on_date)


def may_edit_profile(user, schedule_id):
    return is_manager(user) or (is_operator(user) and ScheduleProfile.objects.filter(
        schedule_id=schedule_id, primary_operator=user
    ).exists())


def decorate_schedule(schedule, user, on_date=None, resolver=None):
    schedule = dict(schedule)
    responsibility = resolver.resolve(schedule["id"], on_date) if resolver else confirmation_responsibility(schedule["id"], on_date)
    schedule["responsibility"] = responsibility
    schedule["can_confirm"] = bool(is_operator(user) and responsibility["operator"] == user)
    schedule["can_operate"] = is_operator(user)
    profile = responsibility["profile"]
    schedule["expected_minutes"] = profile.expected_minutes if profile else None
    return schedule


class SchedulerService:
    def __init__(self, adapter=None, client=None):
        self.adapter = adapter
        self.client = client

    def control(self, schedule, action, actor, *, override_datetime=None, reason="", occurrence_key=None):
        if not is_operator(actor):
            raise PermissionDenied("An active operations account is required.")
        if action in {"reset", "set_override", "clear_override"} and not is_manager(actor):
            raise PermissionDenied("Only a SUPERUSER can reset controls or change evaluation time.")
        responsibility = None
        if action in {"confirm", "clear_confirmation"}:
            responsibility = confirmation_responsibility(schedule["id"], timezone.localdate())
            if not schedule["confirmation_needed"] or responsibility["operator"] != actor:
                raise PermissionDenied("Only the available confirmation assignee can confirm this task.")
            current = (schedule.get("occurrence") or {}).get("occurrence_key")
            if not current or not occurrence_key or current != occurrence_key:
                raise ValidationError("This occurrence has changed. Refresh the task before confirming.")
        if action in {"manual_run", "cancel", "reset", "set_override"} and not reason:
            raise ValidationError("Enter an operational reason for this action.")
        kwargs = {"override_datetime": override_datetime, "actor": actor.get_username(), "reason": reason or None}
        if occurrence_key:
            kwargs["occurrence_key"] = occurrence_key
        after = self.client.control(schedule["id"], action, **kwargs)
        changes = {"before": schedule["control"], "after": after, "occurrence_key": occurrence_key,
                   "report_date": str((schedule.get("occurrence") or {}).get("report_date") or "")}
        if responsibility:
            changes["confirmation_assignment"] = {"employee_id": actor.employee_id, "source": responsibility["source"]}
        record_external_action(actor, f"SCHEDULE_{action.upper()}", object_type="scheduler.ScheduleMaster",
                               object_id=str(schedule["id"]), object_label=schedule["name"], reason=reason, changes=changes)
        return after


@transaction.atomic
def record_runbook_step(schedule, actor, *, report_date, step_index, completed, expected_step):
    if not is_operator(actor):
        raise PermissionDenied("An active operations account is required.")
    profile = ScheduleProfile.objects.select_for_update().filter(schedule_id=schedule["id"]).first()
    if not profile or not 0 <= step_index < len(profile.operational_steps):
        raise ValidationError("This runbook has changed. Refresh and select a current step.")
    text = profile.operational_steps[step_index]
    if expected_step != text:
        raise ValidationError("This guide step has changed. Refresh before recording your check.")
    progress, _ = RunbookProgress.objects.update_or_create(
        schedule_id=schedule["id"], report_date=report_date, step_index=step_index,
        defaults={"completed": completed, "step_text": text, "actor": actor},
    )
    record_external_action(actor, "RUNBOOK_STEP_COMPLETED" if completed else "RUNBOOK_STEP_REOPENED",
                           object_type="scheduler.RunbookProgress", object_id=str(schedule["id"]),
                           object_label=schedule["name"], reason=text,
                           changes={"report_date": report_date.isoformat(), "step": step_index + 1, "completed": completed})
    return progress
