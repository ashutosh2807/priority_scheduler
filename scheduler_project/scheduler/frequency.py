from calendar import monthrange
from datetime import date, datetime, timedelta
from scheduler.holiday import HolidayEvaluator


class FrequencyEvaluator:
    """
    Evaluates RUNS_ON scheduling rules.

    Supported frequencies:

        DAILY
        WEEKLY
        FORTNIGHTLY
        MONTHLY
        QUARTERLY
        BI-ANNUALLY / HALF-YEARLY
        ANNUALLY
        SPECIFIC_DATE

    RUNS_ON may contain more than one frequency.

    Example:

        {
            "RUNS_ON": [
                "WEEKLY",
                "MONTHLY"
            ]
        }

    means either rule can produce an occurrence.
    """

    DAILY = "DAILY"
    WEEKLY = "WEEKLY"
    FORTNIGHTLY = "FORTNIGHTLY"
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    HALF_YEARLY = "HALF-YEARLY"
    BI_ANNUALLY = "BI_ANNUALLY"
    ANNUALLY = "ANNUALLY"
    SPECIFIC_DATE = "SPECIFIC_DATE"

    FREQUENCIES = {
        DAILY,
        WEEKLY,
        FORTNIGHTLY,
        MONTHLY,
        QUARTERLY,
        HALF_YEARLY,
        BI_ANNUALLY,
        ANNUALLY,
        SPECIFIC_DATE,
    }

    # Keep older master data valid while presenting one clear canonical name.
    FREQUENCY_ALIASES = {
        "HALF_YEARLY": HALF_YEARLY,
        "HALF-YEARLY": HALF_YEARLY,
        "HALFYEARLY": HALF_YEARLY,
        "BI_ANNUALLY": HALF_YEARLY,
        "BI-ANNUALLY": HALF_YEARLY,
        "BIANNUALLY": HALF_YEARLY,
        "SPECIFIC_DATE": SPECIFIC_DATE,
        "ON_SPECIFIC_DATE": SPECIFIC_DATE,
        "ON A SPECIFIC DATE": SPECIFIC_DATE,
    }

    WEEKDAYS = {
        "MONDAY": 0,
        "MON": 0,
        "TUESDAY": 1,
        "TUE": 1,
        "TUES": 1,
        "WEDNESDAY": 2,
        "WED": 2,
        "THURSDAY": 3,
        "THU": 3,
        "THURS": 3,
        "FRIDAY": 4,
        "FRI": 4,
        "SATURDAY": 5,
        "SAT": 5,
        "SUNDAY": 6,
        "SUN": 6,
    }

    def __init__(self, working_day_checker=None):
        """Create an evaluator using the scheduler's working-day source.

        Weekly schedules mean the last working day of a Monday-to-Sunday week.
        The live checker includes DATEMAST and exceptional working weekends;
        the fallback closes Sundays and second/fourth Saturdays.
        """
        self.working_day_checker = working_day_checker or HolidayEvaluator.default_working_day

    # =========================================================
    # Public API
    # =========================================================

    def is_scheduled(
        self,
        job,
        current_date,
        reference_date=None,
    ):
        """
        Return True if current_date is a scheduled occurrence.

        Multiple RUNS_ON values are treated as OR.
        """

        current_date = self._to_date(
            current_date
        )

        config = self._get_run_config(
            job
        )

        frequencies = self._get_frequencies(
            config
        )

        if not frequencies:
            return False

        for frequency in frequencies:

            if self._matches_frequency(
                frequency,
                config,
                current_date,
                reference_date,
            ):
                return True

        return False

    # =========================================================
    # Previous occurrence
    # =========================================================

    def get_previous_occurrence(
        self,
        job,
        current_date,
        reference_date=None,
        include_current=True,
        max_days=3700,
    ):
        """
        Find the most recent scheduled occurrence.

        This searches backwards from current_date.

        For a new scheduling cycle this can be used to identify
        the occurrence that should enter STAGING.

        Existing staged occurrences should NOT call this method;
        they should use their persisted occurrence_date.
        """

        current_date = self._to_date(
            current_date
        )

        if not include_current:
            current_date -= timedelta(
                days=1
            )

        for offset in range(
            max_days + 1
        ):
            candidate = (
                current_date
                - timedelta(days=offset)
            )

            if self.is_scheduled(
                job,
                candidate,
                reference_date=reference_date,
            ):
                return candidate

        return None

    # =========================================================
    # Next occurrence
    # =========================================================

    def get_next_occurrence(
        self,
        job,
        current_date,
        reference_date=None,
        include_current=True,
        max_days=3700,
    ):
        """
        Find the next scheduled occurrence.

        Existing staged occurrences should not call this.
        """

        current_date = self._to_date(
            current_date
        )

        if not include_current:
            current_date += timedelta(
                days=1
            )

        for offset in range(
            max_days + 1
        ):
            candidate = (
                current_date
                + timedelta(days=offset)
            )

            if self.is_scheduled(
                job,
                candidate,
                reference_date=reference_date,
            ):
                return candidate

        return None

    # =========================================================
    # Frequency matching
    # =========================================================

    def _matches_frequency(
        self,
        frequency,
        config,
        current_date,
        reference_date,
    ):
        if frequency == self.DAILY:
            return True

        if frequency == self.SPECIFIC_DATE:
            return self._matches_specific_date(config, current_date)

        if frequency == self.WEEKLY:
            return self._matches_weekly(
                config,
                current_date,
            )

        if frequency == self.FORTNIGHTLY:
            return self._matches_fortnightly(
                config,
                current_date,
                reference_date,
            )

        if frequency == self.MONTHLY:
            return self._matches_monthly(
                config,
                current_date,
            )

        if frequency == self.QUARTERLY:
            return self._matches_quarterly(
                config,
                current_date,
            )

        if frequency == self.HALF_YEARLY:
            return self._matches_half_yearly(
                config,
                current_date,
            )

        if frequency == self.ANNUALLY:
            return self._matches_annually(
                config,
                current_date,
            )

        return False

    # =========================================================
    # DAILY
    # =========================================================

    def _matches_daily(
        self,
        current_date,
    ):
        return True

    # =========================================================
    # WEEKLY
    # =========================================================

    def _matches_weekly(
        self,
        config,
        current_date,
    ):
        run_day = (
            config.get("RUN_DAY")
        )

        if run_day is None:

            run_by = config.get(
                "RUN_BY"
            )

            if isinstance(
                run_by,
                dict,
            ):
                run_day = run_by.get(
                    "DAY"
                )

        # A deliberate custom weekday remains supported for an exceptional
        # schedule.  The normal ITRP weekly convention below is the final
        # *working* day, not a hard-coded Friday.
        if run_day is None:
            return self._is_last_working_day_of_week(current_date)

        weekday = self._get_weekday(
            run_day
        )

        if weekday is None:
            return False

        return (
            current_date.weekday()
            == weekday
        )

    def _is_last_working_day_of_week(self, current_date):
        """Whether this is the final working date in this Monday-Sunday week."""
        try:
            if not self.working_day_checker(current_date):
                return False
            week_end = current_date + timedelta(days=6 - current_date.weekday())
            candidate = current_date + timedelta(days=1)
            while candidate <= week_end:
                if self.working_day_checker(candidate):
                    return False
                candidate += timedelta(days=1)
            return True
        except (TypeError, ValueError):
            return False

    # =========================================================
    # FORTNIGHTLY
    # =========================================================

    def _matches_fortnightly(
        self,
        config,
        current_date,
        reference_date,
    ):
        """
        FORTNIGHTLY means two occurrences in every calendar month:

            15th
            Last day of the month

        This is a calendar rule, not a 14-day interval rule.

        The actual sending/execution date for a job with
        same_day=0 is handled by the eligibility/scheduling layer.
        This method only identifies the occurrence date.
        """

        # An explicit configuration can still override the default
        # occurrence days. RUN_DAY_OF_MONTH / DAY_OF_MONTH / numeric
        # RUN_DAY may be used for a single configured day.
        configured_day = self._get_run_day_of_month(config)

        if configured_day is not None:
            last_day = monthrange(
                current_date.year,
                current_date.month,
            )[1]

            effective_day = min(
                configured_day,
                last_day,
            )

            return (
                current_date.day
                == effective_day
            )

        # Default banking fortnightly schedule:
        #
        #     15th of the month
        #     last calendar day of the month
        #
        last_day = monthrange(
            current_date.year,
            current_date.month,
        )[1]

        return (
            current_date.day == 15
            or current_date.day == last_day
        )

    # =========================================================
    # MONTHLY
    # =========================================================

    def _matches_monthly(
        self,
        config,
        current_date,
    ):
        day = self._get_run_day_of_month(
            config
        )

        # Default monthly occurrence is the last calendar day.
        if day is None:
            day = monthrange(
                current_date.year,
                current_date.month,
            )[1]

        last_day = monthrange(
            current_date.year,
            current_date.month,
        )[1]

        # If configured day is beyond the month's length,
        # use the month's last day.
        effective_day = min(
            day,
            last_day,
        )

        return (
            current_date.day
            == effective_day
        )

    # =========================================================
    # QUARTERLY
    # =========================================================

    def _matches_quarterly(
        self,
        config,
        current_date,
    ):
        day = self._get_run_day_of_month(
            config
        )

        if day is None:
            # Quarter-end defaults to the last calendar day of
            # each quarter-end month.
            day = monthrange(
                current_date.year,
                current_date.month,
            )[1]

        months = self._get_months(
            config,
            # ITRP's financial year runs from April through March.
            default_months=(3, 6, 9, 12),
        )

        if current_date.month not in months:
            return False

        last_day = monthrange(
            current_date.year,
            current_date.month,
        )[1]

        effective_day = min(
            day,
            last_day,
        )

        return (
            current_date.day
            == effective_day
        )

    # =========================================================
    # HALF-YEARLY
    # =========================================================

    def _matches_half_yearly(
        self,
        config,
        current_date,
    ):
        """
        BI-ANNUALLY / HALF-YEARLY follows the banking financial-year
        convention configured for this scheduler.

        Default occurrence dates:

            31 March
            30 September

        If RUN_MONTH is explicitly configured, it overrides the
        default months. If no day is configured, the last calendar
        day of each selected month is used.
        """

        day = self._get_run_day_of_month(
            config
        )

        months = self._get_months(
            config,
            default_months=(3, 9),
        )

        if current_date.month not in months:
            return False

        if day is None:
            day = monthrange(
                current_date.year,
                current_date.month,
            )[1]

        last_day = monthrange(
            current_date.year,
            current_date.month,
        )[1]

        effective_day = min(
            day,
            last_day,
        )

        return (
            current_date.day
            == effective_day
        )

    # =========================================================
    # ANNUALLY
    # =========================================================

    def _matches_annually(
        self,
        config,
        current_date,
    ):
        # The financial year closes on 31 March.
        day = self._get_run_day_of_month(
            config
        )
        month = self._get_run_month(
            config
        )

        if month is None:
            month = 3

        if day is None:
            day = monthrange(current_date.year, month)[1]

        if current_date.month != month:
            return False

        last_day = monthrange(
            current_date.year,
            month,
        )[1]

        effective_day = min(
            day,
            last_day,
        )

        return (
            current_date.day
            == effective_day
        )

    # =========================================================
    # Configuration helpers
    # =========================================================

    def _matches_specific_date(self, config, current_date):
        """Match one or more explicit ISO dates held in RUN_CONFIG.

        A master row may use ``SPECIFIC_DATE`` for one date or
        ``SPECIFIC_DATES`` for a list.  This stays an occurrence rule; the
        normal SAME_DAY/working-day eligibility layer remains responsible for
        when it can execute.
        """
        values = config.get("SPECIFIC_DATES", config.get("SPECIFIC_DATE"))
        if values is None:
            return False
        if not isinstance(values, (list, tuple, set)):
            values = [values]
        for value in values:
            try:
                if self._to_date(value) == current_date:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def get_frequencies(self, job):
        """Return the normalized configured frequencies for ``job``.

        The occurrence planner needs to evaluate each frequency independently
        so a combined DAILY + FORTNIGHTLY job can produce two distinct
        report-date occurrences.  Exposing this small read-only helper avoids
        duplicating the aliases and validation rules held by this evaluator.
        """

        return list(
            self._get_frequencies(
                self._get_run_config(job)
            )
        )

    def matches_frequency(
        self,
        job,
        frequency,
        current_date,
        reference_date=None,
    ):
        """Evaluate one normalized frequency without OR-ing sibling rules."""

        frequency = str(frequency).strip().upper()
        frequency = self.FREQUENCY_ALIASES.get(
            frequency,
            frequency,
        )

        if frequency not in self.FREQUENCIES:
            return False

        return self._matches_frequency(
            frequency,
            self._get_run_config(job),
            self._to_date(current_date),
            reference_date,
        )

    @staticmethod
    def _get_run_config(job):
        config = getattr(
            job,
            "run_config",
            None,
        )

        if isinstance(
            config,
            dict,
        ):
            return config

        return {}

    def _get_frequencies(
        self,
        config,
    ):
        value = config.get(
            "RUNS_ON"
        )

        if value is None:
            return []

        if isinstance(
            value,
            str,
        ):
            value = [value]

        if not isinstance(
            value,
            (list, tuple, set),
        ):
            return []

        frequencies = []

        for item in value:
            frequency = str(item).strip().upper()
            frequency = self.FREQUENCY_ALIASES.get(frequency, frequency)

            if frequency in self.FREQUENCIES:
                frequencies.append(
                    frequency
                )

        return frequencies

    # =========================================================
    # Run day
    # =========================================================

    def _get_run_day_of_month(
        self,
        config,
    ):
        for key in (
            "RUN_DAY_OF_MONTH",
            "DAY_OF_MONTH",
        ):
            value = config.get(
                key
            )

            if value is not None:
                return self._to_positive_int(
                    value
                )

        # Numeric RUN_DAY is interpreted as day-of-month for
        # monthly/quarterly/etc.
        value = config.get(
            "RUN_DAY"
        )

        if value is not None:
            if isinstance(
                value,
                int,
            ):
                return value

            if isinstance(
                value,
                str,
            ):
                value = value.strip()

                if value.isdigit():
                    return int(value)

        return None

    # =========================================================
    # Month
    # =========================================================

    def _get_run_month(
        self,
        config,
    ):
        value = config.get(
            "RUN_MONTH"
        )

        if value is None:
            return None

        if isinstance(
            value,
            int,
        ):
            return (
                value
                if 1 <= value <= 12
                else None
            )

        if isinstance(
            value,
            str,
        ):
            value = value.strip().upper()

            month_names = {
                "JAN": 1,
                "JANUARY": 1,
                "FEB": 2,
                "FEBRUARY": 2,
                "MAR": 3,
                "MARCH": 3,
                "APR": 4,
                "APRIL": 4,
                "MAY": 5,
                "JUN": 6,
                "JUNE": 6,
                "JUL": 7,
                "JULY": 7,
                "AUG": 8,
                "AUGUST": 8,
                "SEP": 9,
                "SEPT": 9,
                "SEPTEMBER": 9,
                "OCT": 10,
                "OCTOBER": 10,
                "NOV": 11,
                "NOVEMBER": 11,
                "DEC": 12,
                "DECEMBER": 12,
            }

            if value in month_names:
                return month_names[value]

            if value.isdigit():
                number = int(value)

                if 1 <= number <= 12:
                    return number

        return None

    # =========================================================
    # Multiple months
    # =========================================================

    def _get_months(
        self,
        config,
        default_months,
    ):
        value = config.get(
            "RUN_MONTH"
        )

        if value is None:
            return set(
                default_months
            )

        if isinstance(
            value,
            (list, tuple, set),
        ):
            months = set()

            for item in value:
                month = self._parse_month(
                    item
                )

                if month is not None:
                    months.add(month)

            return months

        month = self._parse_month(
            value
        )

        if month is None:
            return set()

        return {month}

    def _parse_month(
        self,
        value,
    ):
        if isinstance(
            value,
            int,
        ):
            return (
                value
                if 1 <= value <= 12
                else None
            )

        if isinstance(
            value,
            str,
        ):
            value = value.strip().upper()

            names = {
                "JAN": 1,
                "JANUARY": 1,
                "FEB": 2,
                "FEBRUARY": 2,
                "MAR": 3,
                "MARCH": 3,
                "APR": 4,
                "APRIL": 4,
                "MAY": 5,
                "JUN": 6,
                "JUNE": 6,
                "JUL": 7,
                "JULY": 7,
                "AUG": 8,
                "AUGUST": 8,
                "SEP": 9,
                "SEPT": 9,
                "SEPTEMBER": 9,
                "OCT": 10,
                "OCTOBER": 10,
                "NOV": 11,
                "NOVEMBER": 11,
                "DEC": 12,
                "DECEMBER": 12,
            }

            if value in names:
                return names[value]

            if value.isdigit():
                number = int(value)

                if 1 <= number <= 12:
                    return number

        return None

    # =========================================================
    # Weekday
    # =========================================================

    def _get_weekday(
        self,
        value,
    ):
        if value is None:
            return None

        if isinstance(
            value,
            int,
        ):
            return (
                value
                if 0 <= value <= 6
                else None
            )

        if isinstance(
            value,
            str,
        ):
            value = value.strip().upper()

            if value in self.WEEKDAYS:
                return self.WEEKDAYS[value]

            if value.isdigit():
                number = int(value)

                if 0 <= number <= 6:
                    return number

        return None

    # =========================================================
    # Conversion
    # =========================================================

    @staticmethod
    def _to_positive_int(value):
        try:
            value = int(value)

            if value > 0:
                return value

        except (
            TypeError,
            ValueError,
        ):
            pass

        return None

    @staticmethod
    def _to_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(
            value,
            date,
        ):
            return value

        if isinstance(
            value,
            str,
        ):
            return date.fromisoformat(
                value
            )

        raise TypeError(
            f"Unsupported date value: {value!r}"
        )
