from django import forms
from django.contrib import admin
from django.urls import reverse
from django.utils.html import format_html

from apps.accounts.models import AdminUser
from apps.audit.admin_support import AuditedAdminMixin, ReadOnlyAdminMixin
from .file_adapter import get_scheduler_read_adapter
from .models import RunbookProgress, ScheduleProfile
from .services import is_manager, may_edit_profile


class ScheduleProfileAdminForm(forms.ModelForm):
    expected_minutes = forms.IntegerField(required=False, min_value=1, max_value=10080)

    class Meta:
        model = ScheduleProfile
        fields = ("primary_operator", "backup_operator", "description", "expected_minutes", "operational_steps")
        help_texts = {"operational_steps": "A JSON list of up to 20 non-empty operating guide steps."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("primary_operator", "backup_operator"):
            if name in self.fields:
                self.fields[name].queryset = AdminUser.objects.filter(is_active=True, is_active_admin=True)

    def clean_operational_steps(self):
        steps = self.cleaned_data.get("operational_steps")
        if steps in (None, ""):
            return []
        if not isinstance(steps, list) or len(steps) > 20 or any(not isinstance(step, str) or not step.strip() for step in steps):
            raise forms.ValidationError("Provide a list of up to 20 non-empty text steps.")
        return [step.strip() for step in steps]

    def clean(self):
        cleaned = super().clean()
        primary = cleaned.get("primary_operator", self.instance.primary_operator)
        backup = cleaned.get("backup_operator", self.instance.backup_operator)
        if primary and backup and primary == backup:
            raise forms.ValidationError("Preferred cover must differ from the primary confirmation operator.")
        adapter = get_scheduler_read_adapter()
        schedule = adapter.get_schedule(self.instance.schedule_id)
        if not schedule:
            raise forms.ValidationError("The schedule is unavailable. Open it in the operations portal before editing its context.")
        if schedule.get("confirmation_needed") and not primary:
            raise forms.ValidationError("Choose the primary operator who owns this confirmation gate.")
        return cleaned


@admin.register(ScheduleProfile)
class ScheduleProfileAdmin(AuditedAdminMixin, admin.ModelAdmin):
    form = ScheduleProfileAdminForm
    list_display = ("schedule_id", "primary_operator", "backup_operator", "expected_minutes", "updated_by", "updated_at", "operations_link")
    list_select_related = ("primary_operator", "backup_operator", "updated_by")
    search_fields = ("=schedule_id", "description", "primary_operator__employee_id", "primary_operator__display_name")
    readonly_fields = ("schedule_id", "updated_by", "updated_at", "operations_link")
    fields = ("schedule_id", "operations_link", "primary_operator", "backup_operator", "description",
              "expected_minutes", "operational_steps", "updated_by", "updated_at")

    @admin.display(description="Schedule settings and controls")
    def operations_link(self, obj):
        return format_html('<a href="{}">Open schedule in operations portal</a>', reverse("scheduler:detail", args=[obj.schedule_id]))

    def has_add_permission(self, request):
        return False  # Profiles originate with the worker-owned schedule through the operations UI.

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return super().has_change_permission(request, obj) and (
            is_manager(request.user) if obj is None else may_edit_profile(request.user, obj.schedule_id)
        )

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return fields if is_manager(request.user) else (*fields, "primary_operator", "backup_operator")

    def save_model(self, request, obj, form, change):
        obj.updated_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(RunbookProgress)
class RunbookProgressAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("schedule_id", "report_date", "step_index", "completed", "actor", "updated_at")
    list_filter = ("completed", "report_date")
    search_fields = ("=schedule_id", "step_text", "actor__username")
    list_select_related = ("actor",)
