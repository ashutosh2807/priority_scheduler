"""Compatibility guards for legacy direct scheduler write integrations.

The UI must use :mod:`apps.scheduler.control_client`.  These classes remain
only to make an accidental return to direct Schedule Master or scheduler.db
writes fail loudly.
"""


class SchedulerBridgeError(RuntimeError):
    """A forbidden direct scheduler write was requested."""


class ScheduleMasterValidationError(SchedulerBridgeError):
    """Retained for callers that handled the previous bridge's error type."""


class ScheduleMasterStore:
    def create(self, payload):
        raise SchedulerBridgeError(
            "Schedule Master is protected. The Django UI cannot create or edit scheduler definitions."
        )

    def update(self, schedule_id, payload):
        raise SchedulerBridgeError(
            "Schedule Master is protected. Change business rules through the scheduler administration process."
        )


class LocalSchedulerControlBridge:
    def control(self, schedule_id, action, **kwargs):
        raise SchedulerBridgeError(
            "Direct scheduler.db control is disabled. Use the background scheduler control API."
        )
