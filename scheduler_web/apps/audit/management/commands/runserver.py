"""Retain Django staticfiles/autoreload behavior and manage audit delivery."""
import atexit
import os

from django.contrib.staticfiles.management.commands.runserver import Command as StaticfilesRunserverCommand

from apps.audit.background_delivery import AuditDeliveryThread


class Command(StaticfilesRunserverCommand):
    def inner_run(self, *args, **options):
        enabled = os.getenv("SCHEDULER_PORTAL_AUDIT_AUTOSTART", "1").strip().lower() in {"1", "true", "yes", "on"}
        if not enabled:
            return super().inner_run(*args, **options)

        # inner_run executes only in the serving child (or with --noreload),
        # never in the autoreloader supervisor or during migrate/check.
        delivery = AuditDeliveryThread()
        atexit.register(delivery.stop)
        try:
            delivery.start()
            return super().inner_run(*args, **options)
        finally:
            delivery.stop()
            atexit.unregister(delivery.stop)
