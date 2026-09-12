from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import PermissionDenied
from django.urls import reverse_lazy
from django.views.generic import CreateView, DetailView, ListView, UpdateView
from django.db import transaction
from apps.audit.services import record_action

from .forms import AdministratorCreateForm, AdministratorForm
from .models import AdminRole, AdminUser


class OperationsLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True


class OperationsLogoutView(LogoutView):
    next_page = reverse_lazy("accounts:login")


class AdministratorAccessMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return self.handle_no_permission()
        if not (request.user.is_superuser or request.user.role == AdminRole.SUPERUSER):
            raise PermissionDenied("Administrator management is restricted to SUPERUSER users.")
        return super().dispatch(request, *args, **kwargs)


class AdministratorListView(AdministratorAccessMixin, ListView):
    model = AdminUser
    template_name = "accounts/user_list.html"
    context_object_name = "administrators"
    paginate_by = 25

    def get_queryset(self):
        queryset = AdminUser.objects.order_by("display_name")
        query = self.request.GET.get("q", "").strip()
        if query:
            from django.db.models import Q
            queryset = queryset.filter(Q(display_name__icontains=query) | Q(employee_id__icontains=query) | Q(username__icontains=query))
        return queryset


class AdministratorDetailView(LoginRequiredMixin, DetailView):
    model = AdminUser
    template_name = "accounts/user_detail.html"
    context_object_name = "administrator"

    def get_queryset(self):
        queryset = AdminUser.objects.all()
        if self.request.user.is_superuser or self.request.user.role == AdminRole.SUPERUSER:
            return queryset
        return queryset.filter(pk=self.request.user.pk)


class AdministratorAuditMixin:
    @transaction.atomic
    def form_valid(self, form):
        # Explicit allowlist excludes password, hash, auth tokens and form secrets.
        fields = AdministratorForm.Meta.fields
        before = AdminUser.objects.filter(pk=form.instance.pk).values(*fields).first() if form.instance.pk else None
        response = super().form_valid(form)
        after = {field: getattr(self.object, field) for field in fields}
        record_action(self.request.user, "ADMINISTRATOR_CREATED" if before is None else "ADMINISTRATOR_UPDATED",
                      self.object, changes={"before": before, "after": after})
        return response


class AdministratorCreateView(AdministratorAccessMixin, AdministratorAuditMixin, CreateView):
    model = AdminUser
    form_class = AdministratorCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("accounts:list")


class AdministratorUpdateView(AdministratorAccessMixin, AdministratorAuditMixin, UpdateView):
    model = AdminUser
    form_class = AdministratorForm
    template_name = "accounts/user_form.html"

    def get_success_url(self):
        return reverse_lazy("accounts:detail", kwargs={"pk": self.object.pk})
