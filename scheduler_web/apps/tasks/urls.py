from django.urls import path

from .views import (
    TaskAssignmentCreateView,
    TaskAssignmentDeleteView,
    TaskAssignmentPrimaryView,
    TaskCreateView,
    TaskDeleteView,
    TaskDelegationCreateView,
    TaskDelegationDeactivateView,
    TaskDetailView,
    TaskListView,
    TaskUpdateView,
)


app_name = "tasks"


urlpatterns = [
    # =========================================================
    # TASK
    # =========================================================

    path(
        "",
        TaskListView.as_view(),
        name="list",
    ),

    path(
        "create/",
        TaskCreateView.as_view(),
        name="create",
    ),

    path(
        "<int:pk>/",
        TaskDetailView.as_view(),
        name="detail",
    ),

    path(
        "<int:pk>/edit/",
        TaskUpdateView.as_view(),
        name="edit",
    ),

    path(
        "<int:pk>/delete/",
        TaskDeleteView.as_view(),
        name="delete",
    ),


    # =========================================================
    # TASK ASSIGNMENT
    # =========================================================

    path(
        "<int:pk>/assign/",
        TaskAssignmentCreateView.as_view(),
        name="assignment-create",
    ),

    path(
        "<int:pk>/assignment/<int:assignment_id>/delete/",
        TaskAssignmentDeleteView.as_view(),
        name="assignment-delete",
    ),

    path(
        "<int:pk>/assignment/<int:assignment_id>/primary/",
        TaskAssignmentPrimaryView.as_view(),
        name="assignment-primary",
    ),


    # =========================================================
    # TASK DELEGATION
    # =========================================================

    path(
        "<int:pk>/delegation/",
        TaskDelegationCreateView.as_view(),
        name="delegation-create",
    ),

    path(
        "<int:pk>/delegation/<int:delegation_id>/deactivate/",
        TaskDelegationDeactivateView.as_view(),
        name="delegation-deactivate",
    ),
]