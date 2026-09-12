from .delivery import delivery_status


def audit_delivery_health(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {}
    return {"portal_audit_delivery": delivery_status()}
