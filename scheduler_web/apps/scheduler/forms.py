from __future__ import annotations

from datetime import date
import re

from django import forms

from apps.accounts.models import AdminUser


SCHEDULE_FREQUENCIES = (
    "DAILY", "WEEKLY", "FORTNIGHTLY", "MONTHLY", "QUARTERLY", "HALF-YEARLY", "ANNUALLY",
)


class ScheduleProfileForm(forms.Form):
    """Django-owned human context for a scheduler-owned schedule definition."""

    responsible_operator = forms.ModelChoiceField(
        required=False,
        queryset=AdminUser.objects.none(),
        empty_label="Select the confirmation operator",
        label="Primary confirmation operator",
        help_text="Stored in the UI layer; used for leave coverage planning.",
    )
    description = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="UI-only operator description. It is never sent to the scheduler engine.",
    )
    operational_steps = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "placeholder": "Review source file\nConfirm report date\nNotify business owner"}),
        help_text="One operator step per line. Stored in the UI runbook, not Scheduler Master.",
    )
    backup_operator = forms.ModelChoiceField(
        required=False, queryset=AdminUser.objects.none(), empty_label="Use an available operator",
        label="Preferred confirmation cover",
    )
    expected_minutes = forms.IntegerField(
        required=False, min_value=1, max_value=10080, label="Expected execution time (minutes)",
        help_text="An operator estimate used to flag long-running work; it does not set a timeout.",
    )

    def __init__(self, *args, confirmation_needed=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.confirmation_needed = confirmation_needed
        for name in ("description", "operational_steps"):
            self.fields[name].widget.attrs["class"] = "form-control"
        self.fields["responsible_operator"].queryset = AdminUser.objects.filter(
            is_active=True, is_active_admin=True
        ).order_by("display_name")
        self.fields["responsible_operator"].widget.attrs["class"] = "form-select"
        self.fields["backup_operator"].queryset = self.fields["responsible_operator"].queryset
        self.fields["backup_operator"].widget.attrs["class"] = "form-select"
        self.fields["expected_minutes"].widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("backup_operator") and cleaned.get("backup_operator") == cleaned.get("responsible_operator"):
            self.add_error("backup_operator", "Choose someone other than the primary operator.")
        if self.confirmation_needed and not cleaned.get("responsible_operator"):
            self.add_error(
                "responsible_operator",
                "Choose the operator who owns this confirmation gate.",
            )
        return cleaned

    def clean_operational_steps(self):
        items = []
        for line in self.cleaned_data["operational_steps"].splitlines():
            item = line.strip().lstrip("-*").strip()
            if item:
                items.append(item)
        if len(items) > 20:
            raise forms.ValidationError("Keep the operator runbook to 20 steps or fewer.")
        return items


class SchedulerControlForm(forms.Form):
    override_datetime = forms.DateTimeField(
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
        help_text="Optional job-specific evaluation time. This does not change the system clock.",
    )
    reason = forms.CharField(required=False, max_length=500, widget=forms.Textarea(attrs={"rows": 2}), help_text="Saved to the portal audit log.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["override_datetime"].widget.attrs["class"] = "form-control"
        self.fields["reason"].widget.attrs["class"] = "form-control"


class ScheduleMasterConfigForm(forms.Form):
    """The deliberately small, worker-owned Schedule Master edit surface."""

    is_active = forms.BooleanField(
        required=False,
        label="Schedule is active",
        help_text="When cleared, the worker will not create future eligible occurrences for this schedule.",
    )
    from_time = forms.TimeField(
        required=False,
        input_formats=("%H:%M", "%H:%M:%S"),
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
        label="Run from",
    )
    to_time = forms.TimeField(
        required=False,
        input_formats=("%H:%M", "%H:%M:%S"),
        widget=forms.TimeInput(format="%H:%M", attrs={"type": "time"}),
        label="Run until",
    )
    max_attempts = forms.IntegerField(
        required=False,
        min_value=1,
        max_value=100,
        label="Maximum automatic attempts",
        widget=forms.NumberInput(attrs={"step": 1}),
        help_text=(
            "Includes the first run: 6 attempts allows 5 retries. Automatic retries stop after "
            "the planned execution day; later runs must be requested manually. "
            "Leave blank to keep the current limit."
        ),
    )
    reason = forms.CharField(
        required=False,
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
        help_text="Forwarded to the scheduler-side audit and retained in the portal audit.",
        label="Operator reason",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["is_active"].widget.attrs["class"] = "form-check-input"
        for name in ("from_time", "to_time", "max_attempts", "reason"):
            self.fields[name].widget.attrs["class"] = "form-control"

    def clean(self):
        cleaned = super().clean()
        from_time = cleaned.get("from_time")
        to_time = cleaned.get("to_time")
        if bool(from_time) != bool(to_time):
            raise forms.ValidationError("Enter both ends of the execution window, or leave both blank for no time restriction.")
        if from_time and to_time and from_time == to_time:
            raise forms.ValidationError("Run from and run until must be different. Overnight windows are allowed.")
        return cleaned


class ScheduleDefinitionForm(ScheduleProfileForm, ScheduleMasterConfigForm):
    """Guided schedule definition and human ownership, with no raw JSON editing."""

    schedule_id = forms.IntegerField(min_value=1, max_value=2147483647, label="Schedule ID",
        help_text="A unique permanent number. Existing schedule IDs cannot be changed or reused.")
    name = forms.CharField(max_length=128, label="Schedule name")
    package_name = forms.RegexField(
        regex=r"^[A-Za-z][A-Za-z0-9_$#]*(?:\.[A-Za-z][A-Za-z0-9_$#]*){1,2}$",
        max_length=386, label="Oracle procedure",
        help_text="Use PACKAGE.PROCEDURE or SCHEMA.PACKAGE.PROCEDURE.")
    frequencies = forms.MultipleChoiceField(
        choices=[(item, item.replace('_', ' ').title()) for item in (*SCHEDULE_FREQUENCIES, "SPECIFIC_DATE")],
        widget=forms.CheckboxSelectMultiple, label="Run frequency",
        help_text="Select one or more. Weekly uses the last working day; annual periods end on 31 March.")
    specific_dates = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}),
        help_text="For Specific date, enter YYYY-MM-DD dates separated by commas or new lines.")
    margin = forms.RegexField(regex=r"^T(?:\+\d{1,3})?$", initial="T+1", label="DATEMAST margin",
        help_text="T or T+n calendar days, for example T+1. The worker applies the banking calendar.")
    same_day = forms.BooleanField(required=False, label="Execute on the occurrence day",
        help_text="Leave cleared for next working day execution.")
    holiday_run = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple,
        choices=[("SAT", "Saturday"), ("SUN", "Sunday"), ("HOLIDAY", "Bank holiday")],
        label="Allow execution on", help_text="Leave cleared to use normal bank working days.")
    confirmation_needed = forms.BooleanField(required=False, label="Require operator confirmation",
        help_text="Each occurrence appears in confirmation alerts until its assigned operator confirms it.")

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["schedule_id"].disabled = editing
        self.fields["max_attempts"].required = True
        self.fields["max_attempts"].help_text = "Includes the first run: 6 attempts allows 5 retries. Choose 1 to 100 total attempts."
        self.fields["reason"].required = True
        self.fields["reason"].help_text = "Explain the change for the audit log."
        for name in ("schedule_id", "name", "package_name", "margin", "specific_dates"):
            self.fields[name].widget.attrs["class"] = "form-control"
        for name in ("same_day", "confirmation_needed"):
            self.fields[name].widget.attrs["class"] = "form-check-input"
        self.fields["description"].help_text = "Purpose of this schedule and useful operating context."
        self.fields["responsible_operator"].help_text = "The primary person responsible for confirming each occurrence."
        self.fields["operational_steps"].help_text = "One step per line; the guide and completed checks are retained in the portal."

    def clean_specific_dates(self):
        values = [value.strip() for value in re.split(r"[,\n\r]+", self.cleaned_data["specific_dates"]) if value.strip()]
        try:
            return sorted({date.fromisoformat(value).isoformat() for value in values})
        except ValueError:
            raise forms.ValidationError("Enter valid dates in YYYY-MM-DD format.") from None

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("confirmation_needed") and not cleaned.get("responsible_operator"):
            self.add_error("responsible_operator", "Select the primary confirmation operator.")
        if "SPECIFIC_DATE" in cleaned.get("frequencies", []) and not cleaned.get("specific_dates"):
            self.add_error("specific_dates", "Enter at least one date for this frequency.")
        if cleaned.get("specific_dates") and "SPECIFIC_DATE" not in cleaned.get("frequencies", []):
            self.add_error("frequencies", "Select Specific date to use these dates.")
        return cleaned

    def definition(self):
        data = self.cleaned_data
        start, end = data.get("from_time"), data.get("to_time")
        return {
            "name": data["name"], "package_name": data["package_name"],
            "margin": data["margin"], "same_day": data["same_day"],
            "is_active": data["is_active"], "time_flag": bool(start),
            "confirmation_needed": data["confirmation_needed"],
            "run_config": {
                "RUNS_ON": data["frequencies"], "HOLIDAY_RUN": data["holiday_run"],
                "RUN_BY": {"FROM_TIME": start.strftime("%H:%M"), "TO_TIME": end.strftime("%H:%M")} if start and end else None,
                "MAX_ATTEMPTS": data["max_attempts"],
                "SPECIFIC_DATES": data["specific_dates"] or None,
                "SPECIFIC_DATE": None,
            },
        }


class ScheduleDeleteForm(forms.Form):
    confirm_name = forms.CharField(label="Schedule name", help_text="Type the exact schedule name to confirm deletion.",
        widget=forms.TextInput(attrs={"class": "form-control", "autocomplete": "off"}))
    reason = forms.CharField(max_length=500, widget=forms.Textarea(attrs={"class": "form-control", "rows": 2}),
        label="Reason for deletion")
