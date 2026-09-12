from django.urls import path

from .views import CalendarView, DashboardView, HelpView

app_name = "dashboard"

urlpatterns = [
    path("", DashboardView.as_view(), name="home"),
    path("calendar/", CalendarView.as_view(), name="calendar"),
    path("help/", HelpView.as_view(), name="help"),
]
