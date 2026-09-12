from django.urls import path

from .views import AuditListView

app_name = "audit"
urlpatterns = [path("", AuditListView.as_view(), name="list")]
