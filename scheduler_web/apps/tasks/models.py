from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class TaskStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "Active"
    INACTIVE = "INACTIVE", "Inactive"


class Task(models.Model):
    """
    Represents a business/scheduler task managed by the Django
    operations application.

    Scheduler-specific configuration such as RUN_CONFIG, MARGIN,
    SAME_DAY, TIME_FLAG, etc. is intentionally NOT stored here.
    Those remain owned by the scheduler/Oracle side.
    """

    name = models.CharField(
        max_length=200,
        unique=True,
    )

    description = models.TextField(
        blank=True,
    )

    status = models.CharField(
        max_length=20,
        choices=TaskStatus.choices,
        default=TaskStatus.ACTIVE,
    )

    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="owned_tasks",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class TaskAssignment(models.Model):
    """
    Represents the normal assignment of administrators to a task.
    """

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="assignments",
    )

    admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="task_assignments",
    )

    is_primary = models.BooleanField(
        default=False,
    )

    assigned_at = models.DateTimeField(
        auto_now_add=True,
    )

    class Meta:
        ordering = ["-is_primary", "admin__display_name"]

        constraints = [
            models.UniqueConstraint(
                fields=["task", "admin"],
                name="unique_task_admin_assignment",
            )
        ]

    def __str__(self):
        return f"{self.task} -> {self.admin}"


class TaskDelegation(models.Model):
    """
    Represents a temporary delegation of responsibility for a task.

    Example:
        Task A
            From: Admin A
            To:   Admin B
            Start: 2026-10-01
            End:   2026-10-15
    """

    task = models.ForeignKey(
        Task,
        on_delete=models.CASCADE,
        related_name="delegations",
    )

    from_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="delegations_given",
    )

    to_admin = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="delegations_received",
    )

    start_date = models.DateField()

    end_date = models.DateField()

    reason = models.TextField(
        blank=True,
    )

    is_active = models.BooleanField(
        default=True,
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="created_task_delegations",
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    class Meta:
        ordering = [
            "-start_date",
            "-created_at",
        ]

    def clean(self):
        """
        Validate delegation before saving.
        """

        if (
            self.from_admin_id
            and self.to_admin_id
            and self.from_admin_id == self.to_admin_id
        ):
            raise ValidationError(
                "A task cannot be delegated to the same administrator."
            )

        if (
            self.start_date
            and self.end_date
            and self.end_date < self.start_date
        ):
            raise ValidationError(
                "End date cannot be before start date."
            )

    def __str__(self):
        return (
            f"{self.task} | "
            f"{self.from_admin} -> {self.to_admin}"
        )