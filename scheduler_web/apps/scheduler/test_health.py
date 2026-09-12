from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .control_client import SchedulerApiError
from config.health import LOADED_PORTAL_REVISION


class HealthTests(SimpleTestCase):
    @override_settings(SCHEDULER_API_TOKEN="secret-not-for-the-response")
    @patch("config.health.SchedulerApiClient")
    def test_readiness_confirms_real_authenticated_connection_without_secrets(self, api):
        api.return_value.snapshot.return_value = {"meta": {"service": "scheduler-control-api"}}
        response = self.client.get("/healthz/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["service"], "itrp-scheduler-portal")
        self.assertEqual(response.json()["portal_revision"], LOADED_PORTAL_REVISION)
        self.assertRegex(response.json()["portal_revision"], r"^[0-9a-f]{64}$")
        self.assertTrue(response.json()["scheduler_connected"])
        self.assertNotContains(response, "secret-not-for-the-response")

    @patch("config.health.SchedulerApiClient")
    def test_disconnected_is_explicit_and_no_post_allowed(self, api):
        api.return_value.snapshot.side_effect = SchedulerApiError("unavailable")
        self.assertFalse(self.client.get("/healthz/").json()["scheduler_connected"])
        self.assertEqual(self.client.post("/healthz/").status_code, 405)

    @patch("config.health.current_portal_revision", return_value="new-code-on-disk")
    @patch("config.health.SchedulerApiClient")
    def test_health_reports_loaded_code_not_new_files_on_disk(self, api, revision):
        api.return_value.snapshot.return_value = {"meta": {"service": "scheduler-control-api"}}
        self.assertEqual(self.client.get("/healthz/").json()["portal_revision"], LOADED_PORTAL_REVISION)
        revision.assert_not_called()
