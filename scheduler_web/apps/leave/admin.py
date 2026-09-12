"""Admin forms use the same leave workflow and durable audit as the portal."""
from django import forms
from django.contrib import admin, messages
from django.core.exceptions import ValidationError
from django.db import transaction

from apps.accounts.models import AdminUser
from apps.audit.services import object_snapshot, record_action

from . import services
from .models import LeaveRequest, LeaveScheduleCoverage, LeaveStatus


def may_manage(user, leave):
    return services.can_approve(user) or user.pk == leave.admin_id


class LeaveAdminForm(forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = ("admin", "start_date", "end_date", "reason")

    def clean(self):
        values = super().clean()
        if self.instance.pk:
            # Admin POST already owns a transaction; lock before validation so
            # approval cannot race a pending request's date/coverage edit.
            original = LeaveRequest.objects.select_for_update().get(pk=self.instance.pk)
            if original.status != LeaveStatus.PENDING:
                raise ValidationError("Actioned leave is read-only. Cancel it and create a new request if dates change.")
            if {"start_date", "end_date"}.intersection(self.changed_data) and values.get("start_date") and values.get("end_date"):
                list(AdminUser.objects.select_for_update().order_by("pk"))
                proposed = LeaveRequest(pk=original.pk, admin=original.admin,
                                        start_date=values["start_date"], end_date=values["end_date"])
                available = services.available_covering_administrators(proposed).values_list("pk", flat=True)
                if original.schedule_coverages.exclude(covering_admin_id__in=available).exists():
                    raise ValidationError("Existing cover is unavailable for the new dates. Remove or reassign that cover before changing the period.")
        return values


class CoverageAdminForm(forms.ModelForm):
    actor = None

    class Meta:
        model = LeaveScheduleCoverage
        fields = ("leave_request", "schedule_id", "covering_admin")
        help_texts = {
            "schedule_id": "Use the ID of a confirmation-required schedule owned by the operator on leave.",
            "covering_admin": "The operator must be active and available for the entire leave period.",
        }

    def clean(self):
        values = super().clean()
        leave = values.get("leave_request") or (self.instance.leave_request if self.instance.leave_request_id else None)
        schedule_id = values.get("schedule_id", self.instance.schedule_id)
        covering_admin = values.get("covering_admin")
        if leave and schedule_id and covering_admin:
            leave = LeaveRequest.objects.select_for_update().get(pk=leave.pk)
            list(AdminUser.objects.select_for_update().order_by("pk"))
            services.validate_schedule_coverage(
                leave, schedule_id=schedule_id, covering_admin=covering_admin, actor=self.actor,
            )
        return values


@admin.register(LeaveRequest)
class LeaveRequestAdmin(admin.ModelAdmin):
    form = LeaveAdminForm
    list_display = ("admin", "start_date", "end_date", "status", "approved_by", "approved_at")
    list_filter = ("status", "start_date", "end_date")
    search_fields = ("admin__display_name", "admin__employee_id", "admin__username", "reason")
    autocomplete_fields = ("admin",)
    list_select_related = ("admin", "approved_by")
    list_per_page = 25
    date_hierarchy = "start_date"
    fields = ("admin", "start_date", "end_date", "reason", "status", "approved_by", "approved_at", "created_at", "updated_at")
    readonly_fields = ("status", "approved_by", "approved_at", "created_at", "updated_at")
    actions = ("approve_requests", "reject_requests", "cancel_requests")

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if services.can_approve(request.user) else queryset.filter(admin=request.user)

    def get_readonly_fields(self, request, obj=None):
        fields = self.readonly_fields
        if obj is not None:
            fields += ("admin",)
            if obj.status != LeaveStatus.PENDING:
                fields += ("start_date", "end_date", "reason")
        return fields

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "admin" and not services.can_approve(request.user):
            kwargs["queryset"] = AdminUser.objects.filter(pk=request.user.pk)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or (may_manage(request.user, obj) and obj.status == LeaveStatus.PENDING)
        )

    def has_delete_permission(self, request, obj=None):
        return False  # Cancel requests instead; preserve approval and coverage history.

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        before = object_snapshot(LeaveRequest.objects.select_for_update().get(pk=obj.pk)) if change else None
        if not change:
            obj.status = LeaveStatus.PENDING
            obj.approved_by = None
            obj.approved_at = None
        super().save_model(request, obj, form, change)
        record_action(request.user, "LEAVE_UPDATED" if change else "LEAVE_CREATED", obj, reason=obj.reason,
                      changes={"before": before, "after": object_snapshot(obj), "source": "DJANGO_ADMIN"})

    def get_actions(self, request):
        actions = super().get_actions(request)
        if not services.can_approve(request.user):
            actions.pop("approve_requests", None)
            actions.pop("reject_requests", None)
        return actions

    def _transition(self, request, queryset, operation, verb):
        try:
            with transaction.atomic():
                count = 0
                for leave in queryset.select_for_update().order_by("pk"):
                    operation(leave, request.user)
                    leave.refresh_from_db()
                    self.log_change(request, leave, f"Leave {verb} through the validated workflow.")
                    count += 1
        except ValidationError as error:
            self.message_user(request, " ".join(error.messages), level=messages.ERROR)
        else:
            self.message_user(request, f"{count} leave request(s) {verb}.", level=messages.SUCCESS)

    @admin.action(description="Approve selected pending requests", permissions=["change"])
    def approve_requests(self, request, queryset):
        self._transition(request, queryset, services.approve, "approved")

    @admin.action(description="Reject selected pending requests", permissions=["change"])
    def reject_requests(self, request, queryset):
        self._transition(request, queryset, services.reject, "rejected")

    @admin.action(description="Cancel selected pending or approved requests", permissions=["change"])
    def cancel_requests(self, request, queryset):
        self._transition(request, queryset, services.cancel, "cancelled")


@admin.register(LeaveScheduleCoverage)
class LeaveScheduleCoverageAdmin(admin.ModelAdmin):
    form = CoverageAdminForm
    list_display = ("schedule_name", "leave_request", "covering_admin", "assigned_by", "created_at")
    list_filter = ("leave_request__status", "leave_request__start_date")
    search_fields = ("schedule_name", "=schedule_id", "covering_admin__display_name", "covering_admin__employee_id", "leave_request__admin__display_name")
    autocomplete_fields = ("leave_request", "covering_admin")
    list_select_related = ("leave_request__admin", "covering_admin", "assigned_by")
    list_per_page = 25
    fields = ("leave_request", "schedule_id", "covering_admin", "schedule_name", "assigned_by", "created_at")
    readonly_fields = ("schedule_name", "assigned_by", "created_at")
    actions = ("remove_coverages",)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        return queryset if services.can_approve(request.user) else queryset.filter(leave_request__admin=request.user)

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields + (("leave_request", "schedule_id") if obj else ())

    def get_form(self, request, obj=None, **kwargs):
        parent = super().get_form(request, obj, **kwargs)
        class ActorForm(parent):
            actor = request.user
        return ActorForm

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        if db_field.name == "leave_request":
            queryset = LeaveRequest.objects.filter(status=LeaveStatus.PENDING)
            if not services.can_approve(request.user):
                queryset = queryset.filter(admin=request.user)
            kwargs["queryset"] = queryset
        elif db_field.name == "covering_admin":
            kwargs["queryset"] = AdminUser.objects.filter(is_active=True, is_active_admin=True)
        return super().formfield_for_foreignkey(db_field, request, **kwargs)

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            obj is None or (may_manage(request.user, obj.leave_request) and obj.leave_request.status == LeaveStatus.PENDING)
        )

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or (may_manage(request.user, obj.leave_request) and obj.leave_request.status == LeaveStatus.PENDING)
        )

    def save_model(self, request, obj, form, change):
        saved = services.assign_schedule_coverage(obj.leave_request, schedule_id=obj.schedule_id,
                                                  covering_admin=obj.covering_admin, actor=request.user)
        obj.__dict__.update(saved.__dict__)

    def delete_model(self, request, obj):
        services.remove_schedule_coverage(obj, request.user)

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        for coverage in queryset.order_by("pk"):
            services.remove_schedule_coverage(coverage, request.user)

    def get_actions(self, request):
        actions = super().get_actions(request)
        actions.pop("delete_selected", None)
        return actions

    @admin.action(description="Remove selected pending-leave coverage", permissions=["delete"])
    def remove_coverages(self, request, queryset):
        try:
            with transaction.atomic():
                count = 0
                for coverage in queryset.order_by("pk"):
                    self.log_deletions(request, [coverage])
                    services.remove_schedule_coverage(coverage, request.user)
                    count += 1
        except ValidationError as error:
            self.message_user(request, " ".join(error.messages), level=messages.ERROR)
        else:
            self.message_user(request, f"{count} coverage assignment(s) removed.", level=messages.SUCCESS)
