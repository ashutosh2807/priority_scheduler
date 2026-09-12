"""Presentation-only banking dates and timestamps; transport values remain ISO."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from django import template


register = template.Library()
IST = ZoneInfo("Asia/Kolkata")
MISSING_DATE = "—"


def _parse(value):
    if isinstance(value, (datetime, date)):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    # Parse a business date first so no timezone can move it to another day.
    try:
        return date.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None


def _placeholder(value):
    if value is None:
        return MISSING_DATE
    # Return ordinary text, including for a SafeString input, so an unknown
    # date or placeholder cannot bypass the template's normal autoescaping.
    text = str.__str__(value) if isinstance(value, str) else str(value)
    return text if text.strip() else MISSING_DATE


def _as_ist(value):
    if value.utcoffset() is None:
        # Worker timestamps without an offset are already local bank time.
        value = value.replace(tzinfo=IST)
    return value.astimezone(IST)


@register.filter
def indian_date(value):
    """DD/MM/YYYY; timestamps use their IST day, business dates stay unchanged."""
    parsed = _parse(value)
    if parsed is None:
        return _placeholder(value)
    try:
        if isinstance(parsed, datetime):
            parsed = _as_ist(parsed)
        return parsed.strftime("%d/%m/%Y")
    except (ValueError, OverflowError):
        return _placeholder(value)


@register.filter
def indian_datetime(value):
    """DD/MM/YYYY HH:mm IST; date-only values do not acquire an invented time."""
    parsed = _parse(value)
    if parsed is None:
        return _placeholder(value)
    try:
        if isinstance(parsed, datetime):
            return _as_ist(parsed).strftime("%d/%m/%Y %H:%M IST")
        return parsed.strftime("%d/%m/%Y")
    except (ValueError, OverflowError):
        return _placeholder(value)
