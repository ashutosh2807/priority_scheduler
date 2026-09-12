from django import forms
from django.contrib.auth.forms import UserCreationForm

from .models import AdminUser


class AdministratorForm(forms.ModelForm):
    class Meta:
        model = AdminUser
        fields = ("username", "employee_id", "display_name", "designation", "email", "role", "is_active_admin", "is_active")
        widgets = {
            "username": forms.TextInput(attrs={"class": "form-control"}), "employee_id": forms.TextInput(attrs={"class": "form-control"}),
            "display_name": forms.TextInput(attrs={"class": "form-control"}), "designation": forms.TextInput(attrs={"class": "form-control"}),
            "email": forms.EmailInput(attrs={"class": "form-control"}), "role": forms.Select(attrs={"class": "form-select"}),
            "is_active_admin": forms.CheckboxInput(attrs={"class": "form-check-input"}), "is_active": forms.CheckboxInput(attrs={"class": "form-check-input"}),
        }


class AdministratorCreateForm(UserCreationForm, AdministratorForm):
    class Meta(AdministratorForm.Meta):
        fields = AdministratorForm.Meta.fields + ("password1", "password2")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in ("password1", "password2"):
            self.fields[name].widget.attrs["class"] = "form-control"
