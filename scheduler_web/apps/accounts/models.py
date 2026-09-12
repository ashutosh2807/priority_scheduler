from django.contrib.auth.models import AbstractUser
from django.db import models


class AdminRole(models.TextChoices):
    ADMIN = "ADMIN", "Admin"
    SUPERUSER = "SUPERUSER", "SUPERUSER"


class AdminUser(AbstractUser):
    """
    Application administrator.

    Authentication initially uses Django authentication.
    ADS authentication can be integrated later.
    """

    employee_id = models.CharField(
        max_length=30,
        unique=True,
    )

    display_name = models.CharField(
        max_length=150,
    )

    designation = models.CharField(
        max_length=100,
        blank=True,
    )

    role = models.CharField(
        max_length=20,
        choices=AdminRole.choices,
        default=AdminRole.ADMIN,
    )

    is_active_admin = models.BooleanField(
        default=True,
    )

    created_at = models.DateTimeField(
        auto_now_add=True,
    )

    updated_at = models.DateTimeField(
        auto_now=True,
    )

    def __str__(self):
        return f"{self.display_name} ({self.employee_id})"
