from collections import Counter
from html.parser import HTMLParser

from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import TestCase

from .forms import ScheduleDefinitionForm


class FormMarkup(HTMLParser):
    def __init__(self, markup):
        super().__init__()
        self.elements = []
        self.feed(markup)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))


class ScheduleDefinitionUiTests(TestCase):
    def render_form(self, form, **context):
        return render_to_string("scheduler/definition_form.html", {
            "form": form, "user": AnonymousUser(), "configuration_available": True,
            "csrf_token": "test-token", **context,
        })

    def test_validation_preserves_input_and_accessible_help_and_error_targets(self):
        form = ScheduleDefinitionForm({"schedule_id": 8, "name": "Branch settlement", "package_name": "invalid",
                                       "frequencies": ["SPECIFIC_DATE"], "margin": "T+1", "max_attempts": 9})
        self.assertFalse(form.is_valid())
        html = self.render_form(form)
        elements = FormMarkup(html).elements
        ids = Counter(attrs["id"] for _, attrs in elements if attrs.get("id"))
        self.assertEqual({name for name, count in ids.items() if count > 1}, set())
        for _, attrs in elements:
            for target in attrs.get("aria-describedby", "").split():
                self.assertEqual(ids[target], 1, f"Missing or duplicate description {target}")
            if attrs.get("href", "").startswith("#"):
                self.assertEqual(ids[attrs["href"][1:]], 1)
        self.assertIn('value="Branch settlement"', html)
        self.assertIn('value="9"', html)
        self.assertIn("Review the highlighted fields", html)
        self.assertIn("Enter at least one date for this frequency", html)

    def test_edit_form_preserves_field_contract_and_locked_id(self):
        form = ScheduleDefinitionForm(editing=True, initial={"schedule_id": 8, "name": "Branch settlement", "max_attempts": 9})
        html = self.render_form(form, schedule={"id": 8, "name": "Branch settlement"})
        elements = FormMarkup(html).elements
        controls = {attrs["name"]: attrs for tag, attrs in elements if tag in {"input", "select", "textarea"} and attrs.get("name")}
        self.assertTrue(set(form.fields).issubset(controls))
        self.assertIn("disabled", controls["schedule_id"])
        self.assertIn("required", controls["max_attempts"])
        self.assertIn("required", controls["reason"])
        self.assertIn("Save schedule", html)
        self.assertIn("Required for confirmation", html)

    def test_disconnected_form_keeps_save_disabled(self):
        html = self.render_form(ScheduleDefinitionForm(), configuration_available=False)
        buttons = [attrs for tag, attrs in FormMarkup(html).elements if tag == "button" and attrs.get("type") == "submit"]
        self.assertEqual(len(buttons), 1)
        self.assertIn("disabled", buttons[0])
