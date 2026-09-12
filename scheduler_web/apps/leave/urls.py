from django.urls import path

from .views import (
    LeaveActionView,
    LeaveCreateView,
    LeaveDetailView,
    LeaveListView,
    LeaveScheduleCoverageAssignView,
    LeaveScheduleCoverageRemoveView,
)

app_name = "leave"

urlpatterns = [
    path("", LeaveListView.as_view(), name="list"),
    path("request/", LeaveCreateView.as_view(), name="create"),
    path("<int:pk>/", LeaveDetailView.as_view(), name="detail"),
    path("<int:pk>/coverage/", LeaveScheduleCoverageAssignView.as_view(), name="coverage-assign"),
    path("<int:pk>/coverage/<int:coverage_id>/remove/", LeaveScheduleCoverageRemoveView.as_view(), name="coverage-remove"),
    path("<int:pk>/<str:action>/", LeaveActionView.as_view(), name="action"),
]
