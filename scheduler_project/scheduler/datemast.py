from datetime import date, datetime
import json
import os


class DateMast:
    """
    DATEMAST report-date provider.

    The scheduler can load DATEMAST report dates from a JSON file.
    The existing in-memory behavior is retained so the class can
    still be used easily in tests.

    DATEMAST is NOT responsible for:
        - calculating holidays
        - calculating weekends
        - calculating T+N
        - determining job frequency
        - determining job eligibility

    Those responsibilities belong to other scheduler components.
    """

    def __init__(
        self,
        report_dates=None,
        file_path=None,
        today_provider=None,
        coverage_start=None,
        coverage_end=None,
    ):
        """
        Args:
            report_dates:
                Optional iterable of report dates. Useful for tests.

            file_path:
                Optional path to datemaster.json. If supplied, the
                file is loaded during initialization and automatically
                rechecked when DATEMAST is accessed.
        """

        self.file_path = (
            os.fspath(file_path)
            if file_path is not None
            else None
        )

        self._report_dates = set()
        self._file_signature = None
        self.today_provider = today_provider or date.today
        self.coverage_start = None
        self.coverage_end = None
        self.available = True
        self._file_available = True

        self.replace_report_dates(
            report_dates if report_dates is not None else [],
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        )

        if self.file_path:
            self.reload(force=True)

    @property
    def available(self):
        return self._upstream_available and self._file_available

    @available.setter
    def available(self, value):
        self._upstream_available = bool(value)

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
        """
        Remove a report date from the available DATEMAST dates.
        """

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

        Sorted oldest -> newest.
        """

        self.reload()

        return sorted(
            self._report_dates
        )

    # =========================================================
    # Latest report date
    # =========================================================

    def covers_date(self, current_date):
        """Check loaded coverage; publication still requires the real T-1 cutoff.

        Legacy snapshots without bounds retain their historical behavior.
        Coverage alone cannot establish holidays while the feed is unavailable.
        """
        self.reload()
        day = self._to_date(current_date)
        if self.coverage_start is None:
            return True
        return self.coverage_start <= day <= min(
            self.coverage_end, self._to_date(self.today_provider()),
        )

    def get_latest_report_date(self):
        """
        Return the published T-1 watermark, excluding today's/future seeds.
        """

        values = self.get_published_report_dates()
        return max(values) if values else None

    def get_published_report_dates(self):
        """Raw source dates stay inspectable; only dates before today are ready."""
        cutoff = self._to_date(self.today_provider())
        return [value for value in self.get_report_dates() if value < cutoff]

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

        self.reload()

        report_date = self._to_date(
            report_date
        )

        return (
            report_date
            in self._report_dates
            and report_date < self._to_date(self.today_provider())
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

        This does NOT calculate the report date itself.
        It only selects from the report dates supplied by
        DATEMAST.
        """

        self.reload()

        current_date = self._to_date(
            current_date
        )

        candidates = [
            report_date
            for report_date in self.get_published_report_dates()
            if report_date <= current_date
        ]

        if not candidates:
            return None

        return max(
            candidates
        )

    # =========================================================
    # Previous report date
    # =========================================================

    def get_previous_report_date(
        self,
        current_date,
    ):
        """Return the latest DATEMAST report date *before* ``current_date``.

        The distinction from :meth:`get_report_date_for` is deliberate.  The
        legacy Oracle scheduler uses ``MAX(report_date) < run_date`` for a
        normal DAILY occurrence, so a daily run on 14-Aug receives the
        previous business/report date (for example 13-Aug), never 14-Aug
        itself.  Keeping the strict comparison here makes that rule explicit
        and avoids callers accidentally reimplementing it as ``<=``.
        """

        self.reload()

        current_date = self._to_date(
            current_date
        )

        candidates = [
            report_date
            for report_date in self.get_published_report_dates()
            if report_date < current_date
        ]

        if not candidates:
            return None

        return max(
            candidates
        )

    # =========================================================
    # Reload JSON file
    # =========================================================

    def reload(
        self,
        force=False,
    ):
        """
        Reload DATEMAST when datemaster.json has changed.

        Returns:
            True  -> file was successfully loaded
            False -> no reload was needed or the file could not
                     be loaded successfully

        If the file is temporarily missing or contains invalid
        JSON, the last successfully loaded DATEMAST is retained.
        """

        if not self.file_path:
            return False

        try:
            signature = self._get_file_signature()
        except OSError:
            self._file_available = False
            return False

        if (
            not force
            and signature == self._file_signature
            and self._file_available
        ):
            return False

        try:
            with open(
                self.file_path,
                "r",
                encoding="utf-8",
            ) as file:
                data = json.load(file)

            report_dates = self._extract_report_dates(
                data
            )

            parsed_dates = set()

            for report_date in report_dates:
                parsed_dates.add(
                    self._to_date(
                        report_date
                    )
                )

            coverage_start, coverage_end = self._parse_coverage(
                data.get("coverage_start", data.get("COVERAGE_START")) if isinstance(data, dict) else None,
                data.get("coverage_end", data.get("COVERAGE_END")) if isinstance(data, dict) else None,
            )

        except (
            OSError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            # Do not destroy the last known-good data because
            # the upstream process may be replacing the file.
            self._file_available = False
            return False

        self._report_dates = parsed_dates
        self.coverage_start = coverage_start
        self.coverage_end = coverage_end
        self._file_signature = signature
        self._file_available = True

        return True

    # =========================================================
    # Replace report dates
    # =========================================================

    def replace_report_dates(
        self,
        report_dates,
        coverage_start=None,
        coverage_end=None,
    ):
        """
        Replace dates and coverage together after validation.

        Omitting both bounds explicitly restores legacy unbounded behavior.
        """

        parsed_dates = {self._to_date(value) for value in report_dates}
        start, end = self._parse_coverage(coverage_start, coverage_end)
        self._report_dates = parsed_dates
        self.coverage_start = start
        self.coverage_end = end
        self.available = True
        self._file_available = True

    # =========================================================
    # JSON parsing
    # =========================================================

    def _parse_coverage(self, start, end):
        if start is None and end is None:
            return None, None
        if start is None or end is None:
            raise ValueError("DATEMAST coverage requires both start and end dates.")
        start, end = self._to_date(start), self._to_date(end)
        if start > end:
            raise ValueError("DATEMAST coverage start must not follow its end.")
        # A snapshot cannot verify future absences even if supplied a wider
        # upper bound. Keep the read-day limit fixed as the clock advances.
        end = min(end, self._to_date(self.today_provider()))
        if start > end:
            raise ValueError("DATEMAST coverage cannot begin after the current date.")
        return start, end

    @classmethod
    def _extract_report_dates(
        cls,
        data,
    ):
        """
        Extract report dates from common JSON structures.

        Supported examples:

            [
                "2026-09-05",
                "2026-09-06",
                "2026-09-07"
            ]

        or:

            [
                "05-09-2026",
                "06-09-2026",
                "07-09-2026"
            ]

        or:

            {
                "report_dates": [
                    "2026-09-05",
                    "2026-09-06"
                ]
            }

        or:

            [
                {"REPORT_DATE": "2026-09-05"},
                {"REPORT_DATE": "2026-09-06"}
            ]
        """

        if isinstance(
            data,
            list,
        ):
            result = []

            for item in data:
                if isinstance(
                    item,
                    dict,
                ):
                    value = item.get(
                        "REPORT_DATE"
                    )

                    if value is None:
                        value = item.get(
                            "report_date"
                        )

                    if value is None:
                        value = item.get(
                            "DATE"
                        )

                    if value is None:
                        value = item.get(
                            "date"
                        )

                    if value is not None:
                        result.append(
                            value
                        )
                else:
                    result.append(
                        item
                    )

            return result

        if isinstance(
            data,
            dict,
        ):
            for key in (
                "REPORT_DATES",
                "report_dates",
                "DATEMASTER",
                "datemaster",
                "DATEMAST",
                "datemast",
                "DATES",
                "dates",
            ):
                if key in data:
                    return cls._extract_report_dates(
                        data[key]
                    )

            for key in (
                "REPORT_DATE",
                "report_date",
                "DATE",
                "date",
            ):
                if key in data:
                    return [
                        data[key]
                    ]

        raise ValueError(
            "Unsupported datemaster.json structure."
        )

    # =========================================================
    # File signature
    # =========================================================

    def _get_file_signature(self):
        """
        Return the current file signature.

        Both modification time and file size are used so that
        changes to datemaster.json can be detected efficiently.
        """

        stat = os.stat(
            self.file_path
        )

        return (
            stat.st_mtime_ns,
            stat.st_size,
        )

    # =========================================================
    # Date conversion
    # =========================================================

    @staticmethod
    def _to_date(value):
        """
        Convert supported values to datetime.date.

        Supported string formats:

            YYYY-MM-DD
            DD-MM-YYYY

        Supported datetime strings include ISO datetime
        strings such as:

            2026-09-09T10:30:00

        Supported Python values:

            date
            datetime
        """

        if value is None:
            raise ValueError(
                "report_date cannot be None."
            )

        # -----------------------------------------------------
        # datetime
        # -----------------------------------------------------

        if isinstance(
            value,
            datetime,
        ):
            return value.date()

        # -----------------------------------------------------
        # date
        # -----------------------------------------------------

        if isinstance(
            value,
            date,
        ):
            return value

        # -----------------------------------------------------
        # string
        # -----------------------------------------------------

        if isinstance(
            value,
            str,
        ):
            value = value.strip()

            if not value:
                raise ValueError(
                    "report_date cannot be empty."
                )

            # -------------------------------------------------
            # ISO date
            # -------------------------------------------------

            try:
                return date.fromisoformat(
                    value
                )
            except ValueError:
                pass

            # -------------------------------------------------
            # ISO datetime
            # -------------------------------------------------

            try:
                return datetime.fromisoformat(
                    value
                ).date()
            except ValueError:
                pass

            # -------------------------------------------------
            # DD-MM-YYYY
            #
            # Example:
            #     09-09-2026
            # -------------------------------------------------

            try:
                return datetime.strptime(
                    value,
                    "%d-%m-%Y",
                ).date()
            except ValueError as exc:
                raise TypeError(
                    f"Unsupported date value: {value!r}"
                ) from exc

        # -----------------------------------------------------
        # Unsupported type
        # -----------------------------------------------------

        raise TypeError(
            f"Unsupported date value: {value!r}"
        )
