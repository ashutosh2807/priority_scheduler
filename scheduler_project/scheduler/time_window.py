from datetime import datetime, time, timedelta
from datetime_compat import parse_iso_datetime


class TimeWindowEvaluator:
    """
    Evaluates a job's RUN_BY time window.

    RUN_CONFIG example:

        {
            "RUN_BY": {
                "FROM_TIME": "06:00",
                "TO_TIME": "06:30"
            }
        }

    The job window is independent of the scheduler's global
    working hours.

    Global scheduler hours:
        10:00 - 20:00

    A job may have a RUN_BY window outside those hours.
    """

    NO_TIME_RESTRICTION = "NO_TIME_RESTRICTION"
    WAITING_FOR_TIME_WINDOW = "WAITING_FOR_TIME_WINDOW"
    TIME_WINDOW_ACTIVE = "TIME_WINDOW_ACTIVE"
    TIME_WINDOW_EXPIRED = "TIME_WINDOW_EXPIRED"

    def __init__(
        self,
        work_start_time="10:00",
        work_end_time="20:00",
    ):
        self.work_start_time = self._parse_time(
            work_start_time
        )

        self.work_end_time = self._parse_time(
            work_end_time
        )

    # =========================================================
    # TIME_FLAG
    # =========================================================

    def is_time_flag_enabled(self, job):
        """
        Return True when Schedule Master TIME_FLAG is enabled.
        """

        value = getattr(
            job,
            "time_flag",
            0,
        )

        if isinstance(value, str):
            return value.strip().upper() in {
                "1",
                "Y",
                "YES",
                "TRUE",
                "T",
            }

        return bool(value)

    # =========================================================
    # RUN_BY
    # =========================================================

    def get_run_by(self, job):
        """
        Extract RUN_BY FROM_TIME / TO_TIME.

        Returns:

            (from_time, to_time)

        or:

            (None, None)
        """

        if not self.is_time_flag_enabled(job):
            return None, None

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

        from_value = run_by.get(
            "FROM_TIME"
        )

        to_value = run_by.get(
            "TO_TIME"
        )

        if from_value is None or to_value is None:
            return None, None

        return (
            self._parse_time(from_value),
            self._parse_time(to_value),
        )

    # =========================================================
    # Current job window state
    # =========================================================

    def get_job_time_state(
        self,
        job,
        current_datetime,
    ):
        """
        Return the current state of the job's RUN_BY window.
        """

        from_time, to_time = self.get_run_by(
            job
        )

        return self.get_time_state(
            from_time=from_time,
            to_time=to_time,
            current_datetime=current_datetime,
        )

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
        Determine whether the current time is:

            NO_TIME_RESTRICTION
            WAITING_FOR_TIME_WINDOW
            TIME_WINDOW_ACTIVE
            TIME_WINDOW_EXPIRED

        Window semantics:

            FROM <= current < TO
        """

        current_datetime = self._to_datetime(
            current_datetime
        )

        if from_time is None or to_time is None:
            return self.NO_TIME_RESTRICTION

        current_time = (
            current_datetime.time()
        )

        # -----------------------------------------------------
        # Normal same-day window
        #
        # Example:
        #     06:00 -> 06:30
        # -----------------------------------------------------

        if from_time < to_time:

            if current_time < from_time:
                return self.WAITING_FOR_TIME_WINDOW

            if current_time < to_time:
                return self.TIME_WINDOW_ACTIVE

            return self.TIME_WINDOW_EXPIRED

        # -----------------------------------------------------
        # Overnight window
        #
        # Example:
        #     23:00 -> 02:00
        # -----------------------------------------------------

        if from_time > to_time:

            if (
                current_time >= from_time
                or current_time < to_time
            ):
                return self.TIME_WINDOW_ACTIVE

            return self.WAITING_FOR_TIME_WINDOW

        # -----------------------------------------------------
        # FROM == TO
        # -----------------------------------------------------
        #
        # We treat this as an invalid/expired window rather
        # than a 24-hour window.
        # -----------------------------------------------------

        return self.TIME_WINDOW_EXPIRED

    # =========================================================
    # Is currently within job window?
    # =========================================================

    def is_within_job_window(
        self,
        job,
        current_datetime,
    ):
        state = self.get_job_time_state(
            job,
            current_datetime,
        )

        return state in {
            self.NO_TIME_RESTRICTION,
            self.TIME_WINDOW_ACTIVE,
        }

    # =========================================================
    # Global scheduler working hours
    # =========================================================

    def is_working_hour(
        self,
        current_datetime,
    ):
        """
        Check the scheduler's normal working period.

        Default:

            10:00 <= time < 20:00
        """

        current_datetime = self._to_datetime(
            current_datetime
        )

        current_time = (
            current_datetime.time()
        )

        return self._is_time_between(
            current_time,
            self.work_start_time,
            self.work_end_time,
        )

    def is_non_working_hour(
        self,
        current_datetime,
    ):
        return not self.is_working_hour(
            current_datetime
        )

    # =========================================================
    # Global working period
    # =========================================================

    def get_period(
        self,
        current_datetime,
    ):
        """
        Return:

            WORKING_HOURS
            NON_WORKING_HOURS
        """

        if self.is_working_hour(
            current_datetime
        ):
            return "WORKING_HOURS"

        return "NON_WORKING_HOURS"

    # =========================================================
    # Next job window
    # =========================================================

    def get_next_window_start(
        self,
        job,
        current_datetime,
    ):
        """
        Return the next occurrence of the job's RUN_BY start.

        Returns None when there is no time restriction.

        Handles both normal and overnight windows.
        """

        current_datetime = self._to_datetime(
            current_datetime
        )

        from_time, to_time = self.get_run_by(
            job
        )

        if from_time is None:
            return None

        candidate = current_datetime.replace(
            hour=from_time.hour,
            minute=from_time.minute,
            second=0,
            microsecond=0,
        )

        # -----------------------------------------------------
        # If today's start has not happened yet, use today.
        # -----------------------------------------------------

        if candidate > current_datetime:
            return candidate

        # -----------------------------------------------------
        # If we're already inside the current window, return
        # the current window start.
        # -----------------------------------------------------

        state = self.get_time_state(
            from_time=from_time,
            to_time=to_time,
            current_datetime=current_datetime,
        )

        if state == self.TIME_WINDOW_ACTIVE:
            return candidate

        # -----------------------------------------------------
        # Current window has expired.
        # Move to tomorrow.
        # -----------------------------------------------------

        return candidate + timedelta(
            days=1
        )

    # =========================================================
    # Next global working period
    # =========================================================

    def get_next_working_hour_start(
        self,
        current_datetime,
    ):
        """
        Return the next global scheduler working-hour start.

        Example:

            current = 08:30
            result  = 10:00

            current = 15:00
            result  = 15:00

            current = 21:00
            result  = next day's 10:00
        """

        current_datetime = self._to_datetime(
            current_datetime
        )

        if self.is_working_hour(
            current_datetime
        ):
            return current_datetime

        candidate = current_datetime.replace(
            hour=self.work_start_time.hour,
            minute=self.work_start_time.minute,
            second=0,
            microsecond=0,
        )

        if candidate > current_datetime:
            return candidate

        return candidate + timedelta(
            days=1
        )

    # =========================================================
    # Helper
    # =========================================================

    @staticmethod
    def _is_time_between(
        current_time,
        start_time,
        end_time,
    ):
        """
        Half-open interval:

            start <= current < end
        """

        if start_time < end_time:

            return (
                start_time
                <= current_time
                < end_time
            )

        if start_time > end_time:

            # Overnight global window.
            return (
                current_time >= start_time
                or current_time < end_time
            )

        return False

    @staticmethod
    def _parse_time(value):
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

            for fmt in (
                "%H:%M",
                "%H:%M:%S",
            ):
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
    def _to_datetime(value):
        if isinstance(
            value,
            datetime,
        ):
            return value

        if isinstance(
            value,
            str,
        ):
            return parse_iso_datetime(
                value
            )

        raise TypeError(
            "current_datetime must be datetime "
            "or ISO datetime string."
        )