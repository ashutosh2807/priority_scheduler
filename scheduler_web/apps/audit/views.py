from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import ListView

from .models import AuditLog


class AuditListView(LoginRequiredMixin, ListView):
    model = AuditLog
    template_name = "audit/audit_list.html"
    context_object_name = "entries"
    paginate_by = 30

    def get_queryset(self):
        queryset = AuditLog.objects.select_related("actor", "delivery")
        action = self.request.GET.get("action", "").strip()
        if action:
            queryset = queryset.filter(action__icontains=action)
        return queryset

    def get_context_data(self, **kwargs):
        from apps.scheduler.file_adapter import get_scheduler_read_adapter
        context = super().get_context_data(**kwargs)
        adapter = get_scheduler_read_adapter()
        context.update(scheduler=adapter.get_status(), scheduler_adapter=adapter)
        return context
