import uuid
import django.db.models.deletion
from django.db import migrations, models
from django.utils import timezone


class Migration(migrations.Migration):
    dependencies = [("audit", "0002_auditlog_changes")]
    operations = [migrations.CreateModel(
        name="AuditDelivery",
        fields=[
            ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
            ("event_id", models.UUIDField(default=uuid.uuid4, editable=False, unique=True)),
            ("payload", models.JSONField()),
            ("attempts", models.PositiveIntegerField(default=0)),
            ("next_attempt_at", models.DateTimeField(db_index=True, default=timezone.now)),
            ("accepted_at", models.DateTimeField(blank=True, db_index=True, null=True)),
            ("last_error", models.TextField(blank=True)),
            ("lease_token", models.UUIDField(blank=True, editable=False, null=True)),
            ("leased_until", models.DateTimeField(blank=True, null=True)),
            ("created_at", models.DateTimeField(auto_now_add=True)),
            ("audit", models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name="delivery", to="audit.auditlog")),
        ], options={"ordering": ["audit_id"]},
    )]
