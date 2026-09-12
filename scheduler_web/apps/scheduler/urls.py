from django.urls import path
from .schedule_management import ScheduleDefinitionView, ScheduleDeleteView, CalendarRefreshView

from .views import (
    JobActionView, JobDetailView, JobListView, QueueView,
    ScheduleEditView, ScheduleMasterConfigView, SchedulerDashboardView,
    SchedulerServiceActionView,
    UpcomingProcessesView,
    RunbookActionView, QueueReorderView, DailyTasksView,
)

app_name = "scheduler"
urlpatterns = [
    path("", SchedulerDashboardView.as_view(), name="dashboard"),
    path("service/action/<str:action>/", SchedulerServiceActionView.as_view(), name="service-action"),
    path("upcoming/", UpcomingProcessesView.as_view(), name="upcoming"),
    path("jobs/", JobListView.as_view(), name="jobs"),
    path("jobs/new/", ScheduleDefinitionView.as_view(), name="create"),
    path("jobs/<int:job_id>/definition/", ScheduleDefinitionView.as_view(), name="definition"),
    path("jobs/<int:job_id>/delete/", ScheduleDeleteView.as_view(), name="delete"),
    path("calendar/refresh/", CalendarRefreshView.as_view(), name="calendar-refresh"),
    path("jobs/<int:job_id>/", JobDetailView.as_view(), name="detail"),
    path("jobs/<int:job_id>/configuration/", ScheduleMasterConfigView.as_view(), name="master-configuration"),
    path("jobs/<int:job_id>/edit/", ScheduleEditView.as_view(), name="edit"),
    path("jobs/<int:job_id>/action/<str:action>/", JobActionView.as_view(), name="action"),
    path("queue/", QueueView.as_view(), name="queue"),
    path("queue/reorder/", QueueReorderView.as_view(), name="queue-reorder"),
    path("day/", DailyTasksView.as_view(), name="day"),
    path("jobs/<int:job_id>/work-log/", RunbookActionView.as_view(), name="work-log"),
    path("failed/", QueueView.as_view(), {"history": True, "forced_status": "FAILED"}, name="failed"),
    path("confirmation/", DailyTasksView.as_view(), {"confirmations": True}, name="confirmation"),
    path("history/", QueueView.as_view(), {"history": True}, name="history"),
]
