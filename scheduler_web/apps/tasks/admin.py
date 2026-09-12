from django import forms
from django.contrib import admin
from django.forms.models import BaseInlineFormSet

from apps.audit.admin_support import AuditedAdminMixin
from .forms import TaskDelegationForm
from django.core.exceptions import ValidationError

from .models import Task, TaskAssignment, TaskDelegation


class AssignmentAdminForm(forms.ModelForm):
    validate_existing_primary = True

    class Meta:
        model = TaskAssignment
        fields = "__all__"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["admin"].queryset = self.fields["admin"].queryset.filter(is_active=True, is_active_admin=True)

    def clean(self):
        cleaned = super().clean()
        task = cleaned.get("task") or (self.instance.task if self.instance.task_id else None)
        if self.validate_existing_primary and task and cleaned.get("is_primary") and TaskAssignment.objects.filter(
            task=task, is_primary=True
        ).exclude(pk=self.instance.pk).exists():
            self.add_error("is_primary", "This task already has a primary administrator.")
        return cleaned


class InlineAssignmentForm(AssignmentAdminForm):
    validate_existing_primary = False


class AssignmentInlineFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        if any(self.errors):
            return
        represented = [form.instance.pk for form in self.forms if form.instance.pk]
        outside = TaskAssignment.objects.filter(task=self.instance, is_primary=True).exclude(pk__in=represented).count() if self.instance.pk else 0
        primary_count = outside + sum(bool(form.cleaned_data.get("is_primary")) for form in self.forms
                                      if form.cleaned_data and not form.cleaned_data.get("DELETE"))
        if primary_count > 1:
            raise ValidationError("Only one administrator can be the primary assignee for a task.")


class TaskAssignmentInline(admin.TabularInline):
    model = TaskAssignment
    form = InlineAssignmentForm
    formset = AssignmentInlineFormSet
    extra = 1
    fields = ("admin", "is_primary", "assigned_at")
    readonly_fields = ("assigned_at",)

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("admin", "task")


class TaskDelegationInline(admin.TabularInline):
    model = TaskDelegation
    form = TaskDelegationForm
    extra = 0
    fk_name = "task"
    fields = ("from_admin", "to_admin", "start_date", "end_date", "reason", "is_active", "created_by", "created_at")
    readonly_fields = ("created_by", "created_at")

    def get_queryset(self, request):
        return super().get_queryset(request).select_related("from_admin", "to_admin", "created_by", "task")


@admin.register(Task)
class TaskAdmin(AuditedAdminMixin, admin.ModelAdmin):
    audit_related = ("assignments", "delegations")

    def save_formset(self, request, form, formset, change):
        # ModelAdmin owns this hook; methods on InlineModelAdmin are not called.
        instances = formset.save(commit=False)
        for instance in formset.deleted_objects:
            instance.delete()
        for instance in instances:
            if isinstance(instance, TaskDelegation) and not instance.created_by_id:
                instance.created_by = request.user
            instance.full_clean()
            instance.save()
        formset.save_m2m()

    list_display = (
        "name",
        "owner",
        "status",
        "created_at",
        "updated_at",
    )

    list_filter = (
        "status",
    )

    search_fields = (
        "name",
        "description",
        "owner__employee_id",
        "owner__display_name",
    )

    ordering = (
        "name",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )

    autocomplete_fields = (
        "owner",
    )

    inlines = (
        TaskAssignmentInline,
        TaskDelegationInline,
    )


@admin.register(TaskAssignment)
class TaskAssignmentAdmin(AuditedAdminMixin, admin.ModelAdmin):
    form = AssignmentAdminForm
    list_display = (
        "task",
        "admin",
        "is_primary",
        "assigned_at",
    )

    list_filter = (
        "is_primary",
    )

    search_fields = (
        "task__name",
        "admin__employee_id",
        "admin__display_name",
    )

    ordering = (
        "-is_primary",
        "admin__display_name",
    )

    readonly_fields = (
        "assigned_at",
    )

    autocomplete_fields = (
        "task",
        "admin",
    )


@admin.register(TaskDelegation)
class TaskDelegationAdmin(AuditedAdminMixin, admin.ModelAdmin):
    form = TaskDelegationForm
    list_display = (
        "task",
        "from_admin",
        "to_admin",
        "start_date",
        "end_date",
        "is_active",
        "created_by",
        "created_at",
    )

    list_filter = (
        "is_active",
        "start_date",
        "end_date",
    )

    search_fields = (
        "task__name",
        "from_admin__employee_id",
        "from_admin__display_name",
        "to_admin__employee_id",
        "to_admin__display_name",
        "created_by__employee_id",
        "created_by__display_name",
        "reason",
    )

    ordering = (
        "-start_date",
        "-created_at",
    )

    readonly_fields = (
        "created_by",
        "created_at",
        "updated_at",
    )

    autocomplete_fields = (
        "task",
        "from_admin",
        "to_admin",
    )

    def save_model(self, request, obj, form, change):
        """
        Automatically record who created the delegation.
        """

        if not obj.created_by_id:
            obj.created_by = request.user

        obj.full_clean()
        super().save_model(request, obj, form, change)