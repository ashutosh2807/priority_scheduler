"""Local launcher readiness probe; no user or credential data is exposed."""
from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.http import require_GET

from apps.scheduler.control_client import SchedulerApiClient, SchedulerApiError
from portal_revision import current_portal_revision


# Capture once while URL configuration loads. Recomputing per request could
# describe new Python on disk while the process still serves old imported code.
LOADED_PORTAL_REVISION = current_portal_revision()


@require_GET
def health(request):
    connected = False
    try:
        snapshot = SchedulerApiClient().snapshot()
        connected = snapshot.get("meta", {}).get("service") == "scheduler-control-api"
    except SchedulerApiError:
        pass
    return JsonResponse({
        "service": "itrp-scheduler-portal",
        "portal_revision": LOADED_PORTAL_REVISION,
        "scheduler_api_base_url": settings.SCHEDULER_API_BASE_URL,
        "scheduler_connected": connected,
    })
