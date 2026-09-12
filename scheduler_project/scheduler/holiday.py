import json
import os
from datetime import date, datetime


class HolidayEvaluator:
    """
    Determines the type of a calendar date and whether a job
    is allowed to run on that date.

    Day types:

        WORKING_DAY
        SAT
        SUN
        HOLIDAY

    Important:

        SAT/SUN and actual holidays are deliberately kept
        separate because RUN_CONFIG can distinguish them.

    Examples:

        HOLIDAY_RUN = ["HOLIDAY"]

            -> working days + actual holidays
            -> no closed second/fourth Saturday or Sunday

        HOLIDAY_RUN = ["SAT", "SUN", "HOLIDAY"]

            -> working days + Saturday + Sunday + holidays

        HOLIDAY_RUN = []

            -> working days only
    """

    WORKING_DAY = "WORKING_DAY"
    SAT = "SAT"
    SUN = "SUN"
    HOLIDAY = "HOLIDAY"

    def __init__(self, holidays=None, file_path=None, *, datemast=None, today_provider=None):
        """
        holidays:
            Iterable of dates representing actual holidays.

            These also apply to otherwise working Saturdays. Historical
            DATEMAST presence/absence takes precedence over defaults.
        """

        self.holidays = set()
        self.file_path = os.fspath(file_path) if file_path is not None else None
        self._file_signature = None
        self.datemast = datemast
        self.today_provider = today_provider or date.today

        if holidays:
            for holiday in holidays:
                self.add_holiday(holiday)

        if self.file_path:
            self.reload(force=True)

    def reload(self, force=False):
        """Refresh actual bank holidays from an atomically-written JSON list.

        Failed or partial upstream refreshes intentionally retain the last
        known valid holiday set so a transient Oracle issue cannot make the
        scheduler start treating bank holidays as working days.
        """
        if not self.file_path:
            return False
        try:
            signature = self._get_file_signature()
        except OSError:
            return False
        if not force and signature == self._file_signature:
            return False
        try:
            with open(self.file_path, "r", encoding="utf-8") as source:
                payload = json.load(source)
            values = self._extract_holidays(payload)
            parsed = {self._to_date(item) for item in values}
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return False
        self.holidays = parsed
        self._file_signature = signature
        return True

    # =========================================================
    # Add / Remove holidays
    # =========================================================

    def add_holiday(self, holiday):
        """
        Add one actual holiday.

        Retain Saturdays too: an otherwise working Saturday can be a holiday.
        """

        holiday = self._to_date(holiday)

        self.holidays.add(holiday)

    def remove_holiday(self, holiday):
        """
        Remove an actual holiday.
        """

        holiday = self._to_date(holiday)

        self.holidays.discard(holiday)

    def get_holidays(self):
        """
        Return configured actual holidays.
        """

        return sorted(self.holidays)

    # =========================================================
    # Date type
    # =========================================================

    def get_day_type(self, current_date):
        return self.classify_date(current_date)["kind"]

    def bind_datemast(self, datemast):
        """Keep the live provider, so Oracle refreshes also refresh inference."""
        self.datemast = datemast

    def published_dates(self):
        getter = getattr(self.datemast, "get_report_dates", None)
        if not callable(getter):
            return set()
        today = self._to_date(self.today_provider())
        return {self._to_date(value) for value in getter() if self._to_date(value) < today}

    def classify_date(self, current_date):
        """Use observed DATEMAST through T-1, then provisional bank defaults.

        Historical presence is authoritative even on a special working Sunday.
        An empty/unavailable published feed cannot establish missing-day holidays.
        The cutoff is the real clock, never a selected forecast/calendar date.
        """
        self.reload()
        day = self._to_date(current_date)
        today = self._to_date(self.today_provider())
        published = self.published_dates()
        available = bool(getattr(self.datemast, "available", True))
        covers_date = getattr(self.datemast, "covers_date", None)
        covered = covers_date(day) if callable(covers_date) else True
        observed = day < today and bool(published) and (day in published or (available and covered))
        closed_saturday = self.is_bank_saturday(day)
        default_kind = self.SAT if closed_saturday else self.SUN if self.is_sunday(day) else self.HOLIDAY
        if observed:
            working = day in published
            source = "datemast"
            reason = "Present in published DATEMAST." if working else "Absent from DATEMAST through T-1."
            kind = self.WORKING_DAY if working else default_kind
        else:
            working = self.default_working_day(day) and day not in self.holidays
            source = "configured_holiday" if day in self.holidays else "bank_calendar"
            kind = self.WORKING_DAY if working else default_kind
            reason = ("Configured holiday." if day in self.holidays else
                      "Second or fourth Saturday bank holiday." if closed_saturday else
                      "Sunday bank holiday." if self.is_sunday(day) else "Default bank working day.")
            if day < today:
                reason += (
                    " This date is outside the loaded DATEMAST coverage; bank-calendar rules are provisional."
                    if not covered else
                    " Published DATEMAST is unavailable; absence cannot establish a holiday."
                )
        label = ("Working Saturday" if self.is_saturday(day) else "Working Sunday" if self.is_sunday(day) else "Working day") if working else (
            "2nd Saturday" if closed_saturday and day.day <= 14 else
            "4th Saturday" if closed_saturday else "Sunday" if self.is_sunday(day) else
            "Holiday · absent from DATEMAST" if observed else "Holiday"
        )
        return {
            "date": day.isoformat(), "kind": kind, "is_working_day": working,
            "is_holiday": not working, "is_bank_holiday": not working and closed_saturday,
            "holiday_name": None if working else label,
            "day_label": label, "source": source, "reason": reason,
            "is_provisional": not observed,
        }

    # =========================================================
    # Weekend
    # =========================================================

    @staticmethod
    def is_saturday(current_date):
        current_date = HolidayEvaluator._to_date(current_date)

        return current_date.weekday() == 5

    @staticmethod
    def is_sunday(current_date):
        current_date = HolidayEvaluator._to_date(current_date)

        return current_date.weekday() == 6

    @staticmethod
    def is_weekend(current_date):
        current_date = HolidayEvaluator._to_date(current_date)

        return current_date.weekday() >= 5

    @staticmethod
    def is_bank_saturday(current_date):
        current_date = HolidayEvaluator._to_date(current_date)
        return current_date.weekday() == 5 and (current_date.day - 1) // 7 + 1 in {2, 4}

    @staticmethod
    def default_working_day(current_date):
        return not HolidayEvaluator.is_sunday(current_date) and not HolidayEvaluator.is_bank_saturday(current_date)

    # =========================================================
    # Actual holiday
    # =========================================================

    def is_holiday(self, current_date):
        """
        True for a configured or DATEMAST-inferred HOLIDAY kind.
        Closed default Saturdays/Sundays retain their separate override kinds.
        """

        return self.get_day_type(current_date) == self.HOLIDAY

    # =========================================================
    # Working day
    # =========================================================

    def is_working_day(self, current_date):
        """
        Use the same authoritative banking classification as the calendar API.
        """

        return self.get_day_type(current_date) == self.WORKING_DAY

    # =========================================================
    # Job eligibility on calendar day
    # =========================================================

    def can_run_on_day(
        self,
        job,
        current_date,
    ):
        """
        Determine whether the job's HOLIDAY_RUN configuration
        permits execution on current_date.

        Normal working days are always allowed.

        Saturday/Sunday/holiday behavior depends on
        HOLIDAY_RUN.
        """

        current_date = self._to_date(current_date)

        day_type = self.get_day_type(current_date)

        # -----------------------------------------------------
        # Normal working day
        # -----------------------------------------------------

        if day_type == self.WORKING_DAY:
            return True

        # -----------------------------------------------------
        # Read HOLIDAY_RUN
        # -----------------------------------------------------

        allowed_types = self._get_holiday_run(job)

        # -----------------------------------------------------
        # Weekend / holiday
        # -----------------------------------------------------

        return day_type in allowed_types

    # =========================================================
    # HOLIDAY_RUN
    # =========================================================

    @staticmethod
    def _get_holiday_run(job):
        """
        Extract HOLIDAY_RUN from RUN_CONFIG.

        Examples:

            ["SAT", "SUN", "HOLIDAY"]

        or:

            ["HOLIDAY"]

        or:

            "HOLIDAY"
        """

        run_config = getattr(
            job,
            "run_config",
            None,
        )

        if not isinstance(run_config, dict):
            return set()

        value = run_config.get("HOLIDAY_RUN")

        if value is None:
            return set()

        # -----------------------------------------------------
        # Single string
        # -----------------------------------------------------

        if isinstance(value, str):
            value = [value]

        # -----------------------------------------------------
        # Expected collection
        # -----------------------------------------------------

        if not isinstance(
            value,
            (list, tuple, set),
        ):
            return set()

        result = set()

        for item in value:

            if item is None:
                continue

            item = str(item).strip().upper()

            if item in {
                HolidayEvaluator.SAT,
                HolidayEvaluator.SUN,
                HolidayEvaluator.HOLIDAY,
            }:
                result.add(item)

        return result

    # =========================================================
    # Date conversion
    # =========================================================

    @staticmethod
    def _to_date(value):
        """
        Convert supported values to datetime.date.

        Supported:

            date
            datetime
            ISO date string
            ISO datetime string
        """

        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

        if isinstance(value, str):

            value = value.strip()

            # First try date format:
            #
            # 2026-09-08
            #
            try:
                return date.fromisoformat(value)
            except ValueError:
                pass

            # Then support ISO datetime:
            #
            # 2026-09-08T10:30:00
            #
            try:
                return datetime.fromisoformat(value).date()
            except ValueError:
                pass

        raise TypeError(
            f"Unsupported date value: {value!r}"
        )

    def _get_file_signature(self):
        stat = os.stat(self.file_path)
        return stat.st_mtime_ns, stat.st_size

    @staticmethod
    def _extract_holidays(payload):
        if isinstance(payload, dict):
            for key in ("holidays", "HOLIDAYS", "data", "records", "items"):
                if isinstance(payload.get(key), list):
                    payload = payload[key]
                    break
        if not isinstance(payload, list):
            raise ValueError("holiday snapshot must be a JSON list")
        values = []
        for item in payload:
            if isinstance(item, dict):
                item = item.get("holiday_date", item.get("HOLIDAY_DATE", item.get("date")))
            values.append(item)
        return values
