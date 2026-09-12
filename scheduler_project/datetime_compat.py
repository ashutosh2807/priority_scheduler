"""ISO timestamp parsing compatible with supported Python 3.10+ runtimes."""
from datetime import datetime


def parse_iso_datetime(value):
    """Normalize a UTC timestamp suffix without changing date-only inputs.

    Python 3.10 requires the numeric UTC offset. Preserve all existing timezone
    information and leave local-time conversion to the caller's current policy.
    """
    if isinstance(value, str) and value.endswith("Z") and len(value) > 11:
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)
