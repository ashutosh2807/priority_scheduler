from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin, UserAdmin
from django.contrib.auth.models import Group
from django.db import transaction

from apps.audit.admin_support import AuditedAdminMixin

from .models import AdminUser


admin.site.site_header = "ITRP Administration"
admin.site.site_title = "ITRP Admin"
admin.site.index_title = "Manage ITRP operations"


admin.site.unregister(Group)


@admin.register(Group)
class AuditedGroupAdmin(AuditedAdminMixin, GroupAdmin):
    """Keep Django's permission controls and audit group-wide access changes."""

    def audit_snapshot(self, obj):
        values = super().audit_snapshot(obj)
        values["member_ids"] = list(obj.user_set.order_by("pk").values_list("pk", flat=True))
        return values


@admin.register(AdminUser)
class AdminUserAdmin(AuditedAdminMixin, UserAdmin):

    @transaction.atomic
    def user_change_password(self, request, id, form_url=""):
        request._admin_credentials_change = True
        user = self.get_object(request, id)
        if user is not None:
            self.remember_audit_before(request, user)
        return super().user_change_password(request, id, form_url)

    def _audit_saved(self, request, obj, operation):
        if getattr(request, "_admin_credentials_change", False):
            operation = "CREDENTIALS_UPDATED"
        return super()._audit_saved(request, obj, operation)

    list_display = (
        "username",
        "employee_id",
        "display_name",
        "designation",
        "role",
        "is_active_admin",
        "is_staff",
        "is_active",
    )

    list_filter = (
        "role",
        "is_active_admin",
        "is_staff",
        "is_active",
    )

    search_fields = (
        "username",
        "employee_id",
        "display_name",
        "email",
    )

    fieldsets = UserAdmin.fieldsets + (
        (
            "Scheduler Administration",
            {
                "fields": (
                    "employee_id",
                    "display_name",
                    "designation",
                    "role",
                    "is_active_admin",
                )
            },
        ),
    )

    add_fieldsets = UserAdmin.add_fieldsets + (
        (
            "Scheduler Administration",
            {
                "fields": (
                    "employee_id",
                    "display_name",
                    "designation",
                    "role",
                    "is_active_admin",
                )
            },
        ),
    )
