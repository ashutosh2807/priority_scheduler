from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO
import json
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from django.test import SimpleTestCase

from run_portal import main, portal_health, scheduler_snapshot, stop_children, wait_for_ready, worker_command, selected_master_source
from portal_revision import current_portal_revision


class LauncherTests(SimpleTestCase):
    @patch.dict("run_portal.os.environ", {}, clear=True)
    def test_master_source_defaults_to_oracle_and_explicit_file_mode_is_retained(self):
        self.assertEqual(selected_master_source(None, {}), "oracle")
        self.assertEqual(selected_master_source(None, {"SCHEDULER_MASTER_SOURCE": "file"}), "file")
        self.assertEqual(selected_master_source("oracle", {"SCHEDULER_MASTER_SOURCE": "file"}), "oracle")

    def test_default_worker_uses_oracle_calendar_without_execution(self):
        command = worker_command(Path("worker"))
        self.assertIn("--monitor", command)
        self.assertEqual(command[command.index("--calendar-source") + 1], "oracle")
        self.assertNotIn("--monitor", worker_command(Path("worker"), execute=True))

    @patch("run_portal.urlopen", side_effect=URLError("connection refused"))
    def test_disconnected_worker_can_be_started(self, request):
        self.assertIsNone(scheduler_snapshot("http://127.0.0.1:8091"))

    @patch("run_portal.urlopen", side_effect=HTTPError("http://localhost", 401, "unauthorized", {}, None))
    def test_wrong_token_is_not_treated_as_absent_worker(self, request):
        with self.assertRaisesRegex(RuntimeError, "matching API token"):
            scheduler_snapshot("http://127.0.0.1:8091", "test")

    @patch("run_portal.urlopen")
    def test_non_scheduler_service_is_not_reused(self, request):
        request.return_value.__enter__.return_value.read.return_value = b'{"meta": {"service": "other"}}'
        with self.assertRaisesRegex(RuntimeError, "different service"):
            scheduler_snapshot("http://127.0.0.1:8091")

    @patch("run_portal.urlopen")
    def test_scheduler_probe_uses_worker_token_header(self, request):
        request.return_value.__enter__.return_value.read.return_value = b'{"meta": {"service": "scheduler-control-api"}}'
        scheduler_snapshot("http://127.0.0.1:8091", "test-api-token")
        headers = {key.lower(): value for key, value in request.call_args.args[0].header_items()}
        self.assertEqual(headers.get("x-scheduler-token"), "test-api-token")
        self.assertNotIn("authorization", headers)

    @patch("run_portal.urlopen")
    def test_invalid_scheduler_json_is_reported_as_startup_error(self, request):
        request.return_value.__enter__.return_value.read.return_value = b'<html>Other app</html>'
        with self.assertRaisesRegex(RuntimeError, "JSON response"):
            scheduler_snapshot("http://127.0.0.1:8091")

    @patch("run_portal.urlopen")
    def test_non_object_scheduler_payload_is_not_reused(self, request):
        request.return_value.__enter__.return_value.read.return_value = b'[]'
        with self.assertRaisesRegex(RuntimeError, "different service"):
            scheduler_snapshot("http://127.0.0.1:8091")

    @patch("run_portal.urlopen")
    def test_portal_reuse_requires_identity_and_matching_api(self, request):
        request.return_value.__enter__.return_value.read.return_value = json.dumps({
            "service": "itrp-scheduler-portal", "scheduler_api_base_url": "http://127.0.0.1:8091",
            "portal_revision": current_portal_revision(),
        }).encode()
        self.assertEqual(portal_health("http://127.0.0.1:8010", "http://127.0.0.1:8091/")["service"], "itrp-scheduler-portal")
        self.assertEqual(request.call_args.args[0], "http://127.0.0.1:8010/healthz/")
        with self.assertRaisesRegex(RuntimeError, "another scheduler API"):
            portal_health("http://127.0.0.1:8010", "http://127.0.0.1:8092")

    @patch("run_portal.urlopen")
    def test_arbitrary_http_200_is_not_a_portal(self, request):
        request.return_value.__enter__.return_value.read.return_value = b'{"service":"other"}'
        with self.assertRaisesRegex(RuntimeError, "different service"):
            portal_health("http://127.0.0.1:8010", "http://127.0.0.1:8091")

    @patch("run_portal.urlopen")
    def test_rebranded_portal_is_reused_without_starting_another_service(self, request):
        request.return_value.__enter__.return_value.read.return_value = json.dumps({
            "service": "itrp-scheduler-portal", "scheduler_api_base_url": "http://127.0.0.1:8091",
            "scheduler_connected": True, "portal_revision": current_portal_revision(),
        }).encode()
        health = portal_health("http://127.0.0.1:8010", "http://127.0.0.1:8091")
        self.assertEqual(health["service"], "itrp-scheduler-portal")

    @patch("run_portal.urlopen", side_effect=HTTPError("http://localhost", 404, "not found", {}, None))
    def test_older_portal_without_health_identity_is_not_silently_reused(self, request):
        with self.assertRaisesRegex(RuntimeError, "older service"):
            portal_health("http://127.0.0.1:8010", "http://127.0.0.1:8091")

    @patch("run_portal.urlopen")
    def test_existing_portal_with_broken_worker_connection_is_rejected(self, request):
        request.return_value.__enter__.return_value.read.return_value = json.dumps({
            "service": "itrp-scheduler-portal", "scheduler_api_base_url": "http://127.0.0.1:8091",
            "scheduler_connected": False, "portal_revision": current_portal_revision(),
        }).encode()
        with self.assertRaisesRegex(RuntimeError, "cannot authenticate or connect"):
            portal_health("http://127.0.0.1:8010", "http://127.0.0.1:8091")

    @patch("run_portal.urlopen")
    def test_legacy_and_stale_portals_are_not_silently_reused(self, request):
        for service, revision in (("sbi-scheduler-portal", None), ("itrp-scheduler-portal", None),
                                  ("itrp-scheduler-portal", "older-code")):
            with self.subTest(service=service, revision=revision):
                request.return_value.__enter__.return_value.read.return_value = json.dumps({
                    "service": service, "scheduler_api_base_url": "http://127.0.0.1:8091",
                    "scheduler_connected": True, "portal_revision": revision,
                }).encode()
                with self.assertRaisesRegex(RuntimeError, "older project code"):
                    portal_health("http://127.0.0.1:8010", "http://127.0.0.1:8091")

    @patch.dict("run_portal.os.environ", {}, clear=True)
    @patch("run_portal.dotenv_values", return_value={})
    @patch("run_portal.load_dotenv")
    @patch("run_portal.subprocess.Popen")
    @patch("run_portal.subprocess.run")
    @patch("run_portal.portal_health", return_value={"service": "itrp-scheduler-portal"})
    @patch("run_portal.scheduler_snapshot", return_value={"meta": {"master_source": "oracle", "execution_enabled": True}})
    def test_reusing_current_portal_does_not_duplicate_audit_or_worker(self, snapshot, health, run, popen, load, values):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            project = root / "scheduler_project"
            project.mkdir()
            (project / "main.py").touch()
            portal = root / "scheduler_web"
            portal.mkdir()
            output = StringIO()
            with patch("run_portal.BASE_DIR", portal), redirect_stdout(output):
                self.assertEqual(main([]), 0)
            popen.assert_not_called()
            run.assert_not_called()
            self.assertIn("Reusing the current portal", output.getvalue())
            self.assertNotIn("Keep this launcher open", output.getvalue())

    @patch("run_portal.time.sleep")
    def test_spawned_service_is_not_ready_until_probe_succeeds(self, sleep):
        process, probe = Mock(), Mock(side_effect=[None, {"service": "ready"}])
        process.poll.return_value = None
        self.assertEqual(wait_for_ready(process, probe, "Portal"), {"service": "ready"})
        self.assertEqual(probe.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_spawned_service_exit_is_reported_before_url_is_advertised(self):
        process = Mock(returncode=1)
        process.poll.return_value = 1
        with self.assertRaisesRegex(RuntimeError, "Portal exited with code 1"):
            wait_for_ready(process, Mock(), "Portal")

    def test_readiness_timeout_is_bounded(self):
        with self.assertRaisesRegex(RuntimeError, "did not become ready"):
            wait_for_ready(Mock(), Mock(), "Portal", timeout=0)

    @patch("run_portal.os.name", "posix")
    def test_shutdown_only_terminates_owned_running_processes(self):
        exited, running = Mock(), Mock()
        exited.poll.return_value = 1
        running.poll.return_value = None
        stop_children([exited, running])
        exited.terminate.assert_not_called()
        running.terminate.assert_called_once()

    @patch("run_portal.subprocess.run")
    @patch("run_portal.os.name", "nt")
    def test_windows_shutdown_terminates_owned_redirector_tree(self, run):
        exited, running = Mock(), Mock()
        exited.poll.return_value = 1
        running.poll.return_value = None
        running.pid = 12345
        stop_children([exited, running])
        self.assertEqual(run.call_args.args[0], ["taskkill", "/PID", "12345", "/T", "/F"])
        self.assertEqual(run.call_count, 1)
        running.wait.assert_called_once_with(timeout=8)


class PortalRevisionTests(SimpleTestCase):
    def test_runtime_python_changes_revision_but_reloadable_assets_and_unloaded_files_do_not(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "config").mkdir()
            (root / "config" / "urls.py").write_text("urlpatterns = []", encoding="utf-8")
            initial = current_portal_revision(root)
            (root / ".env").write_text("PRIVATE_TOKEN=not-source", encoding="utf-8")
            (root / "db.sqlite3").write_bytes(b"runtime data")
            self.assertEqual(current_portal_revision(root), initial)
            (root / "config" / "urls.py").write_text("urlpatterns = ['sulog/']", encoding="utf-8")
            routes = current_portal_revision(root)
            self.assertNotEqual(routes, initial)
            (root / "apps" / "sulog").mkdir(parents=True)
            (root / "apps" / "sulog" / "apps.py").write_text("name = 'apps.sulog'", encoding="utf-8")
            app = current_portal_revision(root)
            self.assertNotEqual(app, routes)
            (root / "templates").mkdir()
            template = root / "templates" / "navigation.html"
            template.write_text("SULOG ELOADLOG MAILER", encoding="utf-8")
            self.assertEqual(current_portal_revision(root), app)
            template.write_text("Updated application navigation", encoding="utf-8")
            (root / "apps" / "sulog" / "test_views.py").write_text("test code", encoding="utf-8")
            for directory in ("tests", "migrations"):
                folder = root / "apps" / "sulog" / directory
                folder.mkdir()
                (folder / "__init__.py").touch()
                (folder / "fixture.py").write_text("not runtime code", encoding="utf-8")
            self.assertEqual(current_portal_revision(root), app)
