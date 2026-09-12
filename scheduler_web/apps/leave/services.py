from django.core.exceptions import ValidationError
from django.utils import timezone
from django.db import transaction

from apps.accounts.models import AdminUser
from apps.audit.services import record_action
from apps.scheduler.file_adapter import get_scheduler_read_adapter
from apps.scheduler.services import is_manager
from apps.scheduler.models import ScheduleProfile

from .models import LeaveRequest, LeaveScheduleCoverage, LeaveStatus


def can_approve(user) -> bool:
    return is_manager(user)


@transaction.atomic
def approve(request: LeaveRequest, actor) -> None:
    if not can_approve(actor):
        raise ValidationError("Only an active SUPERUSER can approve leave.")
    request = LeaveRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != LeaveStatus.PENDING:
        raise ValidationError("This leave request has already been actioned.")
    # A single lock order serializes coverage decisions on databases with row locks.
    list(AdminUser.objects.select_for_update().order_by("pk"))
    available = list(available_covering_administrators(request))
    for schedule in uncovered_confirmation_schedules(request):
        profile = schedule["profile"]
        cover = next((user for user in available if user.pk == profile.backup_operator_id), None)
        cover = cover or (available[0] if available else None)
        if cover:
            assign_schedule_coverage(request, schedule_id=schedule["id"], covering_admin=cover, actor=actor)
    uncovered = uncovered_confirmation_schedules(request)
    if uncovered:
        names = ", ".join(schedule["name"] for schedule in uncovered[:3])
        suffix = "…" if len(uncovered) > 3 else ""
        raise ValidationError(
            f"Assign available cover for {names}{suffix} before approving this leave."
        )
    request.status = LeaveStatus.APPROVED
    request.approved_by = actor
    request.approved_at = timezone.now()
    request.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    record_action(actor, "LEAVE_APPROVED", request, reason=request.reason)


@transaction.atomic
def reject(request: LeaveRequest, actor) -> None:
    if not can_approve(actor):
        raise ValidationError("Only an active SUPERUSER can reject leave.")
    request = LeaveRequest.objects.select_for_update().get(pk=request.pk)
    if request.status != LeaveStatus.PENDING:
        raise ValidationError("This leave request has already been actioned.")
    request.status = LeaveStatus.REJECTED
    request.approved_by = actor
    request.approved_at = timezone.now()
    request.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
    record_action(actor, "LEAVE_REJECTED", request, reason=request.reason)


@transaction.atomic
def cancel(request: LeaveRequest, actor) -> None:
    request = LeaveRequest.objects.select_for_update().get(pk=request.pk)
    if not (can_approve(actor) or actor.pk == request.admin_id):
        raise ValidationError("You cannot cancel this leave request.")
    if request.status not in {LeaveStatus.PENDING, LeaveStatus.APPROVED}:
        raise ValidationError("This leave request cannot be cancelled.")
    request.status = LeaveStatus.CANCELLED
    request.save(update_fields=["status", "updated_at"])
    record_action(actor, "LEAVE_CANCELLED", request, reason=request.reason)


def available_covering_administrators(leave_request: LeaveRequest):
    """Return active ITRP operators available throughout the requested leave.

    Only approved leave changes operational availability.
    """

    unavailable_admin_ids = LeaveRequest.objects.filter(
        status=LeaveStatus.APPROVED,
        start_date__lte=leave_request.end_date,
        end_date__gte=leave_request.start_date,
    ).exclude(pk=leave_request.pk).values("admin_id")
    return AdminUser.objects.filter(
        is_active=True,
        is_active_admin=True,
    ).exclude(
        pk=leave_request.admin_id,
    ).exclude(
        pk__in=unavailable_admin_ids,
    ).order_by("employee_id", "pk")


def confirmation_schedules_for_leave(leave_request: LeaveRequest) -> list[dict]:
    """Return only confirmation gates owned by the administrator on leave.

    Schedule Master remains the authority for whether a confirmation is needed;
    ScheduleProfile owns the human responsibility mapping in Django.
    """

    profiles = {
        profile.schedule_id: profile
        for profile in ScheduleProfile.objects.filter(
            primary_operator_id=leave_request.admin_id
        ).select_related("primary_operator")
    }
    if not profiles:
        return []

    adapter = get_scheduler_read_adapter()
    schedules = adapter.get_schedules()
    if not adapter.get_status().get("available"):
        # Unknown scheduler rules must not be interpreted as no coverage needed.
        return [{"id": key, "name": f"Schedule #{key}", "confirmation_needed": True,
                 "profile": profile, "source_unavailable": True} for key, profile in profiles.items()]
    return [
        {
            **schedule,
            "profile": profiles[schedule["id"]],
        }
        for schedule in schedules
        if schedule["id"] in profiles and schedule["confirmation_needed"]
    ]


def uncovered_confirmation_schedules(leave_request: LeaveRequest) -> list[dict]:
    available_ids = available_covering_administrators(leave_request).values_list("pk", flat=True)
    covered_ids = set(
        leave_request.schedule_coverages.filter(covering_admin_id__in=available_ids).values_list("schedule_id", flat=True)
    )
    return [
        schedule
        for schedule in confirmation_schedules_for_leave(leave_request)
        if schedule["id"] not in covered_ids
    ]


def coverage_is_complete(leave_request: LeaveRequest) -> bool:
    return not uncovered_confirmation_schedules(leave_request)


def validate_schedule_coverage(
    leave_request: LeaveRequest,
    *,
    schedule_id: int,
    covering_admin,
    actor,
) -> dict:
    """Shared validation for portal services and Django admin forms."""
    if leave_request.status != LeaveStatus.PENDING:
        raise ValidationError("Coverage can only be changed while leave is pending.")
    if not (can_approve(actor) or actor.pk == leave_request.admin_id):
        raise ValidationError("You cannot change this leave coverage.")

    schedule = next(
        (
            item
            for item in confirmation_schedules_for_leave(leave_request)
            if item["id"] == int(schedule_id)
        ),
        None,
    )
    if schedule is None:
        raise ValidationError(
            "Only confirmation-required schedules owned by the administrator "
            "on leave can be assigned as coverage."
        )

    available_ids = set(
        available_covering_administrators(leave_request).values_list("pk", flat=True)
    )
    if covering_admin.pk not in available_ids:
        raise ValidationError(
            "Choose an active administrator who is available for the full leave period."
        )

    return schedule


@transaction.atomic
def assign_schedule_coverage(
    leave_request: LeaveRequest,
    *,
    schedule_id: int,
    covering_admin,
    actor,
) -> LeaveScheduleCoverage:
    leave_request = LeaveRequest.objects.select_for_update().get(pk=leave_request.pk)
    list(AdminUser.objects.select_for_update().order_by("pk"))
    schedule = validate_schedule_coverage(
        leave_request, schedule_id=schedule_id, covering_admin=covering_admin, actor=actor,
    )

    coverage, created = LeaveScheduleCoverage.objects.update_or_create(
        leave_request=leave_request,
        schedule_id=schedule["id"],
        defaults={
            "schedule_name": schedule["name"],
            "covering_admin": covering_admin,
            "assigned_by": actor if getattr(actor, "is_authenticated", False) else None,
        },
    )
    record_action(
        actor,
        "LEAVE_SCHEDULE_COVER_ASSIGNED" if created else "LEAVE_SCHEDULE_COVER_REASSIGNED",
        coverage,
        reason=(
            f"{schedule['name']} confirmation coverage for "
            f"{leave_request.start_date:%d %b %Y} to {leave_request.end_date:%d %b %Y}"
        ),
    )
    return coverage


@transaction.atomic
def remove_schedule_coverage(coverage: LeaveScheduleCoverage, actor) -> None:
    leave_request = LeaveRequest.objects.select_for_update().get(pk=coverage.leave_request_id)
    if leave_request.status != LeaveStatus.PENDING:
        raise ValidationError("Coverage can only be changed while leave is pending.")
    if not (can_approve(actor) or actor.pk == leave_request.admin_id):
        raise ValidationError("You cannot change this leave coverage.")
    coverage = LeaveScheduleCoverage.objects.select_for_update().get(pk=coverage.pk)
    record_action(
        actor,
        "LEAVE_SCHEDULE_COVER_REMOVED",
        coverage,
        reason=f"Removed coverage for {coverage.schedule_name}",
    )
    coverage.delete()
