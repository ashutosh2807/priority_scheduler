"""
Comprehensive calendar and eligibility tests for the scheduler core.

These tests lock down the business-date contract before the Django
control UI is introduced.

Core business rules covered here:

1. SAME_DAY=1:
       occurrence_date == execution_date == report_date

2. SAME_DAY=0:
       execution_date is the next working day strictly after the
       scheduled occurrence.

3. Margin:
       T+N is a calendar-day offset.
       Example:
           T = 15-Sep
           T+3 = 18-Sep

       Working-day logic belongs to execution-date calculation,
       not to the T+N margin calculation.

4. DATEMAST threshold:
       A non-SAME_DAY occurrence waits until DATEMAST reaches the
       calculated target date.

5. Crucial report-date rule:
       The occurrence keeps its original report_date.
       Reaching the T+N threshold must NOT replace the report_date
       with the latest DATEMAST date.

   Example:
       occurrence/report date = 15-Sep
       margin                 = T+3
       threshold              = 18-Sep
       next working day       = 17-Sep

       17-Sep -> next working day reached, but threshold not reached
                 -> WAITING_DATEMAST

       18-Sep -> threshold reached
                 -> READY
                 -> Oracle must receive report_date = 15-Sep

6. Frequency defaults:
       DAILY
       WEEKLY      -> final working day of the week
       FORTNIGHTLY -> 15th and last calendar day
       MONTHLY     -> last calendar day
       QUARTERLY   -> Mar/Jun/Sep/Dec month-end
       BI-ANNUALLY -> Mar/Sep month-end
       ANNUALLY    -> 31st March

7. Holiday/weekend handling:
       next working day skips second/fourth Saturdays, Sundays and
       configured holidays; first/third/fifth Saturdays work by default.

The tests use only the scheduler's plain Python classes. No Django ORM
and no Oracle connection are required.
"""

import unittest
from datetime import date, datetime

from models.schedule_master import ScheduleMaster
from scheduler.eligibility import EligibilityEvaluator
from scheduler.frequency import FrequencyEvaluator
from scheduler.holiday import HolidayEvaluator
from scheduler.margin import MarginCalculator


class InMemoryDateMast:
    """Small DATEMAST test double matching the scheduler API."""

    def __init__(self, report_dates):
        self.report_dates = {
            self._to_date(value)
            for value in report_dates
        }

    def get_latest_report_date(self):
        if not self.report_dates:
            return None

        return max(self.report_dates)

    def get_report_date_for(self, current_date):
        current_date = self._to_date(current_date)

        candidates = [
            value
            for value in self.report_dates
            if value <= current_date
        ]

        if not candidates:
            return None

        return max(candidates)

    @staticmethod
    def _to_date(value):
        if isinstance(value, datetime):
            return value.date()

        if isinstance(value, date):
            return value

        return date.fromisoformat(value)


def make_job(
    *,
    frequency="DAILY",
    same_day=0,
    margin="T+3",
    holiday_run=None,
    time_flag=0,
    run_by=None,
):
    """Create the smallest ScheduleMaster required by the tests."""

    run_config = {
        "RUNS_ON": [frequency],
    }

    if holiday_run is not None:
        run_config["HOLIDAY_RUN"] = holiday_run

    if run_by is not None:
        run_config["RUN_BY"] = run_by

    return ScheduleMaster(
        id=1,
        name="TEST_JOB",
        package_name="TEST_PACKAGE",
        run_config=run_config,
        margin=margin,
        same_day=same_day,
        time_flag=time_flag,
        is_active=1,
    )


class TestMarginCalculator(unittest.TestCase):
    """
    Validate T+N margin semantics.

    T+N is a calendar-day offset. It is intentionally independent
    from the next-working-day execution rule.
    """

    def setUp(self):
        self.holiday = HolidayEvaluator(
            holidays=[
                date(2026, 9, 16),
            ]
        )

        self.margin = MarginCalculator(
            working_day_checker=self.holiday.is_working_day
        )

    def test_t_plus_zero(self):
        self.assertEqual(
            self.margin.calculate(
                date(2026, 9, 15),
                "T",
            ),
            date(2026, 9, 15),
        )

    def test_t_plus_three_is_calendar_plus_three(self):
        # T = 15-Sep
        # T+3 = 18-Sep
        #
        # The holiday on 16-Sep must NOT change the T+3 target.
        self.assertEqual(
            self.margin.calculate(
                date(2026, 9, 15),
                "T+3",
            ),
            date(2026, 9, 18),
        )

    def test_next_working_day_is_separate_from_margin(self):
        # 16-Sep is a holiday.
        # Therefore 15-Sep -> 17-Sep is the next working day.
        self.assertEqual(
            self.margin.next_working_day(
                date(2026, 9, 15)
            ),
            date(2026, 9, 17),
        )

        # 18-Sep -> working third Saturday, 19-Sep.
        self.assertEqual(
            self.margin.next_working_day(
                date(2026, 9, 18)
            ),
            date(2026, 9, 19),
        )


class TestFrequencyDefaults(unittest.TestCase):
    """Validate the scheduler's configured/default frequency rules."""

    def setUp(self):
        self.frequency = FrequencyEvaluator()

    def job(self, frequency):
        return make_job(
            frequency=frequency,
            same_day=1,
            margin="T",
        )

    def test_daily(self):
        job = self.job("DAILY")

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 14),
            )
        )

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 15),
            )
        )

    def test_weekly_defaults_to_last_working_day_of_week(self):
        job = self.job("WEEKLY")

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 11),
            )
        )

        self.assertFalse(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 10),
            )
        )

    def test_weekly_moves_to_thursday_when_friday_is_a_holiday(self):
        holiday = HolidayEvaluator(holidays=[date(2026, 9, 11)])
        frequency = FrequencyEvaluator(working_day_checker=holiday.is_working_day)
        job = self.job("WEEKLY")
        self.assertTrue(frequency.is_scheduled(job, date(2026, 9, 10)))
        self.assertFalse(frequency.is_scheduled(job, date(2026, 9, 11)))

    def test_fortnightly_is_15th_and_last_day(self):
        job = self.job("FORTNIGHTLY")

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 15),
            )
        )

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 30),
            )
        )

        self.assertFalse(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 16),
            )
        )

    def test_fortnightly_february_uses_last_calendar_day(self):
        job = self.job("FORTNIGHTLY")

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2028, 2, 29),
            )
        )

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2027, 2, 28),
            )
        )

    def test_monthly_defaults_to_last_calendar_day(self):
        job = self.job("MONTHLY")

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 30),
            )
        )

        self.assertFalse(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 29),
            )
        )

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2028, 2, 29),
            )
        )

    def test_quarterly_defaults_to_quarter_end(self):
        job = self.job("QUARTERLY")

        quarter_ends = [
            date(2026, 3, 31),
            date(2026, 6, 30),
            date(2026, 9, 30),
            date(2026, 12, 31),
        ]

        for occurrence in quarter_ends:
            with self.subTest(occurrence=occurrence):
                self.assertTrue(
                    self.frequency.is_scheduled(
                        job,
                        occurrence,
                    )
                )

        self.assertFalse(
            self.frequency.is_scheduled(
                job,
                date(2026, 8, 31),
            )
        )

    def test_half_yearly_defaults_to_financial_year_end_and_half_year(self):
        job = self.job("BI_ANNUALLY")

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 3, 31),
            )
        )

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 9, 30),
            )
        )

        self.assertFalse(
            self.frequency.is_scheduled(
                job,
                date(2026, 5, 31),
            )
        )

    def test_annually_defaults_to_march_31(self):
        job = self.job("ANNUALLY")

        self.assertTrue(
            self.frequency.is_scheduled(
                job,
                date(2026, 3, 31),
            )
        )

        self.assertFalse(
            self.frequency.is_scheduled(
                job,
                date(2026, 2, 28),
            )
        )


class TestEligibilitySameDay(unittest.TestCase):
    """Validate SAME_DAY=1 behavior."""

    def setUp(self):
        self.holiday = HolidayEvaluator()
        self.evaluator = EligibilityEvaluator(
            holiday_evaluator=self.holiday
        )

    def test_same_day_ready_on_occurrence(self):
        job = make_job(
            frequency="DAILY",
            same_day=1,
            margin="T",
        )

        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                15,
                10,
                15,
            ),
            occurrence_date=date(2026, 9, 15),
        )

        self.assertTrue(result.eligible)
        self.assertEqual(
            result.state,
            EligibilityEvaluator.READY,
        )
        self.assertEqual(
            result.occurrence_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.execution_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.report_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.target_date,
            date(2026, 9, 15),
        )

    def test_same_day_respects_holiday_run(self):
        holiday_date = date(2026, 9, 17)

        holiday = HolidayEvaluator(
            holidays=[holiday_date]
        )

        evaluator = EligibilityEvaluator(
            holiday_evaluator=holiday
        )

        job = make_job(
            frequency="DAILY",
            same_day=1,
            margin="T",
            holiday_run=[],
        )

        result = evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                17,
                10,
                15,
            ),
            occurrence_date=holiday_date,
        )

        self.assertFalse(result.eligible)
        self.assertEqual(
            result.state,
            EligibilityEvaluator.STAGING,
        )
        self.assertEqual(
            result.waiting_for,
            "CALENDAR_DAY",
        )


class TestEligibilitySameDayZero(unittest.TestCase):
    """
    Validate the universal SAME_DAY=0 next-working-day rule and
    the DATEMAST threshold/report-date contract.
    """

    def setUp(self):
        # 16-Sep is deliberately a holiday so that:
        #
        #     15-Sep occurrence
        #     16-Sep holiday
        #     17-Sep next working day
        #     18-Sep = T+3
        #
        # This exactly separates the execution date from the
        # calendar T+3 threshold.
        self.holiday = HolidayEvaluator(
            holidays=[
                date(2026, 9, 16),
            ]
        )

        self.evaluator = EligibilityEvaluator(
            holiday_evaluator=self.holiday
        )

    def test_same_day_zero_waits_until_next_working_day(self):
        job = make_job(
            frequency="DAILY",
            same_day=0,
            margin="T",
        )

        # 15-Sep -> next working day is 17-Sep.
        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                15,
                10,
                15,
            ),
            t_date=date(2026, 9, 15),
            datemast=InMemoryDateMast(
                [date(2026, 9, 15)]
            ),
            occurrence_date=date(2026, 9, 15),
        )

        self.assertFalse(result.eligible)
        self.assertEqual(
            result.state,
            EligibilityEvaluator.WAITING_EXECUTION_DATE,
        )
        self.assertEqual(
            result.execution_date,
            date(2026, 9, 17),
        )

    def test_same_day_zero_next_working_day_skips_holiday(self):
        job = make_job(
            frequency="DAILY",
            same_day=0,
            margin="T",
        )

        # 16-Sep is the occurrence and is a holiday.
        # 17-Sep is the next working day, so evaluate on 17-Sep.
        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                17,
                10,
                15,
            ),
            t_date=date(2026, 9, 16),
            datemast=InMemoryDateMast(
                [date(2026, 9, 16), date(2026, 9, 17)]
            ),
            occurrence_date=date(2026, 9, 16),
        )

        self.assertTrue(result.eligible)
        self.assertEqual(
            result.state,
            EligibilityEvaluator.READY,
        )
        self.assertEqual(
            result.execution_date,
            date(2026, 9, 17),
        )

    def test_manual_run_executes_immediately(self):
        """
        Django can request an immediate/manual run.

        Manual execution bypasses the normal scheduled occurrence,
        frequency, execution-date and DATEMAST gates.
        """

        job = make_job(
            frequency="WEEKLY",
            same_day=0,
            margin="T+3",
        )

        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                15,
                10,
                15,
            ),
            control={
                "control_status": "ACTIVE",
                "manual_run": 1,
            },
            t_date=None,
            datemast=InMemoryDateMast([]),
            occurrence_date=date(2026, 9, 15),
        )

        self.assertTrue(result.eligible)
        self.assertEqual(
            result.state,
            EligibilityEvaluator.READY,
        )
        self.assertEqual(
            result.occurrence_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.execution_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.report_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.target_date,
            date(2026, 9, 15),
        )

    def test_paused_job_does_not_execute_even_when_manual_run_requested(self):
        """
        PAUSED takes precedence over a Django manual-run request.
        """

        job = make_job(
            frequency="DAILY",
            same_day=0,
            margin="T+3",
        )

        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                18,
                10,
                15,
            ),
            control={
                "control_status": "PAUSED",
                "manual_run": 1,
            },
            t_date=date(2026, 9, 15),
            datemast=InMemoryDateMast(
                [
                    date(2026, 9, 15),
                    date(2026, 9, 16),
                    date(2026, 9, 17),
                    date(2026, 9, 18),
                ]
            ),
            occurrence_date=date(2026, 9, 15),
        )

        self.assertFalse(result.eligible)
        self.assertEqual(
            result.state,
            EligibilityEvaluator.PAUSED,
        )
        self.assertEqual(
            result.waiting_for,
            "CONTROL",
        )
        self.assertEqual(
            result.occurrence_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.t_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.target_date,
            date(2026, 9, 18),
        )
        self.assertEqual(
            result.report_date,
            date(2026, 9, 15),
        )

    def test_t_plus_three_waits_before_threshold(self):
        """
        Core business scenario:

            report/occurrence = 15-Sep
            T                 = 15-Sep
            margin            = T+3
            threshold         = 18-Sep
            execution_date    = 17-Sep

        On 17-Sep the next-working-day condition is satisfied, but
        DATEMAST has only reached 17-Sep. The job must still wait.
        """

        job = make_job(
            frequency="DAILY",
            same_day=0,
            margin="T+3",
        )

        datemast = InMemoryDateMast(
            [
                date(2026, 9, 15),
                date(2026, 9, 16),
                date(2026, 9, 17),
            ]
        )

        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                17,
                10,
                15,
            ),
            t_date=date(2026, 9, 15),
            datemast=datemast,
            occurrence_date=date(2026, 9, 15),
        )

        self.assertFalse(result.eligible)
        self.assertEqual(
            result.state,
            EligibilityEvaluator.WAITING_DATEMAST,
        )
        self.assertEqual(
            result.waiting_for,
            "DATEMAST",
        )
        self.assertEqual(
            result.execution_date,
            date(2026, 9, 17),
        )
        self.assertEqual(
            result.t_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.target_date,
            date(2026, 9, 18),
        )

    def test_t_plus_three_becomes_ready_but_keeps_original_report_date(self):
        """
        This is the most important regression test.

        On 18-Sep DATEMAST has reached the T+3 threshold. The job is
        READY, but the extract is still for the 15-Sep report date.

        Expected:

            report_date    = 15-Sep
            target_date    = 18-Sep
            execution_date = 17-Sep
            state           = READY

        The report_date must NOT become 18-Sep merely because 18-Sep
        is now the latest DATEMAST date.
        """

        job = make_job(
            frequency="DAILY",
            same_day=0,
            margin="T+3",
        )

        datemast = InMemoryDateMast(
            [
                date(2026, 9, 15),
                date(2026, 9, 16),
                date(2026, 9, 17),
                date(2026, 9, 18),
            ]
        )

        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                18,
                10,
                15,
            ),
            t_date=date(2026, 9, 15),
            datemast=datemast,
            occurrence_date=date(2026, 9, 15),
        )

        self.assertTrue(result.eligible)
        self.assertEqual(
            result.state,
            EligibilityEvaluator.READY,
        )

        self.assertEqual(
            result.occurrence_date,
            date(2026, 9, 15),
        )

        self.assertEqual(
            result.execution_date,
            date(2026, 9, 17),
        )

        self.assertEqual(
            result.t_date,
            date(2026, 9, 15),
        )

        self.assertEqual(
            result.target_date,
            date(2026, 9, 18),
        )

        # Critical assertion:
        self.assertEqual(
            result.report_date,
            date(2026, 9, 15),
        )

    def test_report_date_does_not_move_when_datemast_advances_further(self):
        """
        Once the 15-Sep occurrence becomes eligible, later DATEMAST
        dates must not change that occurrence's report_date.
        """

        job = make_job(
            frequency="DAILY",
            same_day=0,
            margin="T+3",
        )

        datemast = InMemoryDateMast(
            [
                date(2026, 9, 15),
                date(2026, 9, 16),
                date(2026, 9, 17),
                date(2026, 9, 18),
                date(2026, 9, 19),
                date(2026, 9, 21),
            ]
        )

        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                21,
                10,
                15,
            ),
            t_date=date(2026, 9, 15),
            datemast=datemast,
            occurrence_date=date(2026, 9, 15),
        )

        self.assertTrue(result.eligible)
        self.assertEqual(
            result.report_date,
            date(2026, 9, 15),
        )
        self.assertEqual(
            result.target_date,
            date(2026, 9, 18),
        )

    def test_weekend_occurrence_executes_on_next_working_day(self):
        """
        SAME_DAY=0 uses the next working day strictly after the
        occurrence. This test deliberately uses a Saturday occurrence.
        """

        job = make_job(
            frequency="DAILY",
            same_day=0,
            margin="T",
        )

        saturday = date(2026, 9, 19)

        result = self.evaluator.evaluate(
            job=job,
            current_datetime=datetime(
                2026,
                9,
                21,
                10,
                15,
            ),
            t_date=saturday,
            datemast=InMemoryDateMast(
                [saturday]
            ),
            occurrence_date=saturday,
        )

        self.assertTrue(result.eligible)
        self.assertEqual(
            result.execution_date,
            date(2026, 9, 21),
        )


class TestOccurrenceSelection(unittest.TestCase):
    """Validate previous/next occurrence lookup independently."""

    def setUp(self):
        self.frequency = FrequencyEvaluator()

    def test_previous_weekly_occurrence(self):
        job = make_job(
            frequency="WEEKLY",
            same_day=1,
            margin="T",
        )

        self.assertEqual(
            self.frequency.get_previous_occurrence(
                job,
                date(2026, 9, 15),
            ),
            date(2026, 9, 11),
        )

    def test_next_weekly_occurrence(self):
        job = make_job(
            frequency="WEEKLY",
            same_day=1,
            margin="T",
        )

        self.assertEqual(
            self.frequency.get_next_occurrence(
                job,
                date(2026, 9, 15),
            ),
            date(2026, 9, 19),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
