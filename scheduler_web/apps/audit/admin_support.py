"""Admin changes use the same durable audit path as the operations portal."""
from django.db import transaction

from .services import object_snapshot, record_action, record_external_action


class ReadOnlyAdminMixin:
    actions = None

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        return tuple(field.name for field in self.model._meta.fields)


class AuditedAdminMixin:
    """Log after related saves, inside Django's normal change-form transaction."""
    audit_related = ()

    def changelist_view(self, request, extra_context=None):
        # Bulk actions write Django's own LogEntry before delete_queryset.
        # Keep that log in the same transaction as deletion and portal delivery.
        if request.method == "POST":
            with transaction.atomic():
                return super().changelist_view(request, extra_context)
        return super().changelist_view(request, extra_context)

    def audit_snapshot(self, obj):
        snapshot = object_snapshot(obj)
        for field in obj._meta.many_to_many:
            snapshot[field.name] = list(getattr(obj, field.name).order_by("pk").values_list("pk", flat=True))
        for name in self.audit_related:
            snapshot[name] = [object_snapshot(item) for item in getattr(obj, name).order_by("pk")]
        return snapshot

    def remember_audit_before(self, request, obj):
        if not hasattr(request, "_admin_audit_before"):
            request._admin_audit_before = {}
        existing = self.model._default_manager.filter(pk=obj.pk).first() if obj.pk else None
        request._admin_audit_before[(self.model._meta.label, obj.pk)] = self.audit_snapshot(existing) if existing else None

    def save_model(self, request, obj, form, change):
        self.remember_audit_before(request, obj)
        super().save_model(request, obj, form, change)

    def _audit_saved(self, request, obj, operation):
        before = getattr(request, "_admin_audit_before", {}).get((self.model._meta.label, obj.pk))
        record_action(request.user, f"ADMIN_{self.model._meta.model_name.upper()}_{operation}", obj,
                      reason="Changed through Django administration.",
                      changes={"before": before, "after": self.audit_snapshot(obj), "interface": "django_admin"})

    def log_addition(self, request, obj, message):
        result = super().log_addition(request, obj, message)
        self._audit_saved(request, obj, "CREATED")
        return result

    def log_change(self, request, obj, message):
        result = super().log_change(request, obj, message)
        self._audit_saved(request, obj, "UPDATED")
        return result

    def _audit_deleted(self, request, identity, snapshot):
        record_external_action(request.user, f"ADMIN_{self.model._meta.model_name.upper()}_DELETED",
                               object_type=self.model._meta.label, object_id=identity[0], object_label=identity[1],
                               reason="Deleted through Django administration.",
                               changes={"before": snapshot, "after": None, "interface": "django_admin"})

    @transaction.atomic
    def delete_model(self, request, obj):
        identity, snapshot = (str(obj.pk), str(obj)), self.audit_snapshot(obj)
        super().delete_model(request, obj)
        self._audit_deleted(request, identity, snapshot)

    @transaction.atomic
    def delete_queryset(self, request, queryset):
        snapshots = [((str(obj.pk), str(obj)), self.audit_snapshot(obj)) for obj in queryset]
        super().delete_queryset(request, queryset)
        for identity, snapshot in snapshots:
            self._audit_deleted(request, identity, snapshot)
