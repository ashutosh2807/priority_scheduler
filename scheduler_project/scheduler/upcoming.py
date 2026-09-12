"""Scheduler-owned, read-only forecast of upcoming occurrences.

The portal must never recreate frequency or holiday decisions.  Instead the
worker includes this compact preview in its control-API snapshot.  A preview
does not create STAGING/READY rows and does not promise that DATEMAST or a
confirmation gate will be satisfied when the date arrives.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta


class UpcomingPlanner:
    """Build a transparent forecast from the same occurrence planner."""

    def __init__(
        self,
        frequency_evaluator,
        holiday_evaluator,
        margin_calculator,
        time_window_evaluator,
        occurrence_planner=None,
        datemast=None,
    ):
        self.frequency_evaluator = frequency_evaluator
        self.holiday_evaluator = holiday_evaluator
        self.margin_calculator = margin_calculator
        self.time_window_evaluator = time_window_evaluator
        # The worker injects the Oracle-compatible planner.  Keeping this
        # optional preserves the small, generic preview API for older callers.
        self.occurrence_planner = occurrence_planner
        self.datemast = datemast

    def build(self, jobs, *, start_date=None, days=14, limit=120):
        start_date = self._to_date(start_date or date.today())
        days = max(1, min(int(days), 62))
        records = []
        seen = set()
        today = date.today()
        for offset in range(days):
            run_date = start_date + timedelta(days=offset)
            for job in jobs:
                if not self._as_bool(getattr(job, "is_active", 0)):
                    continue
                if self.occurrence_planner is not None:
                    # Do not use a known *current* DATEMAST record as if it
                    # were a future daily report date.  Periodic occurrences
                    # remain exactly forecastable; daily future rows are
                    # labelled as waiting for an authoritative DATEMAST date.
                    contexts = []
                    if run_date <= today:
                        contexts = self.occurrence_planner.due_occurrences(
                            job,
                            run_date,
                            self.datemast,
                        )
                    else:
                        contexts = [
                            item
                            for item in self.occurrence_planner.due_occurrences(
                                job,
                                run_date,
                                self.datemast,
                            )
                            if item.frequency != "DAILY" or self._as_bool(getattr(job, "same_day", 0))
                        ]

                    for context in contexts:
                        key = context.occurrence_key
                        if key in seen:
                            continue
                        seen.add(key)
                        records.append(self._planned_record(job, context))

                    # Keep the dashboard useful when a daily report date is
                    # not yet published, but never fabricate the Oracle date
                    # parameter.  This placeholder has no occurrence key and
                    # cannot be mistaken for a runnable queue item.
                    if (
                        "DAILY" in self.frequency_evaluator.get_frequencies(job)
                        and not self._as_bool(getattr(job, "same_day", 0))
                        and not any(item.frequency == "DAILY" for item in contexts)
                        and self.holiday_evaluator.can_run_on_day(job, run_date)
                    ):
                        key = (getattr(job, "id", None), "DAILY_PENDING", run_date)
                        if key not in seen:
                            seen.add(key)
                            records.append(self._daily_waiting_record(job, run_date))
                    continue

                if not self.frequency_evaluator.is_scheduled(job, run_date):
                    continue
                key = (getattr(job, "id", None), run_date)
                if key in seen:
                    continue
                seen.add(key)
                records.append(self._record(job, run_date))
        records.sort(key=lambda item: (
            item["execution_date"] or "9999-12-31",
            item["from_time"] or "23:59",
            item["job_id"] or 0,
            item.get("occurrence_key") or "",
        ))
        return records if limit is None else records[:max(1, int(limit))]

    def _planned_record(self, job, context):
        same_day = self._as_bool(getattr(job, "same_day", 0))
        allowed = self.holiday_evaluator.can_run_on_day(
            job, context.occurrence_date if same_day else context.execution_date
        )
        from_time, to_time = self.time_window_evaluator.get_run_by(job)
        return {
            "occurrence_key": context.occurrence_key,
            "job_id": getattr(job, "id", None),
            "job_name": getattr(job, "name", ""),
            "frequencies": list((getattr(job, "run_config", {}) or {}).get("RUNS_ON", [])),
            "frequency": context.frequency,
            "occurrence_date": context.occurrence_date.isoformat(),
            "t_date": context.t_date.isoformat(),
            "report_date": context.report_date.isoformat(),
            "target_date": context.target_date.isoformat(),
            "execution_date": context.execution_date.isoformat(),
            "run_date": context.run_date.isoformat(),
            "from_time": from_time.strftime("%H:%M") if from_time else None,
            "to_time": to_time.strftime("%H:%M") if to_time else None,
            "same_day": same_day,
            "state": "FORECAST" if allowed else "CALENDAR_POLICY_BLOCKED",
            "time_flag": self._as_bool(getattr(job, "time_flag", 0)),
        }

    def _daily_waiting_record(self, job, run_date):
        from_time, to_time = self.time_window_evaluator.get_run_by(job)
        return {
            "occurrence_key": None,
            "job_id": getattr(job, "id", None),
            "job_name": getattr(job, "name", ""),
            "frequencies": list((getattr(job, "run_config", {}) or {}).get("RUNS_ON", [])),
            "frequency": "DAILY",
            "occurrence_date": run_date.isoformat(),
            "t_date": None,
            "report_date": None,
            "target_date": None,
            "execution_date": run_date.isoformat(),
            "run_date": run_date.isoformat(),
            "from_time": from_time.strftime("%H:%M") if from_time else None,
            "to_time": to_time.strftime("%H:%M") if to_time else None,
            "same_day": self._as_bool(getattr(job, "same_day", 0)),
            "state": "FORECAST_AWAITING_DATEMAST",
            "time_flag": self._as_bool(getattr(job, "time_flag", 0)),
        }

    def _record(self, job, occurrence_date):
        same_day = self._as_bool(getattr(job, "same_day", 0))
        allowed = self.holiday_evaluator.can_run_on_day(job, occurrence_date)
        if same_day:
            execution_date = occurrence_date if allowed else None
            state = "FORECAST" if allowed else "CALENDAR_POLICY_BLOCKED"
        else:
            execution_date = self.margin_calculator.next_working_day(occurrence_date)
            state = "FORECAST_AWAITING_DATEMAST"
        from_time, to_time = self.time_window_evaluator.get_run_by(job)
        return {
            "occurrence_key": None,
            "job_id": getattr(job, "id", None),
            "job_name": getattr(job, "name", ""),
            "frequencies": list((getattr(job, "run_config", {}) or {}).get("RUNS_ON", [])),
            "occurrence_date": occurrence_date.isoformat(),
            "t_date": None,
            # The final report date for non-same-day runs is resolved against
            # DATEMAST at runtime.  Keep this labelled as an occurrence, not
            # a false guarantee of the Oracle parameter.
            "report_date": occurrence_date.isoformat() if same_day else None,
            "execution_date": execution_date.isoformat() if execution_date else None,
            "run_date": occurrence_date.isoformat(),
            "from_time": from_time.strftime("%H:%M") if from_time else None,
            "to_time": to_time.strftime("%H:%M") if to_time else None,
            "same_day": same_day,
            "state": state,
            "time_flag": self._as_bool(getattr(job, "time_flag", 0)),
        }

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
        return date.fromisoformat(str(value))
