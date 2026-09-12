"""Persistent service controls and immutable operational audit records.

The scheduler is the owner of the operational state.  Both the web portal and
the command-line console submit an intent to the scheduler control API; this
repository makes that intent durable and attributable before the next worker
cycle acts on it.
"""

from __future__ import annotations

import json
from datetime import datetime

from database.sqlite_db import get_connection
from repositories.worker_logging import atomic_logging, log_operation


class OperationsRepository:
    """Store the safe global stop switch and control-plane audit trail."""

    def __init__(self, connection=None, event_logger=None):
        self.connection = connection
        self.event_logger = event_logger

    def _get_connection(self):
        return self.connection if self.connection is not None else get_connection()

    @staticmethod
    def _now():
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _json(value):
        return json.dumps(value, default=str, sort_keys=True) if value is not None else None

    def get_scheduler_control(self):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            connection.execute(
                """
                INSERT INTO scheduler_control (control_id, scheduler_enabled, updated_at)
                VALUES (1, 1, ?)
                ON CONFLICT(control_id) DO NOTHING
                """,
                (self._now(),),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT scheduler_enabled, updated_at, updated_by, reason
                FROM scheduler_control
                WHERE control_id = 1
                """
            ).fetchone()
            return dict(row) if row is not None else {"scheduler_enabled": 1}
        finally:
            if close_connection:
                connection.close()

    def set_scheduler_enabled(self, enabled, actor=None, reason=None):
        """Safely gate future cycles; an already-running Oracle call is untouched."""
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            with atomic_logging(connection):
                before_row = connection.execute('SELECT * FROM scheduler_control WHERE control_id=1').fetchone()
                before = dict(before_row) if before_row else {'scheduler_enabled': 1}
                after = {
                    "scheduler_enabled": 1 if enabled else 0,
                    "updated_at": self._now(),
                    "updated_by": self._clean_text(actor),
                    "reason": self._clean_text(reason),
                }
                connection.execute(
                    """
                    INSERT INTO scheduler_control (
                        control_id, scheduler_enabled, updated_at, updated_by, reason
                    ) VALUES (1, ?, ?, ?, ?)
                    ON CONFLICT(control_id) DO UPDATE SET
                        scheduler_enabled = excluded.scheduler_enabled,
                        updated_at = excluded.updated_at,
                        updated_by = excluded.updated_by,
                        reason = excluded.reason
                    """,
                    (
                        after["scheduler_enabled"],
                        after["updated_at"],
                        after["updated_by"],
                        after["reason"],
                    ),
                )
                self.record_audit(
                    action="SCHEDULER_STARTED" if enabled else "SCHEDULER_STOPPED",
                    target_type="scheduler.service",
                    target_id="service",
                    target_label="Scheduler worker",
                    actor=actor,
                    reason=reason,
                    before_state=before,
                    after_state=after,
                    source="CONTROL_API", connection=connection, commit=False,
                )
                return after
        finally:
            if close_connection:
                connection.close()

    def is_scheduler_enabled(self):
        return bool(self.get_scheduler_control().get("scheduler_enabled", 1))

    def get_queue_order(self):
        connection = self._get_connection()
        try:
            return {
                row["occurrence_key"]: row["position"]
                for row in connection.execute(
                    "SELECT occurrence_key, position FROM scheduler_queue_order ORDER BY position"
                ).fetchall()
            }
        finally:
            if connection is not self.connection:
                connection.close()

    def set_queue_order(self, occurrence_keys, actor=None, reason=None):
        """Persist ordering and audit in one transaction, independently of READY."""
        connection = self._get_connection()
        try:
            before = [row["occurrence_key"] for row in connection.execute(
                "SELECT occurrence_key FROM scheduler_queue_order ORDER BY position"
            ).fetchall()]
            now = self._now()
            with atomic_logging(connection):
                connection.execute("DELETE FROM scheduler_queue_order")
                connection.executemany(
                    "INSERT INTO scheduler_queue_order VALUES (?, ?, ?, ?, ?)",
                    [(key, index, now, self._clean_text(actor), self._clean_text(reason))
                     for index, key in enumerate(occurrence_keys, 1)],
                )
                audit_cursor = connection.execute(
                    """INSERT INTO scheduler_operation_audit (
                        occurred_at, actor, action, target_type, target_id, target_label,
                        reason, before_state, after_state, source
                    ) VALUES (?, ?, 'QUEUE_REORDERED', 'scheduler.queue', 'queue',
                              'Live execution queue', ?, ?, ?, 'CONTROL_API')""",
                    (now, self._clean_text(actor), self._clean_text(reason),
                     self._json(before), self._json(occurrence_keys)),
                )
                if self.event_logger is not None:
                    log_operation(self.event_logger, connection, audit_cursor.lastrowid)
            return dict(zip(occurrence_keys, range(1, len(occurrence_keys) + 1)))
        finally:
            if connection is not self.connection:
                connection.close()

    def record_audit(
        self,
        *,
        action,
        target_type,
        target_id=None,
        target_label=None,
        actor=None,
        reason=None,
        before_state=None,
        after_state=None,
        source="CONTROL_API",
        correlation_id=None,
        connection=None,
        commit=True,
    ):
        supplied_connection = connection is not None
        connection = connection if supplied_connection else self._get_connection()
        close_connection = not supplied_connection and connection is not self.connection
        try:
            cursor = connection.execute(
                """
                INSERT INTO scheduler_operation_audit (
                    occurred_at, actor, action, target_type, target_id, target_label,
                    reason, before_state, after_state, source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self._now(),
                    self._clean_text(actor),
                    str(action).strip().upper(),
                    str(target_type).strip(),
                    None if target_id is None else str(target_id),
                    self._clean_text(target_label),
                    self._clean_text(reason),
                    self._json(before_state),
                    self._json(after_state),
                    str(source or "CONTROL_API").strip().upper(),
                ),
            )
            if self.event_logger is not None:
                log_operation(self.event_logger, connection, cursor.lastrowid, correlation_id)
            if commit:
                connection.commit()
            return cursor.lastrowid
        except Exception:
            if commit:
                connection.rollback()
            raise
        finally:
            if close_connection:
                connection.close()

    def get_recent_audit(self, limit=100):
        connection = self._get_connection()
        close_connection = connection is not self.connection
        try:
            rows = connection.execute(
                """
                SELECT id, occurred_at, actor, action, target_type, target_id,
                       target_label, reason, before_state, after_state, source
                FROM scheduler_operation_audit
                ORDER BY id DESC
                LIMIT ?
                """,
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            if close_connection:
                connection.close()

    @staticmethod
    def _clean_text(value):
        if value is None:
            return None
        text = str(value).strip()
        return text[:500] if text else None
