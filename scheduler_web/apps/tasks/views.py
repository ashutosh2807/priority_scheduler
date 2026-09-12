from datetime import date

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, reverse_lazy
from django.views import View
from django.views.generic import (
    CreateView,
    DeleteView,
    DetailView,
    ListView,
    UpdateView,
)

from apps.audit.services import record_action

from .forms import (
    TaskAssignmentForm,
    TaskDelegationForm,
    TaskForm,
)
from .models import (
    Task,
    TaskAssignment,
    TaskDelegation,
)
from .services import TaskResponsibilityService


# =============================================================
# TASK VIEWS
# =============================================================


class TaskListView(LoginRequiredMixin, ListView):
    """
    Display all tasks.
    """

    model = Task
    template_name = "tasks/task_list.html"
    context_object_name = "tasks"
    paginate_by = 20

    def get_queryset(self):
        queryset = (
            Task.objects
            .select_related("owner")
            .prefetch_related(
                "assignments__admin",
                "delegations__from_admin",
                "delegations__to_admin",
            )
            .order_by("name")
        )
        query = self.request.GET.get("q", "").strip()
        status = self.request.GET.get("status", "")
        if query:
            from django.db.models import Q
            queryset = queryset.filter(
                Q(name__icontains=query)
                | Q(owner__display_name__icontains=query)
                | Q(assignments__admin__display_name__icontains=query)
            ).distinct()
        if status in {"ACTIVE", "INACTIVE"}:
            queryset = queryset.filter(status=status)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["selected_status"] = self.request.GET.get("status", "")
        context["search"] = self.request.GET.get("q", "")
        return context


class TaskDetailView(LoginRequiredMixin, DetailView):
    """
    Display complete details of a task.
    """

    model = Task
    template_name = "tasks/task_detail.html"
    context_object_name = "task"

    def get_queryset(self):
        return (
            Task.objects
            .select_related("owner")
            .prefetch_related(
                "assignments__admin",
                "delegations__from_admin",
                "delegations__to_admin",
                "delegations__created_by",
            )
        )

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        try:
            context["responsibility"] = TaskResponsibilityService.get_responsibility_details(self.object, date.today())
        except Exception:
            context["responsibility"] = None
        return context


class TaskCreateView(LoginRequiredMixin, CreateView):
    """
    Create a new task.
    """

    model = Task
    form_class = TaskForm
    template_name = "tasks/task_form.html"

    def get_success_url(self):
        return reverse(
            "tasks:detail",
            kwargs={"pk": self.object.pk},
        )

    @transaction.atomic
    def form_valid(self, form):
        response = super().form_valid(form)
        record_action(self.request.user, "TASK_CREATED", self.object)

        messages.success(
            self.request,
            f'Task "{self.object.name}" was created successfully.',
        )

        return response


class TaskUpdateView(LoginRequiredMixin, UpdateView):
    """
    Update an existing task.
    """

    model = Task
    form_class = TaskForm
    template_name = "tasks/task_form.html"

    def get_success_url(self):
        return reverse(
            "tasks:detail",
            kwargs={"pk": self.object.pk},
        )

    @transaction.atomic
    def form_valid(self, form):
        response = super().form_valid(form)
        record_action(self.request.user, "TASK_UPDATED", self.object)

        messages.success(
            self.request,
            f'Task "{self.object.name}" was updated successfully.',
        )

        return response


class TaskDeleteView(LoginRequiredMixin, DeleteView):
    """
    Delete an existing task.
    """

    model = Task
    template_name = "tasks/task_confirm_delete.html"
    success_url = reverse_lazy("tasks:list")

    @transaction.atomic
    def form_valid(self, form):
        task_name = self.object.name
        record_action(self.request.user, "TASK_DELETED", self.object)

        response = super().form_valid(form)

        messages.success(
            self.request,
            f'Task "{task_name}" was deleted successfully.',
        )

        return response


# =============================================================
# TASK ASSIGNMENT VIEWS
# =============================================================


class TaskAssignmentCreateView(LoginRequiredMixin, View):
    """
    Display and process the assignment form for a task.
    """

    template_name = "tasks/task_assignment_form.html"

    def get_task(self, pk):
        return get_object_or_404(
            Task,
            pk=pk,
        )

    def get(self, request, pk):
        task = self.get_task(pk)

        form = TaskAssignmentForm(
            task=task,
        )

        return render(
            request,
            self.template_name,
            {
                "task": task,
                "form": form,
            },
        )

    @transaction.atomic
    def post(self, request, pk):
        task = self.get_task(pk)

        form = TaskAssignmentForm(
            request.POST,
            task=task,
        )

        if not form.is_valid():

            return render(
                request,
                self.template_name,
                {
                    "task": task,
                    "form": form,
                },
            )

        assignment = form.save(
            commit=False,
        )

        assignment.task = task

        try:

            with transaction.atomic():

                if assignment.is_primary:

                    TaskAssignment.objects.filter(
                        task=task,
                        is_primary=True,
                    ).update(
                        is_primary=False,
                    )

                assignment.save()

        except Exception as exc:

            form.add_error(
                None,
                f"Unable to assign administrator: {exc}",
            )

            return render(
                request,
                self.template_name,
                {
                    "task": task,
                    "form": form,
                },
            )

        record_action(
            request.user,
            "ADMIN_ASSIGNED",
            assignment,
            reason=f"Assigned to {task.name}",
        )
        messages.success(
            request,
            f"{assignment.admin.display_name} was assigned to the task.",
        )

        return redirect(
            "tasks:detail",
            pk=task.pk,
        )


class TaskAssignmentDeleteView(LoginRequiredMixin, View):
    """
    Remove an administrator assignment from a task.
    """

    @transaction.atomic
    def post(self, request, pk, assignment_id):

        task = get_object_or_404(
            Task,
            pk=pk,
        )

        assignment = get_object_or_404(
            TaskAssignment,
            pk=assignment_id,
            task=task,
        )

        admin_name = assignment.admin.display_name

        record_action(request.user, "ADMIN_REMOVED", assignment, reason=f"Removed from {task.name}")
        assignment.delete()

        messages.success(
            request,
            f"{admin_name} was removed from the task.",
        )

        return redirect(
            "tasks:detail",
            pk=task.pk,
        )


class TaskAssignmentPrimaryView(LoginRequiredMixin, View):
    """
    Make an existing assignment the primary assignment.
    """

    @transaction.atomic
    def post(self, request, pk, assignment_id):

        task = get_object_or_404(
            Task,
            pk=pk,
        )

        assignment = get_object_or_404(
            TaskAssignment,
            pk=assignment_id,
            task=task,
        )

        with transaction.atomic():

            TaskAssignment.objects.filter(
                task=task,
                is_primary=True,
            ).update(
                is_primary=False,
            )

            assignment.is_primary = True

            assignment.save(
                update_fields=[
                    "is_primary",
                ],
            )

        record_action(
            request.user,
            "PRIMARY_CHANGED",
            assignment,
            reason=f"Primary for {task.name}",
        )
        messages.success(
            request,
            f"{assignment.admin.display_name} is now the primary administrator.",
        )

        return redirect(
            "tasks:detail",
            pk=task.pk,
        )


# =============================================================
# TASK DELEGATION VIEWS
# =============================================================


class TaskDelegationCreateView(LoginRequiredMixin, View):
    """
    Display and process the delegation form for a task.
    """

    template_name = "tasks/task_delegation_form.html"

    def get_task(self, pk):
        return get_object_or_404(
            Task,
            pk=pk,
        )

    def get(self, request, pk):

        task = self.get_task(pk)

        form = TaskDelegationForm(
            task=task,
        )

        return render(
            request,
            self.template_name,
            {
                "task": task,
                "form": form,
            },
        )

    @transaction.atomic
    def post(self, request, pk):

        task = self.get_task(pk)

        form = TaskDelegationForm(
            request.POST,
            task=task,
        )

        if not form.is_valid():

            return render(
                request,
                self.template_name,
                {
                    "task": task,
                    "form": form,
                },
            )

        delegation = form.save(
            commit=False,
        )

        delegation.task = task
        delegation.created_by = request.user

        try:

            delegation.full_clean()
            delegation.save()

        except Exception as exc:

            form.add_error(
                None,
                f"Unable to create delegation: {exc}",
            )

            return render(
                request,
                self.template_name,
                {
                    "task": task,
                    "form": form,
                },
            )

        record_action(
            request.user,
            "DELEGATION_CREATED",
            delegation,
            reason=delegation.reason,
        )
        messages.success(
            request,
            "Task delegation created successfully.",
        )

        return redirect(
            "tasks:detail",
            pk=task.pk,
        )


class TaskDelegationDeactivateView(LoginRequiredMixin, View):
    """
    Deactivate an existing delegation.

    The delegation record is retained for history.
    """

    @transaction.atomic
    def post(self, request, pk, delegation_id):

        task = get_object_or_404(
            Task,
            pk=pk,
        )

        delegation = get_object_or_404(
            TaskDelegation,
            pk=delegation_id,
            task=task,
        )

        if not delegation.is_active:

            messages.info(
                request,
                "This delegation is already inactive.",
            )

            return redirect(
                "tasks:detail",
                pk=task.pk,
            )

        delegation.is_active = False

        delegation.save(
            update_fields=[
                "is_active",
                "updated_at",
            ],
        )

        record_action(request.user, "DELEGATION_DEACTIVATED", delegation, reason=delegation.reason)

        messages.success(
            request,
            "Task delegation has been deactivated.",
        )

        return redirect(
            "tasks:detail",
            pk=task.pk,
        )
