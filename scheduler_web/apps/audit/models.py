from django.conf import settings
from django.db import models
from django.utils import timezone
import uuid


class AuditLog(models.Model):
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="audit_entries", null=True, blank=True)
    action = models.CharField(max_length=80)
    object_type = models.CharField(max_length=100)
    object_id = models.CharField(max_length=100)
    object_label = models.CharField(max_length=255)
    reason = models.TextField(blank=True)
    changes = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} · {self.object_label}"


class AuditDelivery(models.Model):
    """Durable portal-to-worker handoff; acceptance is not Oracle persistence."""
    audit = models.OneToOneField(AuditLog, on_delete=models.PROTECT, related_name="delivery")
    event_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    payload = models.JSONField()
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(default=timezone.now, db_index=True)
    accepted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.TextField(blank=True)
    lease_token = models.UUIDField(null=True, blank=True, editable=False)
    leased_until = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["audit_id"]
