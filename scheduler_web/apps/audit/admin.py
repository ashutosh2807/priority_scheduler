from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.contrib.sessions.models import Session
from django.utils import timezone

from apps.dashboard.templatetags.indian_dates import indian_datetime
from .admin_support import ReadOnlyAdminMixin
from .models import AuditDelivery, AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("created_at", "action", "actor", "object_type", "object_label")
    list_filter = ("action", "object_type", "created_at")
    search_fields = ("actor__username", "object_id", "object_label", "reason")
    list_select_related = ("actor",)
    date_hierarchy = "created_at"


@admin.register(AuditDelivery)
class AuditDeliveryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("event_id", "audit", "accepted_at", "attempts", "next_attempt_at")
    list_filter = ("accepted_at", "created_at")
    search_fields = ("event_id", "audit__action", "audit__object_label", "last_error")
    list_select_related = ("audit",)


@admin.register(LogEntry)
class DjangoLogEntryAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("action_time", "user", "action_flag", "content_type", "object_repr")
    list_filter = ("action_flag", "content_type__app_label", "action_time")
    search_fields = ("user__username", "object_repr", "change_message")
    list_select_related = ("user", "content_type")
    date_hierarchy = "action_time"


@admin.register(Permission)
class PermissionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("name", "codename", "content_type")
    list_filter = ("content_type__app_label",)
    search_fields = ("name", "codename", "content_type__app_label", "content_type__model")
    list_select_related = ("content_type",)
    ordering = ("content_type__app_label", "content_type__model", "codename")


@admin.register(ContentType)
class ContentTypeAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    list_display = ("app_label", "model")
    search_fields = ("app_label", "model")
    ordering = ("app_label", "model")


@admin.register(Session)
class SessionAdmin(ReadOnlyAdminMixin, admin.ModelAdmin):
    """Inspect expiry metadata without exposing bearer keys or session payloads."""
    list_display = ("expires_at", "expired")
    list_display_links = None
    list_filter = ("expire_date",)
    ordering = ("-expire_date",)
    show_full_result_count = False

    @admin.display(description="Expires at (IST)", ordering="expire_date")
    def expires_at(self, obj):
        return indian_datetime(obj.expire_date)

    @admin.display(boolean=True, description="Expired")
    def expired(self, obj):
        return obj.expire_date <= timezone.now()

    def has_view_permission(self, request, obj=None):
        # A session's primary key is itself a credential. Standard change and
        # history pages would include it in titles/URLs, so allow the list only.
        return obj is None and super().has_view_permission(request, obj)

    def get_queryset(self, request):
        return super().get_queryset(request).defer("session_data")
