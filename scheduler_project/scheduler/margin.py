from datetime import date, datetime, timedelta
import re
from scheduler.holiday import HolidayEvaluator


class MarginCalculator:
    """
    Calculates T+n dates.

    Important:
        T+n is a CALENDAR-DAY offset.

        T must come from the authoritative DATEMAST/source context
        supplied by the caller.

    Examples:

        T       -> T date itself
        T+0     -> T date itself
        T+1     -> calendar day after T
        T+2     -> two calendar days after T
        T+3     -> three calendar days after T

    IMPORTANT:
        Margin calculation is deliberately separate from
        next-working-day execution.

        For example:

            T = 2026-09-15
            T+3 = 2026-09-18

        even if 2026-09-16 is a holiday.

        If SAME_DAY=0, the scheduler separately calculates the
        execution date as the next working day after the occurrence.
    """

    MARGIN_PATTERN = re.compile(
        r"^T(?:\+(\d+))?$",
        re.IGNORECASE,
    )

    def __init__(self, working_day_checker=None):
        """
        working_day_checker:
            Callable receiving a date and returning True when that
            date is a working day.

        It is used ONLY by working-day helper methods such as
        next_working_day() and previous_working_day().

        It is NOT used by calculate(), because T+n is a
        calendar-day offset.
        """

        self.working_day_checker = (
            working_day_checker
            or self._default_working_day
        )

    # =========================================================
    # Parse margin
    # =========================================================

    @classmethod
    def parse(cls, margin):
        """
        Convert a margin string into an integer calendar-day offset.

        Examples:

            T       -> 0
            T+0     -> 0
            T+1     -> 1
            T+3     -> 3

        Returns:
            int

        Raises:
            ValueError for invalid margins.
        """

        if margin is None:
            return 0

        if isinstance(margin, int):
            if margin < 0:
                raise ValueError(
                    f"Margin cannot be negative: {margin}"
                )

            return margin

        margin = str(margin).strip()

        match = cls.MARGIN_PATTERN.fullmatch(margin)

        if not match:
            raise ValueError(
                f"Invalid margin: {margin!r}. "
                f"Expected T, T+1, T+2, ..."
            )

        value = match.group(1)

        if value is None:
            return 0

        return int(value)

    # =========================================================
    # Calculate target date
    # =========================================================

    def calculate(
        self,
        start_date,
        margin,
    ):
        """
        Calculate the target date using calendar-day arithmetic.

        Examples:

            T = 2026-09-15
            T+1 = 2026-09-16
            T+2 = 2026-09-17
            T+3 = 2026-09-18

        Weekends and holidays do not change the result.

        This method must not be confused with next_working_day().
        """

        start_date = self._to_date(start_date)

        offset = self.parse(margin)

        return start_date + timedelta(
            days=offset
        )

    # =========================================================
    # Working day
    # =========================================================

    def is_working_day(self, current_date):
        """
        Determine whether a date is a working day.

        The actual decision is delegated to the injected
        working_day_checker.
        """

        current_date = self._to_date(current_date)

        return bool(
            self.working_day_checker(
                current_date
            )
        )

    # =========================================================
    # Previous working day
    # =========================================================

    def previous_working_day(self, current_date):
        """
        Return the previous working day.

        The supplied date itself is NOT considered.
        """

        current_date = self._to_date(current_date)

        # With authoritative absence inference, no date before the earliest
        # published working date can match. Avoid an unbounded backward scan.
        evaluator = getattr(self.working_day_checker, "__self__", None)
        if isinstance(evaluator, HolidayEvaluator):
            published = evaluator.published_dates()
            if published and current_date <= min(published):
                raise ValueError("No previous working date is available in DATEMAST.")

        current_date -= timedelta(days=1)

        while not self.is_working_day(current_date):
            current_date -= timedelta(days=1)

        return current_date

    # =========================================================
    # Next working day
    # =========================================================

    def next_working_day(self, current_date):
        """
        Return the next working day.

        The supplied date itself is NOT considered.

        This is the method used for the universal SAME_DAY=0
        execution-date rule.
        """

        current_date = self._to_date(current_date)

        current_date += timedelta(days=1)

        while not self.is_working_day(current_date):
            current_date += timedelta(days=1)

        return current_date

    # =========================================================
    # Convenience methods
    # =========================================================

    def add_working_days(
        self,
        start_date,
        number_of_days,
    ):
        """
        Add working days to a date.

        This is intentionally different from calculate().

        Example:

            add_working_days(T, 3)

        means three working days after T.

        calculate(T, "T+3") means three calendar days after T.
        """

        start_date = self._to_date(start_date)

        if not isinstance(number_of_days, int):
            raise TypeError(
                "number_of_days must be an integer"
            )

        if number_of_days < 0:
            raise ValueError(
                "number_of_days cannot be negative"
            )

        current_date = start_date

        for _ in range(number_of_days):
            current_date = self.next_working_day(
                current_date
            )

        return current_date

    # =========================================================
    # Margin conversion
    # =========================================================

    @classmethod
    def to_offset(cls, margin):
        """
        Convert:

            T       -> 0
            T+1     -> 1
            T+5     -> 5
        """

        return cls.parse(margin)

    @classmethod
    def to_margin(cls, offset):
        """
        Convert:

            0 -> T
            1 -> T+1
            5 -> T+5
        """

        if not isinstance(offset, int):
            raise TypeError(
                "Offset must be an integer"
            )

        if offset < 0:
            raise ValueError(
                "Offset cannot be negative"
            )

        if offset == 0:
            return "T"

        return f"T+{offset}"

    # =========================================================
    # Default working-day implementation
    # =========================================================

    @staticmethod
    def _default_working_day(current_date):
        """
        Fallback only.

        This closes Sundays and second/fourth Saturdays; explicit and
        DATEMAST-inferred holidays require the injected live evaluator.

        In the real scheduler, inject:

            HolidayEvaluator.is_working_day
        """

        return HolidayEvaluator.default_working_day(current_date)

    # =========================================================
    # Date conversion
    # =========================================================

    @staticmethod
    def _to_date(value):

        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

        if isinstance(value, str):

            value = value.strip()

            try:
                return date.fromisoformat(value)

            except ValueError:
                pass

            try:
                return datetime.fromisoformat(
                    value
                ).date()

            except ValueError:
                pass

        raise TypeError(
            f"Unsupported date value: {value!r}"
        )
