from .models import AuditLog
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
import json


def object_snapshot(obj):
    """Concrete business fields only; credentials never enter the audit payload."""
    excluded = {"password", "password1", "password2", "secret", "token", "api_key"}
    values = {field.name: getattr(obj, field.attname) for field in obj._meta.concrete_fields
              if not any(part in field.name.lower() for part in excluded)}
    return json.loads(json.dumps(values, cls=DjangoJSONEncoder))


def _create_audit(**fields):
    from .delivery import enqueue_audit
    with transaction.atomic():
        entry = AuditLog.objects.create(**fields)
        enqueue_audit(entry)
    return entry


def record_action(actor, action: str, obj, *, reason: str = "", changes: dict | None = None) -> AuditLog:
    """Create an immutable, UI-visible audit record for a Django-side action."""
    return _create_audit(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        object_type=obj._meta.label,
        object_id=str(obj.pk),
        object_label=str(obj),
        reason=reason,
        changes=changes if changes is not None else {"snapshot": object_snapshot(obj)},
    )


def record_external_action(actor, action: str, *, object_type: str, object_id: str, object_label: str, reason: str = "", changes: dict | None = None) -> AuditLog:
    """Audit a UI action against scheduler-owned data, not a Django ORM object."""
    return _create_audit(
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        object_type=object_type,
        object_id=str(object_id),
        object_label=object_label,
        reason=reason,
        changes=changes or {},
    )
