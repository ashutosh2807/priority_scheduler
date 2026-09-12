"""Lifecycle-bound portal audit delivery for standalone development servers."""
import logging
import threading

from django.db import close_old_connections, connections

from .delivery import backfill_audits, deliver_batch


logger = logging.getLogger(__name__)


class AuditDeliveryThread:
    """Only forwards saved audit events; it never starts the scheduler worker."""

    def __init__(self, *, interval=5, batch_size=50, shutdown_timeout=5):
        self.interval = interval
        self.batch_size = batch_size
        self.shutdown_timeout = shutdown_timeout
        self.stop_event = threading.Event()
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, name="portal-audit-delivery", daemon=True)
            self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread is not None and self.thread is not threading.current_thread():
            try:
                self.thread.join(timeout=self.shutdown_timeout)
            except RuntimeError:
                # A failed start or interpreter shutdown must not hold up exit.
                pass

    def _run(self):
        failure_reported = False
        try:
            while not self.stop_event.is_set():
                try:
                    close_old_connections()
                    backfill_audits(limit=self.batch_size)
                    if not self.stop_event.is_set():
                        deliver_batch(limit=self.batch_size)
                    failure_reported = False
                except Exception:
                    if not failure_reported:
                        logger.warning("Portal audit delivery is temporarily unavailable; saved events will be retried.")
                        failure_reported = True
                finally:
                    try:
                        close_old_connections()
                    except Exception:
                        pass
                self.stop_event.wait(self.interval)
        finally:
            # Django database connections are local to this delivery thread.
            try:
                connections.close_all()
            except Exception:
                pass
