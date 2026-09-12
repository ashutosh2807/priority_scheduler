from django.urls import path

from .views import AdministratorCreateView, AdministratorDetailView, AdministratorListView, AdministratorUpdateView, OperationsLoginView, OperationsLogoutView

app_name = "accounts"
urlpatterns = [
    path("login/", OperationsLoginView.as_view(), name="login"),
    path("logout/", OperationsLogoutView.as_view(), name="logout"),
    path("", AdministratorListView.as_view(), name="list"),
    path("create/", AdministratorCreateView.as_view(), name="create"),
    path("<int:pk>/", AdministratorDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", AdministratorUpdateView.as_view(), name="edit"),
]
