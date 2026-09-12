"""Supervised delivery process; API or Oracle outages never block UI requests."""
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, OperationalError

from apps.audit.delivery import backfill_audits, deliver_batch


class Command(BaseCommand):
    help = "Deliver durable portal audit events to the scheduler worker. Use --loop for continuous delivery."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true")
        parser.add_argument("--interval", type=float, default=5)
        parser.add_argument("--batch-size", type=int, default=50)

    def handle(self, *args, **options):
        if not 1 <= options["batch_size"] <= 200 or not 1 <= options["interval"] <= 60:
            raise CommandError("Batch size must be 1–200 and interval must be 1–60 seconds.")
        try:
            while True:
                close_old_connections()
                try:
                    backfill_audits(options["batch_size"])
                    result = deliver_batch(limit=options["batch_size"])
                    if result["attempted"]:
                        self.stdout.write(f"Audit delivery: {result['accepted']} accepted by worker; {result['pending']} retained for retry.")
                except OperationalError:
                    if not options["loop"]:
                        raise CommandError("The audit database is unavailable. Check migrations and retry.")
                    self.stderr.write("Audit database temporarily unavailable; events will be retried.")
                if not options["loop"]:
                    return
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            self.stdout.write("Audit delivery stopped; undelivered events remain in the portal database.")
