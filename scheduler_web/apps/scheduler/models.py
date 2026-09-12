from django.conf import settings
from django.db import models


class ScheduleProfile(models.Model):
    """UI-owned operator context for a standalone Schedule Master entry.

    The standalone scheduler does not consume these fields.  They are a
    human-facing description and runbook kept by the Django operations layer.
    """

    schedule_id = models.PositiveIntegerField(unique=True)
    primary_operator = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="primary_schedule_profiles",
        help_text="UI-owned operator responsible for confirmation-required occurrences.",
    )
    description = models.TextField(blank=True)
    backup_operator = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="backup_schedule_profiles",
        help_text="Preferred confirmation cover when the primary operator is unavailable.",
    )
    expected_minutes = models.PositiveIntegerField(null=True, blank=True)
    operational_steps = models.JSONField(default=list, blank=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="schedule_profiles_updated",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["schedule_id"]

    def __str__(self):
        return f"Schedule #{self.schedule_id} operator context"


class RunbookProgress(models.Model):
    """Human checks for one business report, separate from scheduler success."""
    schedule_id = models.PositiveIntegerField()
    report_date = models.DateField()
    step_index = models.PositiveIntegerField()
    step_text = models.TextField()
    completed = models.BooleanField(default=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(
            fields=["schedule_id", "report_date", "step_index"], name="unique_report_runbook_step"
        )]
