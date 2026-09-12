from datetime import date

from django.core.exceptions import ValidationError

from .models import Task, TaskAssignment, TaskDelegation


class TaskResponsibilityService:
    """
    Determines who is effectively responsible for a task
    on a particular date.

    Responsibility hierarchy:

        1. Active delegation covering the date
        2. Primary task assignment
        3. Task owner

    The underlying assignment/owner is not changed when a
    delegation is active.
    """

    @staticmethod
    def get_active_delegation(
        task: Task,
        on_date: date,
    ) -> TaskDelegation | None:
        """
        Return the active delegation applicable to a task
        on the supplied date.

        A delegation is applicable when:

            start_date <= on_date <= end_date
        """

        delegations = (
            TaskDelegation.objects
            .select_related(
                "from_admin",
                "to_admin",
            )
            .filter(
                task=task,
                is_active=True,
                start_date__lte=on_date,
                end_date__gte=on_date,
            )
            .order_by(
                "-start_date",
                "-created_at",
            )
        )

        delegation_list = list(delegations)

        if len(delegation_list) > 1:
            raise ValidationError(
                f"Multiple active delegations exist for "
                f"task '{task.name}' on {on_date}."
            )

        return delegation_list[0] if delegation_list else None

    @staticmethod
    def get_primary_assignment(
        task: Task,
    ) -> TaskAssignment | None:
        """
        Return the primary administrator assigned to the task.
        """

        return (
            TaskAssignment.objects
            .select_related("admin")
            .filter(
                task=task,
                is_primary=True,
            )
            .first()
        )

    @classmethod
    def get_responsible_admin(
        cls,
        task: Task,
        on_date: date | None = None,
    ):
        """
        Determine the effective responsible administrator.

        Priority:

            Active delegation
                ↓
            Primary assignment
                ↓
            Task owner

        If no date is supplied, today's date is used.
        """

        if on_date is None:
            on_date = date.today()

        # -----------------------------------------------------
        # 1. Check active delegation
        # -----------------------------------------------------

        delegation = cls.get_active_delegation(
            task=task,
            on_date=on_date,
        )

        if delegation is not None:
            return delegation.to_admin

        # -----------------------------------------------------
        # 2. Check primary assignment
        # -----------------------------------------------------

        primary_assignment = cls.get_primary_assignment(
            task=task,
        )

        if primary_assignment is not None:
            return primary_assignment.admin

        # -----------------------------------------------------
        # 3. Fall back to task owner
        # -----------------------------------------------------

        return task.owner

    @classmethod
    def get_responsibility_details(
        cls,
        task: Task,
        on_date: date | None = None,
    ):
        """
        Return detailed information about the effective
        responsibility.

        Example result:

            {
                "admin": <AdminUser>,
                "source": "DELEGATION",
                "delegation": <TaskDelegation>,
                "date": date(...),
            }
        """

        if on_date is None:
            on_date = date.today()

        # -----------------------------------------------------
        # Delegation
        # -----------------------------------------------------

        delegation = cls.get_active_delegation(
            task=task,
            on_date=on_date,
        )

        if delegation is not None:

            return {
                "admin": delegation.to_admin,
                "source": "DELEGATION",
                "delegation": delegation,
                "date": on_date,
            }

        # -----------------------------------------------------
        # Primary assignment
        # -----------------------------------------------------

        primary_assignment = cls.get_primary_assignment(
            task=task,
        )

        if primary_assignment is not None:

            return {
                "admin": primary_assignment.admin,
                "source": "PRIMARY_ASSIGNMENT",
                "delegation": None,
                "date": on_date,
            }

        # -----------------------------------------------------
        # Owner
        # -----------------------------------------------------

        return {
            "admin": task.owner,
            "source": "OWNER",
            "delegation": None,
            "date": on_date,
        }

    @classmethod
    def is_responsible_admin(
        cls,
        task: Task,
        admin,
        on_date: date | None = None,
    ) -> bool:
        """
        Check whether the supplied administrator is the
        effective responsible administrator for the task
        on the supplied date.
        """

        responsible_admin = cls.get_responsible_admin(
            task=task,
            on_date=on_date,
        )

        return responsible_admin.pk == admin.pk