from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import ValidationError
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views import View
from django.views.generic import CreateView, DetailView, ListView
from django.db import transaction

from apps.audit.services import record_action
from apps.scheduler.file_adapter import get_scheduler_read_adapter

from .forms import LeaveRequestForm, LeaveScheduleCoverageForm
from .models import LeaveRequest, LeaveScheduleCoverage, LeaveStatus
from .services import (
    approve,
    assign_schedule_coverage,
    available_covering_administrators,
    can_approve,
    cancel,
    confirmation_schedules_for_leave,
    coverage_is_complete,
    reject,
    remove_schedule_coverage,
    uncovered_confirmation_schedules,
)


class LeaveListView(LoginRequiredMixin, ListView):
    model = LeaveRequest
    template_name = "leave/leave_list.html"
    context_object_name = "leave_requests"
    paginate_by = 20

    def get_queryset(self):
        queryset = LeaveRequest.objects.select_related("admin", "approved_by")
        if not can_approve(self.request.user):
            queryset = queryset.filter(admin=self.request.user)
        status = self.request.GET.get("status")
        if status in LeaveStatus.values:
            queryset = queryset.filter(status=status)
        return queryset

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({"status_choices": LeaveStatus.choices, "selected_status": self.request.GET.get("status", ""), "can_approve": can_approve(self.request.user)})
        from apps.dashboard.views import calendar_context, _selected_month
        context.update(calendar_context(_selected_month(self.request.GET.get("month")), get_scheduler_read_adapter()))
        return context


class LeaveCreateView(LoginRequiredMixin, CreateView):
    model = LeaveRequest
    form_class = LeaveRequestForm
    template_name = "leave/leave_form.html"
    @transaction.atomic
    def form_valid(self, form):
        form.instance.admin = self.request.user
        self.object = form.save()
        record_action(self.request.user, "LEAVE_CREATED", self.object, reason=self.object.reason)
        messages.success(
            self.request,
            "Leave request created. Arrange cover for any confirmation-required schedules before approval.",
        )
        return redirect("leave:detail", pk=self.object.pk)


class LeaveDetailView(LoginRequiredMixin, DetailView):
    model = LeaveRequest
    template_name = "leave/leave_detail.html"
    context_object_name = "leave_request"

    def get_queryset(self):
        queryset = LeaveRequest.objects.select_related("admin", "approved_by").prefetch_related(
            "schedule_coverages__covering_admin",
        )
        return queryset if can_approve(self.request.user) else queryset.filter(admin=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        leave_request = self.object
        may_manage_coverage = (
            can_approve(self.request.user)
            or leave_request.admin_id == self.request.user.id
        )
        available_operators = available_covering_administrators(leave_request)
        coverage_by_schedule = {
            coverage.schedule_id: coverage
            for coverage in leave_request.schedule_coverages.all()
        }
        confirmation_schedules = confirmation_schedules_for_leave(leave_request)
        for schedule in confirmation_schedules:
            schedule["coverage"] = coverage_by_schedule.get(schedule["id"])
            schedule["coverage_form"] = LeaveScheduleCoverageForm(
                initial={
                    "schedule_id": schedule["id"],
                    "covering_admin": (
                        schedule["coverage"].covering_admin_id
                        if schedule["coverage"]
                        else None
                    ),
                },
                available_operators=available_operators,
            )
        context.update({
            "can_approve": can_approve(self.request.user),
            "can_manage_coverage": may_manage_coverage,
            "available_operators": available_operators,
            "confirmation_schedules": confirmation_schedules,
            "uncovered_confirmation_schedules": uncovered_confirmation_schedules(leave_request),
            "coverage_complete": coverage_is_complete(leave_request),
        })
        return context


class LeaveActionView(LoginRequiredMixin, View):
    def post(self, request, pk, action):
        leave_request = get_object_or_404(LeaveRequest, pk=pk)
        if action in {"approve", "reject"}:
            if not can_approve(request.user):
                return HttpResponseForbidden("You are not allowed to approve leave requests.")
            if leave_request.status != LeaveStatus.PENDING:
                messages.info(request, "This leave request has already been actioned.")
            elif action == "approve":
                try:
                    approve(leave_request, request.user)
                except ValidationError as error:
                    messages.error(request, error.messages[0])
                else:
                    messages.success(request, "Leave request approved.")
            else:
                try:
                    reject(leave_request, request.user)
                except ValidationError as error:
                    messages.error(request, error.messages[0])
                else:
                    messages.success(request, "Leave request rejected.")
        elif action == "cancel":
            if leave_request.admin_id != request.user.id and not can_approve(request.user):
                return HttpResponseForbidden("You are not allowed to cancel this leave request.")
            if leave_request.status in {LeaveStatus.PENDING, LeaveStatus.APPROVED}:
                try:
                    cancel(leave_request, request.user)
                except ValidationError as error:
                    messages.error(request, error.messages[0])
                else:
                    messages.success(request, "Leave request cancelled.")
            else:
                messages.info(request, "This leave request cannot be cancelled.")
        else:
            return HttpResponseForbidden("Unsupported leave action.")
        return redirect("leave:detail", pk=leave_request.pk)


class LeaveScheduleCoverageAssignView(LoginRequiredMixin, View):
    def post(self, request, pk):
        leave_request = get_object_or_404(
            LeaveRequest.objects.select_related("admin"),
            pk=pk,
        )
        if (
            leave_request.admin_id != request.user.id
            and not can_approve(request.user)
        ):
            return HttpResponseForbidden("You are not allowed to arrange this leave coverage.")
        form = LeaveScheduleCoverageForm(
            request.POST,
            available_operators=available_covering_administrators(leave_request),
        )
        if not form.is_valid():
            messages.error(request, "Choose an available operator before assigning coverage.")
            return redirect("leave:detail", pk=leave_request.pk)
        try:
            coverage = assign_schedule_coverage(
                leave_request,
                schedule_id=form.cleaned_data["schedule_id"],
                covering_admin=form.cleaned_data["covering_admin"],
                actor=request.user,
            )
        except ValidationError as error:
            messages.error(request, error.messages[0])
        else:
            messages.success(
                request,
                f"{coverage.covering_admin.display_name} will cover {coverage.schedule_name}.",
            )
        return redirect("leave:detail", pk=leave_request.pk)


class LeaveScheduleCoverageRemoveView(LoginRequiredMixin, View):
    def post(self, request, pk, coverage_id):
        leave_request = get_object_or_404(LeaveRequest, pk=pk)
        if (
            leave_request.admin_id != request.user.id
            and not can_approve(request.user)
        ):
            return HttpResponseForbidden("You are not allowed to change this leave coverage.")
        coverage = get_object_or_404(
            LeaveScheduleCoverage,
            pk=coverage_id,
            leave_request=leave_request,
        )
        if leave_request.status != LeaveStatus.PENDING:
            messages.error(request, "Coverage cannot be changed after leave has been actioned.")
        else:
            try:
                remove_schedule_coverage(coverage, request.user)
            except ValidationError as error:
                messages.error(request, error.messages[0])
            else:
                messages.success(request, f"Coverage for {coverage.schedule_name} was removed.")
        return redirect("leave:detail", pk=leave_request.pk)
