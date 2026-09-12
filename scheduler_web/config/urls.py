from django.contrib import admin
from django.urls import include, path
from .health import health


urlpatterns = [
    path("healthz/", health, name="health"),
    path("admin/", admin.site.urls),
    path("", include("apps.dashboard.urls")),
    path("accounts/", include("apps.accounts.urls")),
    path("tasks/", include("apps.tasks.urls")),
    path("leave/", include("apps.leave.urls")),
    path("scheduler/", include("apps.scheduler.urls")),
    path("audit/", include("apps.audit.urls")),
    path("sulog/", include("apps.sulog.urls")),
    path("eloadlog/", include("apps.eloadlog.urls")),
    path("mailer/", include("apps.mailer.urls")),
]
