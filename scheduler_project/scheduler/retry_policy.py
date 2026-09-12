"""The non-disruptive retry policy inherited from the Oracle scheduler."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from datetime_compat import parse_iso_datetime
import json
from config.settings import MAX_SCHEDULED_ATTEMPTS


class RetryPolicy:
    """Decide whether a failed occurrence may use a global retry slot."""

    DEFAULT_WINDOWS = "ALL_DAY"

    @staticmethod
    def max_attempts_for(job):
        """Total automatic attempts, including the first; invalid legacy data uses the default."""
        if job is None:
            return MAX_SCHEDULED_ATTEMPTS

        config = job.get("run_config", {}) if isinstance(job, dict) else getattr(job, "run_config", {})
        raw = RetryPolicy._normalise_run_config(config)
        value = raw.get("MAX_ATTEMPTS") if isinstance(raw, dict) else None
        if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 100:
            return value
        return MAX_SCHEDULED_ATTEMPTS

    @staticmethod
    def _normalise_run_config(value):
        if value is None:
            return {}
        if isinstance(value, dict):
            normalised = {}
            for key, attempt_value in value.items():
                if not isinstance(key, str):
                    normalised[str(key)] = attempt_value
                    continue
                if key.strip().upper() == "MAX_ATTEMPTS":
                    normalised["MAX_ATTEMPTS"] = attempt_value
            return normalised
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return {}
            if not isinstance(parsed, dict):
                return {}
            return RetryPolicy._normalise_run_config(parsed)
        return {}

    def __init__(self, windows=None, lookback_days=15):
        self.lookback_days = max(1, int(lookback_days))
        self.windows = self._parse_windows(windows or self.DEFAULT_WINDOWS)

    def is_retry_window(self, current_datetime):
        value = self._as_datetime(current_datetime)
        current_time = value.time().replace(tzinfo=None)
        return any(self._contains(start, end, current_time) for start, end in self.windows)

    def is_retryable_report_date(self, report_date, current_date):
        report = self._as_date(report_date)
        today = self._as_date(current_date)
        if report is None:
            return False
        return today - timedelta(days=self.lookback_days) <= report <= today

    def next_window_hint(self, current_datetime):
        """Human-readable operational hint, without inventing a date/time zone."""
        value = self._as_datetime(current_datetime)
        if self.is_retry_window(value):
            return "Retry window is open."
        return "Retries wait for " + ", ".join(f"{start:%H:%M}–{end:%H:%M}" for start, end in self.windows) + "."

    @classmethod
    def _parse_windows(cls, value):
        if str(value).strip().upper() == "ALL_DAY":
            return ((time.min, time.min),)
        parsed = []
        for segment in str(value).split(","):
            segment = segment.strip()
            if not segment:
                continue
            try:
                start_text, end_text = (part.strip() for part in segment.split("-", 1))
                start = cls._parse_time(start_text)
                end = cls._parse_time(end_text)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Invalid retry window {segment!r}; use HH:MM-HH:MM.") from exc
            if start == end:
                raise ValueError("A retry window start and end must be different.")
            parsed.append((start, end))
        if not parsed:
            raise ValueError("At least one retry window is required.")
        return tuple(parsed)

    @staticmethod
    def _contains(start, end, current):
        if start < end:
            return start <= current < end
        return current >= start or current < end

    @staticmethod
    def _parse_time(value):
        return datetime.strptime(value, "%H:%M").time()

    @staticmethod
    def _as_datetime(value):
        if isinstance(value, datetime):
            return value
        if isinstance(value, date):
            return datetime.combine(value, time.min)
        if isinstance(value, str):
            return parse_iso_datetime(value)
        raise TypeError("current_datetime must be a datetime, date, or ISO datetime string.")

    @staticmethod
    def _as_date(value):
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value[:10])
        raise TypeError("report_date must be a date, datetime, ISO date string, or None.")
