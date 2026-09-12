from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class LeaveStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    CANCELLED = "CANCELLED", "Cancelled"


class LeaveRequest(models.Model):
    """An administrator's availability request. Assignment records remain unchanged."""
    admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="leave_requests")
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=LeaveStatus.choices, default=LeaveStatus.PENDING)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approved_leave_requests", null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-start_date", "-created_at"]

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date cannot be before start date."})

    def __str__(self):
        return f"{self.admin} · {self.start_date:%d %b %Y} to {self.end_date:%d %b %Y}"


class LeaveScheduleCoverage(models.Model):
    """UI-owned cover for a confirmation-required scheduler schedule.

    schedule_id refers to the standalone Schedule Master entry. It is
    intentionally not a Django foreign key: Schedule Master remains owned by
    the scheduler project.
    """

    leave_request = models.ForeignKey(
        LeaveRequest,
        on_delete=models.CASCADE,
        related_name="schedule_coverages",
    )
    schedule_id = models.PositiveIntegerField()
    schedule_name = models.CharField(max_length=200)
    covering_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="leave_schedule_coverages",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_leave_schedule_coverages",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["schedule_name"]
        constraints = [
            models.UniqueConstraint(
                fields=["leave_request", "schedule_id"],
                name="unique_leave_schedule_coverage",
            )
        ]

    def clean(self):
        if (
            self.leave_request_id
            and self.covering_admin_id
            and self.leave_request.admin_id == self.covering_admin_id
        ):
            raise ValidationError(
                "The administrator on leave cannot cover their own schedule."
            )

    def __str__(self):
        return f"{self.schedule_name} covered by {self.covering_admin}"
