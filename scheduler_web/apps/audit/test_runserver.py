import importlib
import os
import threading
from unittest.mock import Mock, patch

from django.contrib.staticfiles.management.commands.runserver import Command as StaticfilesRunserverCommand
from django.core.management import get_commands, load_command_class
from django.db import OperationalError
from django.test import SimpleTestCase

from .background_delivery import AuditDeliveryThread
from .management.commands.runserver import Command


class AuditRunserverCommandTests(SimpleTestCase):
    def test_discovery_selects_custom_runserver_and_preserves_staticfiles_options(self):
        self.assertEqual(get_commands()["runserver"], "apps.audit")
        command = load_command_class(get_commands()["runserver"], "runserver")
        self.assertIsInstance(command, StaticfilesRunserverCommand)
        parsed = command.create_parser("manage.py", "runserver").parse_args(["--noreload", "--nostatic"])
        self.assertFalse(parsed.use_reloader)
        self.assertFalse(parsed.use_static_handler)

    @patch("apps.audit.management.commands.runserver.AuditDeliveryThread")
    def test_import_and_other_management_commands_do_not_start_delivery(self, factory):
        importlib.import_module("apps.audit.apps")
        importlib.import_module("apps.audit.management.commands.runserver")
        load_command_class(get_commands()["migrate"], "migrate")
        load_command_class(get_commands()["check"], "check")
        factory.assert_not_called()

    @patch("apps.audit.management.commands.runserver.AuditDeliveryThread")
    @patch("django.core.management.commands.runserver.autoreload.run_with_reloader")
    def test_autoreloader_parent_never_constructs_a_delivery_worker(self, run_with_reloader, factory):
        command = Command()
        command.run(use_reloader=True)
        run_with_reloader.assert_called_once_with(command.inner_run, use_reloader=True)
        factory.assert_not_called()

    @patch.dict(os.environ, {"SCHEDULER_PORTAL_AUDIT_AUTOSTART": "1"})
    @patch("apps.audit.management.commands.runserver.atexit")
    @patch("apps.audit.management.commands.runserver.AuditDeliveryThread")
    def test_serving_child_starts_once_and_stops_when_server_returns(self, factory, exit_hooks):
        command = Command()
        with patch.object(StaticfilesRunserverCommand, "inner_run", return_value="served") as serve:
            result = command.inner_run(None, use_reloader=True, use_threading=True)
        self.assertEqual(result, "served")
        serve.assert_called_once_with(None, use_reloader=True, use_threading=True)
        factory.assert_called_once_with()
        factory.return_value.start.assert_called_once_with()
        factory.return_value.stop.assert_called_once_with()
        exit_hooks.register.assert_called_once_with(factory.return_value.stop)
        exit_hooks.unregister.assert_called_once_with(factory.return_value.stop)

    @patch.dict(os.environ, {"SCHEDULER_PORTAL_AUDIT_AUTOSTART": "1"})
    @patch("apps.audit.management.commands.runserver.atexit")
    @patch("apps.audit.management.commands.runserver.AuditDeliveryThread")
    def test_noreload_and_exception_exit_stop_delivery(self, factory, exit_hooks):
        command = Command()
        with patch.object(StaticfilesRunserverCommand, "inner_run", side_effect=SystemExit(0)):
            with self.assertRaises(SystemExit):
                command.run(use_reloader=False)
        factory.return_value.start.assert_called_once_with()
        factory.return_value.stop.assert_called_once_with()
        exit_hooks.unregister.assert_called_once_with(factory.return_value.stop)

    @patch.dict(os.environ, {"SCHEDULER_PORTAL_AUDIT_AUTOSTART": "0"})
    @patch("apps.audit.management.commands.runserver.atexit")
    @patch("apps.audit.management.commands.runserver.AuditDeliveryThread")
    def test_combined_launcher_opt_out_never_constructs_duplicate_worker(self, factory, exit_hooks):
        with patch.object(StaticfilesRunserverCommand, "inner_run", return_value="served"):
            self.assertEqual(Command().inner_run(use_reloader=False), "served")
        factory.assert_not_called()
        exit_hooks.register.assert_not_called()


class AuditDeliveryThreadTests(SimpleTestCase):
    @patch("apps.audit.background_delivery.connections.close_all")
    @patch("apps.audit.background_delivery.close_old_connections")
    @patch("apps.audit.background_delivery.deliver_batch")
    @patch("apps.audit.background_delivery.backfill_audits")
    def test_background_lifecycle_is_single_daemon_and_interruptible(self, backfill, deliver, close_old, close_all):
        delivered = threading.Event()
        deliver.side_effect = lambda **kwargs: delivered.set()
        worker = AuditDeliveryThread(interval=60)
        try:
            worker.start()
            original = worker.thread
            worker.start()
            self.assertIs(worker.thread, original)
            self.assertTrue(worker.thread.daemon)
            self.assertTrue(delivered.wait(timeout=2))
        finally:
            worker.stop()
        self.assertFalse(worker.thread.is_alive())
        backfill.assert_called_once_with(limit=50)
        deliver.assert_called_once_with(limit=50)
        self.assertGreaterEqual(close_old.call_count, 2)
        close_all.assert_called_once_with()

    @patch("apps.audit.background_delivery.connections.close_all")
    @patch("apps.audit.background_delivery.close_old_connections")
    @patch("apps.audit.background_delivery.deliver_batch")
    @patch("apps.audit.background_delivery.backfill_audits")
    def test_transient_errors_retry_without_logging_sensitive_details(self, backfill, deliver, close_old, close_all):
        worker = AuditDeliveryThread(interval=0)
        backfill.side_effect = [OperationalError("password=private"), RuntimeError("dsn=private"), 0]
        deliver.side_effect = lambda **kwargs: worker.stop_event.set()
        with self.assertLogs("apps.audit.background_delivery", level="WARNING") as logs:
            worker._run()
        self.assertEqual(backfill.call_count, 3)
        deliver.assert_called_once_with(limit=50)
        self.assertEqual(len(logs.output), 1)
        self.assertNotIn("private", logs.output[0])
        close_all.assert_called_once_with()

    @patch("apps.audit.background_delivery.connections.close_all")
    @patch("apps.audit.background_delivery.close_old_connections")
    @patch("apps.audit.background_delivery.deliver_batch")
    @patch("apps.audit.background_delivery.backfill_audits")
    def test_stop_during_backfill_prevents_another_delivery_request(self, backfill, deliver, close_old, close_all):
        worker = AuditDeliveryThread(interval=0)
        backfill.side_effect = lambda **kwargs: worker.stop_event.set()
        worker._run()
        deliver.assert_not_called()
        close_all.assert_called_once_with()
