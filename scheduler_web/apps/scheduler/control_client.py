"""Client boundary between the Django operations UI and scheduler worker.

The worker owns Schedule Master, DATEMAST, lifecycle transitions, priority
queue rebuilds and Oracle execution.  Django receives a snapshot and records
control *intent* through its API; it never opens the scheduler write database.
"""

from __future__ import annotations

import json
from http.client import HTTPException
from datetime import datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from django.conf import settings


class SchedulerApiError(RuntimeError):
    """The scheduler background control plane is unavailable or rejected a request."""


class SchedulerApiClient:
    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int | None = None):
        self.base_url = (base_url if base_url is not None else settings.SCHEDULER_API_BASE_URL).rstrip("/")
        self.token = token if token is not None else settings.SCHEDULER_API_TOKEN
        self.timeout = timeout if timeout is not None else settings.SCHEDULER_API_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def snapshot(self) -> dict:
        return self._request("GET", "/v1/operations/snapshot")

    def submit_logging_events(self, events):
        return self._request("POST", "/v1/logging/events", {"events": events})

    def logging_status(self):
        return self._request("GET", "/v1/logging/status")

    def calendar(self, start_date, days=31):
        return self._request("GET", f"/v1/operations/calendar?start_date={start_date.isoformat()}&days={int(days)}")

    def reorder_queue(self, occurrence_keys, queue_revision, *, actor, reason):
        return self._request("POST", "/v1/queue/reorder", {
            "occurrence_keys": occurrence_keys, "queue_revision": queue_revision, "actor": actor, "reason": reason,
        })

    def control(
        self,
        schedule_id: int,
        action: str,
        *,
        override_datetime=None,
        actor: str | None = None,
        reason: str | None = None,
        occurrence_key: str | None = None,
    ) -> dict:
        """Send an operator's control intent without granting Django ownership.

        ``actor`` and ``reason`` are optional metadata.  Existing callers can
        continue to use the smaller method signature, while the scheduler API
        may preserve these fields in its own audit history.
        """
        value = self._normalise_datetime(override_datetime) if override_datetime else None
        request_payload = {"override_datetime": value}
        if occurrence_key:
            request_payload["occurrence_key"] = occurrence_key
        if actor and str(actor).strip():
            request_payload["actor"] = str(actor).strip()
        if reason and str(reason).strip():
            request_payload["reason"] = str(reason).strip()
        payload = self._request(
            "POST",
            f"/v1/jobs/{int(schedule_id)}/controls/{action}",
            request_payload,
        )
        return payload.get("control") or {}

    def service_control(self, action: str, *, actor: str | None = None, reason: str | None = None) -> dict:
        """Safely start or stop *future* scheduler cycles through the worker.

        The endpoint is owned by the scheduler service.  It records the
        request and deliberately does not terminate an Oracle call already in
        progress; Django merely forwards the authenticated operator's intent.
        """
        action = str(action or "").strip().lower()
        if action not in {"start", "stop"}:
            raise SchedulerApiError("Scheduler service control supports only start or stop.")
        request_payload = {}
        if actor and str(actor).strip():
            request_payload["actor"] = str(actor).strip()
        if reason and str(reason).strip():
            request_payload["reason"] = str(reason).strip()
        payload = self._request(
            "POST",
            f"/v1/scheduler/controls/{action}",
            request_payload,
        )
        return payload.get("service_control") or {}

    def update_schedule_configuration(
        self,
        schedule_id: int,
        *,
        is_active: bool,
        run_by: dict | None,
        max_attempts: int | None = None,
        actor: str | None = None,
        reason: str | None = None,
    ) -> dict:
        """Ask the worker to update only its allowed Schedule Master fields.

        This narrow PATCH deliberately has no frequency, margin, package, or
        procedure fields. The scheduler validates and persists the bounded
        change in its own repository and returns the before/after snapshot.
        """
        request_payload = {
            "is_active": bool(is_active),
            "run_by": run_by,
        }
        if max_attempts is not None:
            request_payload["max_attempts"] = max_attempts
        if actor and str(actor).strip():
            request_payload["actor"] = str(actor).strip()
        if reason and str(reason).strip():
            request_payload["reason"] = str(reason).strip()
        payload = self._request(
            "PATCH",
            f"/v1/jobs/{int(schedule_id)}/configuration",
            request_payload,
        )
        return payload.get("configuration") or {}

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        if not self.configured:
            raise SchedulerApiError("The background scheduler control API is not configured.")
        headers = {"Accept": "application/json"}
        if self.token:
            headers["X-Scheduler-Token"] = self.token
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310 -- base URL is administrator config
                raw = response.read().decode("utf-8")
        except HTTPError as error:
            detail = ""
            try:
                detail = json.loads(error.read().decode("utf-8")).get("detail", "")
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise SchedulerApiError(detail or f"Scheduler control API returned HTTP {error.code}.") from error
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise SchedulerApiError("The background scheduler control API is unavailable.") from error
        try:
            return json.loads(raw)
        except json.JSONDecodeError as error:
            raise SchedulerApiError("The background scheduler returned an invalid response.") from error

    def create_schedule(self, definition, *, actor, reason):
        return self._request("POST", "/v1/jobs", {**definition, "actor": actor, "reason": reason}).get("schedule") or {}

    def update_schedule(self, schedule_id, definition, *, actor, reason):
        return self._request("PATCH", f"/v1/jobs/{int(schedule_id)}/definition",
                             {**definition, "actor": actor, "reason": reason}).get("schedule") or {}

    def delete_schedule(self, schedule_id, *, actor, reason):
        return self._request("DELETE", f"/v1/jobs/{int(schedule_id)}",
                             {"actor": actor, "reason": reason}).get("schedule") or {}

    def refresh_calendar(self, *, actor, reason):
        return self._request("POST", "/v1/operations/calendar/refresh", {"actor": actor, "reason": reason}).get("calendar") or {}

    @staticmethod
    def _normalise_datetime(value) -> str:
        if isinstance(value, datetime):
            return value.isoformat(timespec="seconds")
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).isoformat(timespec="seconds")


def get_scheduler_control_client() -> SchedulerApiClient:
    return SchedulerApiClient()
