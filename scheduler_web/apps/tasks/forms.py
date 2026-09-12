from django import forms

from .models import Task, TaskAssignment, TaskDelegation


class TaskForm(forms.ModelForm):
    """
    Form used to create and update tasks.
    """

    class Meta:
        model = Task
        fields = (
            "name",
            "description",
            "status",
            "owner",
        )

        widgets = {
            "name": forms.TextInput(
                attrs={
                    "class": "form-control",
                    "placeholder": "Enter task name",
                }
            ),
            "description": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Enter task description",
                }
            ),
            "status": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "owner": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
        }


class TaskAssignmentForm(forms.ModelForm):
    """
    Form used to assign an administrator to a task.
    """

    class Meta:
        model = TaskAssignment
        fields = (
            "admin",
            "is_primary",
        )

        widgets = {
            "admin": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "is_primary": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
        }

    def __init__(self, *args, task=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.task = task

        if task is not None:
            self.fields["admin"].queryset = (
                self.fields["admin"]
                .queryset
                .filter(is_active=True, is_active_admin=True)
                .order_by("display_name")
            )

    def clean(self):
        cleaned_data = super().clean()

        admin = cleaned_data.get("admin")
        is_primary = cleaned_data.get("is_primary")

        if (
            self.task is not None
            and admin is not None
            and is_primary
        ):
            existing_primary = (
                TaskAssignment.objects
                .filter(
                    task=self.task,
                    is_primary=True,
                )
                .exclude(pk=self.instance.pk)
                .exists()
            )

            if existing_primary:
                self.add_error(
                    "is_primary",
                    "This task already has a primary administrator.",
                )

        return cleaned_data


class TaskDelegationForm(forms.ModelForm):
    """
    Form used to create a temporary task delegation.
    """

    class Meta:
        model = TaskDelegation

        fields = (
            "task",
            "from_admin",
            "to_admin",
            "start_date",
            "end_date",
            "reason",
            "is_active",
        )

        widgets = {
            "task": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "from_admin": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "to_admin": forms.Select(
                attrs={
                    "class": "form-select",
                }
            ),
            "start_date": forms.DateInput(
                attrs={
                    "class": "form-control",
                    "type": "date",
                }
            ),
            "end_date": forms.DateInput(
                attrs={
                    "class": "form-control",
                    "type": "date",
                }
            ),
            "reason": forms.Textarea(
                attrs={
                    "class": "form-control",
                    "rows": 4,
                    "placeholder": "Enter reason for delegation",
                }
            ),
            "is_active": forms.CheckboxInput(
                attrs={
                    "class": "form-check-input",
                }
            ),
        }

    def __init__(self, *args, task=None, **kwargs):
        super().__init__(*args, **kwargs)

        self.task = task

        active_admins = (
            self.fields["from_admin"]
            .queryset
            .filter(
                is_active=True,
                is_active_admin=True,
            )
            .order_by("display_name")
        )

        self.fields["from_admin"].queryset = active_admins
        self.fields["to_admin"].queryset = active_admins

        if task is not None:
            self.fields["task"].queryset = (
                self.fields["task"]
                .queryset
                .filter(pk=task.pk)
            )

    def clean(self):
        cleaned_data = super().clean()

        from_admin = cleaned_data.get("from_admin")
        to_admin = cleaned_data.get("to_admin")
        start_date = cleaned_data.get("start_date")
        end_date = cleaned_data.get("end_date")

        if from_admin and to_admin:

            if from_admin == to_admin:
                self.add_error(
                    "to_admin",
                    "A task cannot be delegated to the same administrator.",
                )

        if start_date and end_date:

            if end_date < start_date:
                self.add_error(
                    "end_date",
                    "End date cannot be before start date.",
                )

        return cleaned_data