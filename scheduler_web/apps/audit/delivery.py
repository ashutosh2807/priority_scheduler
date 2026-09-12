"""At-least-once audit delivery with immutable IDs and durable local retries."""
from datetime import date, timedelta
import json
import uuid

from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.db.models import Q, Count, Min, Max
from django.utils import timezone

from apps.scheduler.control_client import SchedulerApiClient, SchedulerApiError
from .models import AuditDelivery, AuditLog

MAX_BATCH_BYTES = 4 * 1024 * 1024 - 1024


def event_for(entry, event_id):
    actor = entry.actor
    event = {
        "event_id": str(event_id), "event_type": entry.action,
        "occurred_at": entry.created_at.isoformat(), "source": "PORTAL",
        "actor": actor.get_username() if actor else "system", "reason": entry.reason,
        "payload": {
            "portal_audit_id": entry.pk, "actor_id": entry.actor_id,
            "actor_name": actor.display_name if actor else "System",
            "action": entry.action, "object_type": entry.object_type,
            "object_id": entry.object_id, "object_label": entry.object_label,
            "reason": entry.reason, "changes": entry.changes,
        },
    }
    if entry.object_type in {"scheduler.ScheduleMaster", "scheduler.ScheduleProfile", "scheduler.RunbookProgress", "scheduler.WorkNote"}:
        if entry.object_id.isdigit():
            event.update(job_id=int(entry.object_id), name=entry.object_label)
        changes = entry.changes if isinstance(entry.changes, dict) else {}
        if changes.get("occurrence_key"):
            # Occurrence keys can exceed Oracle's 64-character correlation
            # column. Keep the original in payload.changes and use this UUID.
            event["correlation_id"] = str(event_id)
        if changes.get("report_date"):
            try:
                event["report_date"] = date.fromisoformat(str(changes["report_date"])).isoformat()
            except ValueError:
                pass
    return json.loads(json.dumps(event, cls=DjangoJSONEncoder))


def enqueue_audit(entry):
    """Must run in the same transaction as a newly created AuditLog."""
    event_id = uuid.uuid4()
    delivery, _ = AuditDelivery.objects.get_or_create(
        audit=entry, defaults={"event_id": event_id, "payload": event_for(entry, event_id)},
    )
    return delivery


def backfill_audits(limit=100):
    """Older records and direct ORM-created audit rows use the same outbox."""
    entries = list(AuditLog.objects.filter(delivery__isnull=True).select_related("actor").order_by("pk")[:limit])
    for entry in entries:
        with transaction.atomic():
            enqueue_audit(entry)
    return len(entries)


def deliver_batch(client=None, *, limit=50, now=None):
    """Lease locally, then release DB locks before making any network request."""
    now = now or timezone.now()
    client = client or SchedulerApiClient()
    token = uuid.uuid4()
    available = Q(leased_until__isnull=True) | Q(leased_until__lte=now)
    due = AuditDelivery.objects.filter(available, accepted_at__isnull=True, next_attempt_at__lte=now)
    candidates = list(due.values_list("pk", flat=True)[:limit])
    if not candidates:
        return {"attempted": 0, "accepted": 0, "pending": 0}
    # Optimistic claiming is valid on SQLite as well as row-locking databases.
    due.filter(pk__in=candidates).update(lease_token=token, leased_until=now+timedelta(seconds=max(60, int(getattr(client, "timeout", 3) or 3)+30)))
    claimed = list(AuditDelivery.objects.filter(lease_token=token).order_by("audit_id"))
    if not claimed:
        return {"attempted": 0, "accepted": 0, "pending": 0}
    # Preserve a little room for the JSON envelope and split large batches
    # rather than allowing one request to exceed the worker's 4 MB limit.
    outbound, request_bytes = [], len(b'{"events":[]}')
    for item in claimed:
        event_bytes = len(json.dumps(item.payload).encode("utf-8")) + 2
        if event_bytes > MAX_BATCH_BYTES:
            AuditDelivery.objects.filter(pk=item.pk, lease_token=token).update(
                lease_token=None, leased_until=None, attempts=item.attempts+1,
                next_attempt_at=now+timedelta(minutes=5),
                last_error="This event exceeds the worker audit payload limit. It is retained locally for review.",
            )
        elif request_bytes + event_bytes > MAX_BATCH_BYTES:
            AuditDelivery.objects.filter(pk=item.pk, lease_token=token).update(lease_token=None, leased_until=None)
        else:
            outbound.append(item)
            request_bytes += event_bytes
    if not outbound:
        return {"attempted": 0, "accepted": 0, "pending": len(claimed)}
    error = "The worker did not acknowledge this event. It will be retried."
    accepted = set()
    try:
        response = client.submit_logging_events([item.payload for item in outbound])
        values = response.get("accepted") if isinstance(response, dict) else None
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise SchedulerApiError("The worker returned an invalid audit acknowledgement.")
        accepted = set(values)
    except (SchedulerApiError, ValueError, TypeError) as exc:
        error = str(exc)[:1000]
    accepted_count = 0
    for item in outbound:
        attempts = item.attempts + 1
        values = {"attempts": attempts, "lease_token": None, "leased_until": None}
        if str(item.event_id) in accepted:
            values.update(accepted_at=now, last_error="")
            accepted_count += 1
        else:
            delay = min(300, 5 * (2 ** min(attempts-1, 6)))
            values.update(next_attempt_at=now+timedelta(seconds=delay), last_error=error)
        AuditDelivery.objects.filter(pk=item.pk, lease_token=token, accepted_at__isnull=True).update(**values)
    return {"attempted": len(outbound), "accepted": accepted_count, "pending": len(claimed)-accepted_count}


def delivery_status():
    pending = AuditDelivery.objects.filter(accepted_at__isnull=True)
    aggregate = pending.aggregate(pending=Count("pk"), oldest_pending_at=Min("created_at"), next_attempt_at=Min("next_attempt_at"))
    missing = AuditLog.objects.filter(delivery__isnull=True).count()
    aggregate["pending"] += missing
    aggregate["awaiting_backfill"] = missing
    aggregate["last_success_at"] = AuditDelivery.objects.aggregate(value=Max("accepted_at"))["value"]
    aggregate["last_error"] = pending.exclude(last_error="").order_by("next_attempt_at").values_list("last_error", flat=True).first() or ""
    return aggregate
