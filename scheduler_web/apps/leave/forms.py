from django import forms

from apps.accounts.models import AdminUser

from .models import LeaveRequest


class LeaveRequestForm(forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = ("start_date", "end_date", "reason")
        widgets = {
            "start_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "end_date": forms.DateInput(attrs={"class": "form-control", "type": "date"}),
            "reason": forms.Textarea(attrs={"class": "form-control", "rows": 4, "placeholder": "Describe the reason for this leave request"}),
        }


class LeaveScheduleCoverageForm(forms.Form):
    schedule_id = forms.IntegerField(widget=forms.HiddenInput)
    covering_admin = forms.ModelChoiceField(
        queryset=AdminUser.objects.none(),
        empty_label="Choose an available operator",
        widget=forms.Select(attrs={"class": "form-select form-select-sm"}),
    )

    def __init__(self, *args, available_operators=None, **kwargs):
        super().__init__(*args, **kwargs)
        if available_operators is not None:
            self.fields["covering_admin"].queryset = available_operators
