from datetime import date, datetime


class DateMast:
    """
    DATEMAST report-date provider.

    Purpose:
        Provide the authoritative report dates available to
        the scheduler.

    DATEMAST is NOT responsible for:
        - calculating holidays
        - calculating weekends
        - calculating T+N
        - determining job frequency
        - determining job eligibility

    Those responsibilities belong to other scheduler
    components.
    """

    def __init__(
        self,
        report_dates=None,
    ):
        """
        report_dates:
            Optional iterable of report dates.

        This in-memory implementation is mainly useful for
        development/testing.

        Later this class can be backed by Oracle DATEMAST.
        """

        self._report_dates = set()

        if report_dates:
            for report_date in report_dates:
                self.add_report_date(
                    report_date
                )

    # =========================================================
    # Add report date
    # =========================================================

    def add_report_date(self, report_date):
        """
        Add a report date to the available DATEMAST dates.
        """

        report_date = self._to_date(
            report_date
        )

        self._report_dates.add(
            report_date
        )

    # =========================================================
    # Remove report date
    # =========================================================

    def remove_report_date(self, report_date):
        report_date = self._to_date(
            report_date
        )

        self._report_dates.discard(
            report_date
        )

    # =========================================================
    # All report dates
    # =========================================================

    def get_report_dates(self):
        """
        Return all known DATEMAST report dates.

        Sorted oldest → newest.
        """

        return sorted(
            self._report_dates
        )

    # =========================================================
    # Latest report date
    # =========================================================

    def get_latest_report_date(self):
        """
        Return the latest report_date available in DATEMAST.

        Returns:
            date | None
        """

        if not self._report_dates:
            return None

        return max(
            self._report_dates
        )

    # =========================================================
    # Exact report-date availability
    # =========================================================

    def has_report_date(
        self,
        report_date,
    ):
        """
        Check whether an exact report_date exists in DATEMAST.
        """

        report_date = self._to_date(
            report_date
        )

        return (
            report_date
            in self._report_dates
        )

    # =========================================================
    # Required report date
    # =========================================================

    def is_ready_for(
        self,
        required_report_date,
    ):
        """
        Return True only when the exact required report date
        is available.

        This is intentionally an exact comparison.

        Example:

            required = 2026-09-03

            DATEMAST:
                2026-09-02
                2026-09-03

            → True
        """

        return self.has_report_date(
            required_report_date
        )

    # =========================================================
    # Latest >= required
    # =========================================================

    def latest_is_at_least(
        self,
        required_report_date,
    ):
        """
        Check whether the latest DATEMAST report date is at
        least the required report date.

        This is useful when the business rule is based on
        cumulative availability rather than exact availability.
        """

        required_report_date = self._to_date(
            required_report_date
        )

        latest = (
            self.get_latest_report_date()
        )

        if latest is None:
            return False

        return (
            latest >= required_report_date
        )

    # =========================================================
    # Get latest report date before/equal to a date
    # =========================================================

    def get_report_date_for(
        self,
        current_date,
    ):
        """
        Return the latest DATEMAST report_date that is
        less than or equal to current_date.

        This is useful for inspecting the DATEMAST state
        relative to a scheduler date.

        It does NOT calculate the report date itself.
        """

        current_date = self._to_date(
            current_date
        )

        candidates = [
            report_date
            for report_date
            in self._report_dates
            if report_date <= current_date
        ]

        if not candidates:
            return None

        return max(
            candidates
        )

    # =========================================================
    # Refresh
    # =========================================================

    def replace_report_dates(
        self,
        report_dates,
    ):
        """
        Replace the in-memory DATEMAST data.
        """

        self._report_dates.clear()

        for report_date in report_dates:
            self.add_report_date(
                report_date
            )

    # =========================================================
    # Date conversion
    # =========================================================

    @staticmethod
    def _to_date(value):
        if value is None:
            raise ValueError(
                "report_date cannot be None."
            )

        if isinstance(
            value,
            datetime,
        ):
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
            f"Unsupported date value: {value}"
        )