from dataclasses import dataclass
from datetime import date, datetime, time
from datetime_compat import parse_iso_datetime


@dataclass(frozen=True)
class PriorityResult:
    """
    Result produced by PriorityCalculator.

    This gives the rest of the scheduler a consistent object
    interface instead of returning a dictionary.
    """

    date_priority: int
    time_priority: int
    job_priority: int
    priority_key: tuple

    time_state: str
    from_time: object = None
    to_time: object = None


class PriorityCalculator:
    """
    Calculates the scheduling priority of a READY job.

    Priority concept:

        1. Older pending report dates get higher priority.
        2. A currently active RUN_BY window gets higher priority.
        3. A future RUN_BY window is lower priority.
        4. A job whose RUN_BY window has expired must not
           permanently dominate newer/current work.
        5. Normal job priority is used as a tie-breaker.
        6. Job ID provides deterministic ordering.

    IMPORTANT:

        This class calculates priority only.

        It does NOT decide whether a job is eligible to run.

        EligibilityEvaluator is responsible for:

            frequency
            calendar
            DATEMAST
            confirmation
            RUN_BY eligibility
            READY/STAGING state

    Lower numeric values mean higher priority.
    """

    # =========================================================
    # Time priority constants
    # =========================================================

    TIME_ACTIVE = 0
    TIME_FUTURE = 10
    TIME_NONE = 20
    TIME_EXPIRED = 100

    # ---------------------------------------------------------
    # Expired jobs receive an effective date-priority penalty.
    #
    # This prevents:
    #
    #     old report date + expired window
    #
    # from permanently dominating:
    #
    #     newer report date + active window
    #
    # The actual report-date priority is still retained in
    # PriorityResult.date_priority for monitoring/debugging.
    # ---------------------------------------------------------

    EXPIRED_DATE_PENALTY = 1_000_000

    # =========================================================
    # Calculate
    # =========================================================

    def calculate(
        self,
        job,
        report_date,
        current_datetime,
    ):
        """
        Calculate priority for a READY job.

        Parameters
        ----------
        job:
            ScheduleMaster object.

        report_date:
            Report date associated with the READY occurrence.

        current_datetime:
            Current scheduler datetime.

        Returns
        -------
        PriorityResult
        """

        current_datetime = self._to_datetime(
            current_datetime
        )

        report_date = self._to_date(
            report_date
        )

        # -----------------------------------------------------
        # 1. Base report-date priority
        # -----------------------------------------------------

        date_priority = (
            self._calculate_date_priority(
                report_date=report_date,
                current_date=current_datetime.date(),
            )
        )

        # -----------------------------------------------------
        # 2. RUN_BY time-window state
        # -----------------------------------------------------

        from_time, to_time = (
            self._get_run_by(
                job
            )
        )

        time_state = (
            self.get_time_state(
                from_time=from_time,
                to_time=to_time,
                current_datetime=current_datetime,
            )
        )

        time_priority = (
            self._time_priority(
                time_state
            )
        )

        # -----------------------------------------------------
        # 3. Normal job priority
        # -----------------------------------------------------

        job_priority = (
            self._get_job_priority(
                job
            )
        )

        # -----------------------------------------------------
        # 4. Effective date priority
        # -----------------------------------------------------
        #
        # Normally:
        #
        #     older report date wins.
        #
        # But an expired RUN_BY slot should not permanently
        # dominate newer active work.
        #
        # Therefore an expired READY job receives a large
        # effective priority penalty.
        #
        # The original date_priority remains available in the
        # PriorityResult for monitoring.
        # -----------------------------------------------------

        effective_date_priority = (
            date_priority
        )

        if (
            time_state
            == "TIME_WINDOW_EXPIRED"
        ):
            effective_date_priority = (
                date_priority
                + self.EXPIRED_DATE_PENALTY
            )

        # -----------------------------------------------------
        # 5. Final heap key
        # -----------------------------------------------------
        #
        # Lower tuple values are popped first.
        #
        # Normal case:
        #
        #     older report date
        #         ↓
        #     time state
        #         ↓
        #     job priority
        #         ↓
        #     job ID
        #
        # Expired time-window jobs receive the effective date
        # penalty described above.
        # -----------------------------------------------------

        priority_key = (
            effective_date_priority,
            time_priority,
            job_priority,
            int(job.id),
        )

        return PriorityResult(
            date_priority=date_priority,
            time_priority=time_priority,
            job_priority=job_priority,
            priority_key=priority_key,
            time_state=time_state,
            from_time=from_time,
            to_time=to_time,
        )

    # =========================================================
    # Date priority
    # =========================================================

    def _calculate_date_priority(
        self,
        report_date,
        current_date,
    ):
        """
        Calculate report-date priority.

        Older report dates receive higher priority.

        Example:

            current date = 2026-09-08

            report_date = 2026-09-07
            report_date = 2026-09-06

        06-Sep is older and therefore receives the higher
        priority.

        Because heapq selects the smallest value first, age is
        represented as a negative number.

        Example:

            age = 1  -> -1
            age = 2  -> -2

        Therefore:

            -2 < -1

        and the older report date wins.
        """

        if report_date is None:
            return 0

        age = (
            current_date - report_date
        ).days

        return -age

    # =========================================================
    # RUN_BY extraction
    # =========================================================

    def _get_run_by(
        self,
        job,
    ):
        """
        Extract RUN_BY.FROM_TIME and RUN_BY.TO_TIME from
        RUN_CONFIG.

        Returns:

            (from_time, to_time)

        or:

            (None, None)
        """

        run_config = getattr(
            job,
            "run_config",
            None,
        )

        if not isinstance(
            run_config,
            dict,
        ):
            return None, None

        run_by = run_config.get(
            "RUN_BY"
        )

        if not isinstance(
            run_by,
            dict,
        ):
            return None, None

        from_value = (
            run_by.get(
                "FROM_TIME"
            )
        )

        to_value = (
            run_by.get(
                "TO_TIME"
            )
        )

        from_time = (
            self._parse_time(
                from_value
            )
        )

        to_time = (
            self._parse_time(
                to_value
            )
        )

        return from_time, to_time

    # =========================================================
    # Time state
    # =========================================================

    def get_time_state(
        self,
        from_time,
        to_time,
        current_datetime,
    ):
        """
        Determine the current state of a job's RUN_BY window.

        States:

            NO_TIME_RESTRICTION
            WAITING_FOR_TIME_WINDOW
            TIME_WINDOW_ACTIVE
            TIME_WINDOW_EXPIRED

        Normal windows are half-open:

            FROM <= current < TO

        Example:

            10:00 -> 11:00

            09:59  WAITING
            10:00  ACTIVE
            10:30  ACTIVE
            10:59  ACTIVE
            11:00  EXPIRED
        """

        current_datetime = (
            self._to_datetime(
                current_datetime
            )
        )

        current_time = (
            current_datetime.time()
        )

        # -----------------------------------------------------
        # No RUN_BY restriction
        # -----------------------------------------------------

        if (
            from_time is None
            or to_time is None
        ):
            return "NO_TIME_RESTRICTION"

        from_time = self._parse_time(
            from_time
        )

        to_time = self._parse_time(
            to_time
        )

        # -----------------------------------------------------
        # Normal same-day window
        #
        # Example:
        #
        #     06:00 -> 06:30
        # -----------------------------------------------------

        if from_time < to_time:

            if current_time < from_time:
                return (
                    "WAITING_FOR_TIME_WINDOW"
                )

            if current_time < to_time:
                return (
                    "TIME_WINDOW_ACTIVE"
                )

            return "TIME_WINDOW_EXPIRED"

        # -----------------------------------------------------
        # Overnight window
        #
        # Example:
        #
        #     23:00 -> 02:00
        # -----------------------------------------------------

        if from_time > to_time:

            if (
                current_time >= from_time
                or current_time < to_time
            ):
                return (
                    "TIME_WINDOW_ACTIVE"
                )

            return (
                "WAITING_FOR_TIME_WINDOW"
            )

        # -----------------------------------------------------
        # Equal start/end
        #
        # We treat this as an expired/invalid window rather
        # than an unrestricted window.
        # -----------------------------------------------------

        return "TIME_WINDOW_EXPIRED"

    # =========================================================
    # Time priority
    # =========================================================

    def _time_priority(
        self,
        time_state,
    ):
        """
        Convert time-window state into a numeric priority.

        Lower number = higher priority.
        """

        if (
            time_state
            == "TIME_WINDOW_ACTIVE"
        ):
            return self.TIME_ACTIVE

        if (
            time_state
            == "WAITING_FOR_TIME_WINDOW"
        ):
            return self.TIME_FUTURE

        if (
            time_state
            == "NO_TIME_RESTRICTION"
        ):
            return self.TIME_NONE

        return self.TIME_EXPIRED

    # =========================================================
    # Job priority
    # =========================================================

    def _get_job_priority(
        self,
        job,
    ):
        """
        Get normal job priority.

        There is currently no dedicated priority column in
        ScheduleMaster.

        Therefore 0 is used as the default tie-breaker.

        This method exists so a real business priority can be
        introduced later without changing the heap architecture.
        """

        value = getattr(
            job,
            "priority",
            0,
        )

        if value is None:
            return 0

        try:
            return int(
                value
            )

        except (
            TypeError,
            ValueError,
        ):
            return 0

    # =========================================================
    # Public sort key
    # =========================================================

    def get_sort_key(
        self,
        job,
        report_date,
        current_datetime,
    ):
        """
        Return the complete heap sort key.
        """

        result = self.calculate(
            job=job,
            report_date=report_date,
            current_datetime=current_datetime,
        )

        return result.priority_key

    # =========================================================
    # Helpers
    # =========================================================

    @staticmethod
    def _parse_time(
        value,
    ):
        """
        Convert a supported time representation into datetime.time.
        """

        if value is None:
            return None

        if isinstance(
            value,
            time,
        ):
            return value

        if isinstance(
            value,
            datetime,
        ):
            return value.time()

        if isinstance(
            value,
            str,
        ):

            value = value.strip()

            if not value:
                return None

            formats = (
                "%H:%M",
                "%H:%M:%S",
            )

            for fmt in formats:

                try:
                    return datetime.strptime(
                        value,
                        fmt,
                    ).time()

                except ValueError:
                    continue

        raise ValueError(
            f"Invalid time value: {value!r}"
        )

    @staticmethod
    def _to_date(
        value,
    ):
        """
        Convert supported date values into datetime.date.
        """

        if value is None:
            return None

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

            value = value.strip()

            if not value:
                return None

            # Plain ISO date.
            try:
                return date.fromisoformat(
                    value
                )

            except ValueError:
                pass

            # ISO datetime.
            try:
                return datetime.fromisoformat(
                    value
                ).date()

            except ValueError as exc:
                raise TypeError(
                    f"Unsupported date value: {value!r}"
                ) from exc

        raise TypeError(
            f"Unsupported date value: {value!r}"
        )

    @staticmethod
    def _to_datetime(
        value,
    ):
        """
        Convert supported values into datetime.datetime.
        """

        if isinstance(
            value,
            datetime,
        ):
            return value

        if isinstance(
            value,
            str,
        ):

            value = value.strip()

            if not value:
                raise TypeError(
                    "current_datetime cannot be empty."
                )

            try:
                return parse_iso_datetime(
                    value
                )

            except ValueError as exc:
                raise TypeError(
                    "current_datetime must be datetime "
                    "or ISO datetime string."
                ) from exc

        raise TypeError(
            "current_datetime must be datetime "
            "or ISO datetime string."
        )