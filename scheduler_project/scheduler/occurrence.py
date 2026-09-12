"""Oracle-compatible scheduled-occurrence planning.

The legacy package stages an occurrence before it decides whether the
current time is inside ``RUN_BY``.  This module preserves that useful split:
it answers *which report-date occurrences are due for this run date*; the
eligibility layer later applies controls, confirmation and time-window gates.

In particular, a non-SAME_DAY DAILY run is intentionally different from a
periodic run:

* DAILY: previous DATEMAST report date is the base T date and the current
  run date is eligible to execute it.
* WEEKLY/FORTNIGHTLY/MONTHLY/QUARTERLY/BI-ANNUALLY/ANNUALLY/SPECIFIC_DATE:
  the natural report occurrence executes on its next working day.

The report date passed to the target Oracle procedure is ``base T + margin``
just as it was in ``PKG_SCHEDULE_EXTRACTS.calculate_report_date``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from scheduler.holiday import HolidayEvaluator


@dataclass(frozen=True)
class ScheduledOccurrence:
    """One unique, durable scheduled occurrence for one master job."""

    occurrence_key: str
    frequency: str
    occurrence_date: date
    t_date: date
    report_date: date
    target_date: date
    execution_date: date
    run_date: date


class OracleCompatibleOccurrencePlanner:
    """Plan Oracle-package-compatible occurrences without mutating state."""

    DAILY = "DAILY"

    def __init__(
        self,
        frequency_evaluator,
        margin_calculator,
        *,
        lookback_days=370,
    ):
        self.frequency_evaluator = frequency_evaluator
        self.margin_calculator = margin_calculator
        checker_owner = getattr(margin_calculator.working_day_checker, "__self__", None)
        self.holiday_evaluator = checker_owner if isinstance(checker_owner, HolidayEvaluator) else HolidayEvaluator()
        self.lookback_days = max(7, int(lookback_days))

    def due_occurrences(self, job, run_date, datemast):
        """Return all distinct occurrences due on ``run_date``.

        A job may have more than one frequency.  Values are deduplicated by
        report date, matching the Oracle ``NAME + REPORT_DATE`` staging
        identity.  Thus coincident rules create one occurrence while DAILY
        and a periodic boundary create two whenever their report dates differ.
        """

        run_date = self._to_date(run_date)
        same_day = self._as_bool(getattr(job, "same_day", 0))
        frequencies = self.frequency_evaluator.get_frequencies(job)
        occurrences = {}

        for frequency in frequencies:
            context = self._context_for_frequency(
                job=job,
                frequency=frequency,
                run_date=run_date,
                datemast=datemast,
                same_day=same_day,
            )

            if context is None:
                continue

            # NAME + REPORT_DATE was the legacy unique identity.  The local
            # key substitutes immutable master ID for name, avoiding an
            # accidental merge merely because two jobs share a display label.
            occurrences.setdefault(
                context.report_date,
                context,
            )

        return sorted(
            occurrences.values(),
            key=lambda item: (
                item.execution_date,
                item.report_date,
                item.frequency,
            ),
        )

    def _context_for_frequency(
        self,
        *,
        job,
        frequency,
        run_date,
        datemast,
        same_day,
    ):
        if same_day:
            if not self.frequency_evaluator.matches_frequency(
                job,
                frequency,
                run_date,
            ):
                return None
            base_date = run_date
            execution_date = run_date

        elif frequency == self.DAILY:
            if not self.holiday_evaluator.can_run_on_day(job, run_date):
                return None
            base_date = self._previous_report_date(
                datemast,
                run_date,
            )
            if base_date is None:
                return None
            # The package stages the daily row with this invocation's run
            # date.  Unlike periodic boundaries, it does not defer a daily
            # row to the following working day.
            execution_date = run_date

        else:
            base_date = self._natural_date_due_on_run_date(
                job,
                frequency,
                run_date,
            )
            if base_date is None:
                return None
            execution_date = run_date

        report_date = self.margin_calculator.calculate(
            base_date,
            getattr(job, "margin", None),
        )

        key = self.occurrence_key(
            getattr(job, "id", None),
            report_date,
        )

        return ScheduledOccurrence(
            occurrence_key=key,
            frequency=frequency,
            occurrence_date=base_date,
            t_date=base_date,
            report_date=report_date,
            target_date=report_date,
            execution_date=execution_date,
            run_date=run_date,
        )

    def _natural_date_due_on_run_date(self, job, frequency, run_date):
        """Find a periodic natural date whose next working day is run date."""

        candidate = run_date - timedelta(days=1)

        for _ in range(self.lookback_days):
            if self.frequency_evaluator.matches_frequency(
                job,
                frequency,
                candidate,
            ):
                next_working = self.margin_calculator.next_working_day(
                    candidate
                )
                if next_working == run_date:
                    return candidate
                if next_working < run_date:
                    # Next-working dates are monotonic. No earlier periodic
                    # boundary can first become due on this later run date.
                    return None

            candidate -= timedelta(days=1)

        return None

    @staticmethod
    def occurrence_key(job_id, report_date):
        report_date = OracleCompatibleOccurrencePlanner._to_date(report_date)
        return f"{job_id}:{report_date.isoformat()}"

    @staticmethod
    def _previous_report_date(datemast, run_date):
        if datemast is None:
            return None

        method = getattr(datemast, "get_previous_report_date", None)
        if callable(method):
            value = method(run_date)
            return (
                OracleCompatibleOccurrencePlanner._to_date(value)
                if value is not None
                else None
            )

        # Compatibility for small DATEMAST test doubles from older callers.
        values_method = getattr(datemast, "get_report_dates", None)
        if callable(values_method):
            values = [
                OracleCompatibleOccurrencePlanner._to_date(value)
                for value in values_method()
            ]
            values = [value for value in values if value < min(run_date, date.today())]
            return max(values) if values else None

        return None

    @staticmethod
    def _as_bool(value):
        if isinstance(value, str):
            return value.strip().upper() in {"1", "Y", "YES", "TRUE", "T"}
        return bool(value)

    @staticmethod
    def _to_date(value):
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value
        if isinstance(value, str):
            return date.fromisoformat(value.strip())
        raise TypeError(f"Unsupported date value: {value!r}")
