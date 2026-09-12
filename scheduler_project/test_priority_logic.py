"""
Priority ordering test.

This test directly exercises the project's real PriorityCalculator
and PriorityQueue without touching Oracle or scheduler.db.

It validates the documented priority hierarchy:

    1. effective date priority
    2. RUN_BY time priority
    3. normal job priority
    4. job_id

It also validates the special expired-window penalty.
"""

from datetime import datetime

from scheduler.priority import PriorityCalculator
from scheduler_queue.priority_queue import PriorityQueue


class TestJob:
    def __init__(
        self,
        job_id,
        name,
        from_time=None,
        to_time=None,
        priority=0,
    ):
        self.id = job_id
        self.name = name
        self.priority = priority

        self.run_config = {}

        if from_time is not None and to_time is not None:
            self.run_config["RUN_BY"] = {
                "FROM_TIME": from_time,
                "TO_TIME": to_time,
            }


def print_priority(label, job, report_date, current_datetime, result):
    print()
    print(label)
    print(f"job_id:          {job.id}")
    print(f"job_name:        {job.name}")
    print(f"report_date:     {report_date}")
    print(f"time_state:      {result.time_state}")
    print(f"date_priority:   {result.date_priority}")
    print(f"time_priority:   {result.time_priority}")
    print(f"job_priority:    {result.job_priority}")
    print(f"priority_key:    {result.priority_key}")


def main():
    calculator = PriorityCalculator()
    queue = PriorityQueue()

    current = datetime(2026, 9, 10, 10, 30)

    print("=" * 60)
    print("PRIORITY CALCULATOR + HEAP TEST")
    print("=" * 60)

    # ---------------------------------------------------------
    # TEST 1
    #
    # Older report date must beat newer report date.
    #
    # Both jobs are ACTIVE, so date_priority is the deciding
    # factor.
    # ---------------------------------------------------------

    old_job = TestJob(
        1,
        "OLDER_DATE",
        "10:00",
        "11:00",
    )

    new_job = TestJob(
        2,
        "NEWER_DATE",
        "10:00",
        "11:00",
    )

    old_result = calculator.calculate(
        old_job,
        "2026-09-08",
        current,
    )

    new_result = calculator.calculate(
        new_job,
        "2026-09-09",
        current,
    )

    print_priority(
        "TEST 1A - OLDER DATE",
        old_job,
        "2026-09-08",
        current,
        old_result,
    )

    print_priority(
        "TEST 1B - NEWER DATE",
        new_job,
        "2026-09-09",
        current,
        new_result,
    )

    assert old_result.date_priority < new_result.date_priority
    assert old_result.priority_key < new_result.priority_key

    print("PASS: older report date receives higher priority.")

    # ---------------------------------------------------------
    # TEST 2
    #
    # Same report date.
    #
    # ACTIVE must beat FUTURE.
    # ---------------------------------------------------------

    active_job = TestJob(
        3,
        "ACTIVE_WINDOW",
        "10:00",
        "11:00",
    )

    future_job = TestJob(
        4,
        "FUTURE_WINDOW",
        "11:00",
        "12:00",
    )

    active_result = calculator.calculate(
        active_job,
        "2026-09-10",
        current,
    )

    future_result = calculator.calculate(
        future_job,
        "2026-09-10",
        current,
    )

    print_priority(
        "TEST 2A - ACTIVE WINDOW",
        active_job,
        "2026-09-10",
        current,
        active_result,
    )

    print_priority(
        "TEST 2B - FUTURE WINDOW",
        future_job,
        "2026-09-10",
        current,
        future_result,
    )

    assert active_result.time_state == "TIME_WINDOW_ACTIVE"
    assert future_result.time_state == "WAITING_FOR_TIME_WINDOW"
    assert active_result.time_priority < future_result.time_priority
    assert active_result.priority_key < future_result.priority_key

    print("PASS: active RUN_BY window beats future window.")

    # ---------------------------------------------------------
    # TEST 3
    #
    # Older report date + EXPIRED window versus newer report
    # date + ACTIVE window.
    #
    # The expired-date penalty should prevent the expired job
    # from permanently dominating current active work.
    # ---------------------------------------------------------

    expired_job = TestJob(
        5,
        "OLD_EXPIRED",
        "09:00",
        "10:00",
    )

    current_job = TestJob(
        6,
        "NEW_ACTIVE",
        "10:00",
        "11:00",
    )

    expired_result = calculator.calculate(
        expired_job,
        "2026-09-01",
        current,
    )

    current_result = calculator.calculate(
        current_job,
        "2026-09-10",
        current,
    )

    print_priority(
        "TEST 3A - OLD EXPIRED",
        expired_job,
        "2026-09-01",
        current,
        expired_result,
    )

    print_priority(
        "TEST 3B - NEW ACTIVE",
        current_job,
        "2026-09-10",
        current,
        current_result,
    )

    assert expired_result.time_state == "TIME_WINDOW_EXPIRED"
    assert current_result.time_state == "TIME_WINDOW_ACTIVE"

    assert (
        expired_result.priority_key
        > current_result.priority_key
    )

    print(
        "PASS: newer active work beats older expired-window work."
    )

    # ---------------------------------------------------------
    # TEST 4
    #
    # Same report date and same time state.
    #
    # Lower normal job priority wins.
    # ---------------------------------------------------------

    high_priority_number = TestJob(
        7,
        "JOB_PRIORITY_10",
        "10:00",
        "11:00",
        priority=10,
    )

    low_priority_number = TestJob(
        8,
        "JOB_PRIORITY_1",
        "10:00",
        "11:00",
        priority=1,
    )

    result_10 = calculator.calculate(
        high_priority_number,
        "2026-09-10",
        current,
    )

    result_1 = calculator.calculate(
        low_priority_number,
        "2026-09-10",
        current,
    )

    print_priority(
        "TEST 4A - JOB PRIORITY 10",
        high_priority_number,
        "2026-09-10",
        current,
        result_10,
    )

    print_priority(
        "TEST 4B - JOB PRIORITY 1",
        low_priority_number,
        "2026-09-10",
        current,
        result_1,
    )

    assert result_1.job_priority < result_10.job_priority
    assert result_1.priority_key < result_10.priority_key

    print("PASS: lower numeric job priority wins.")

    # ---------------------------------------------------------
    # TEST 5
    #
    # Complete heap test.
    #
    # Deliberately insert jobs in an arbitrary order.
    #
    # Expected order:
    #
    #   old active
    #   same-date active with lower job priority
    #   same-date future
    #   old expired
    #
    # The final order demonstrates that the heap uses the
    # calculated priority_key rather than insertion order.
    # ---------------------------------------------------------

    heap_jobs = [
        (
            TestJob(
                12,
                "SAME_DATE_FUTURE",
                "11:00",
                "12:00",
            ),
            "2026-09-10",
        ),
        (
            TestJob(
                11,
                "OLD_ACTIVE",
                "10:00",
                "11:00",
            ),
            "2026-09-08",
        ),
        (
            TestJob(
                13,
                "SAME_DATE_ACTIVE_LOW_PRIORITY",
                "10:00",
                "11:00",
                priority=1,
            ),
            "2026-09-10",
        ),
        (
            TestJob(
                14,
                "OLD_EXPIRED",
                "09:00",
                "10:00",
            ),
            "2026-09-01",
        ),
    ]

    print()
    print("=" * 60)
    print("TEST 5 - HEAP INSERTION ORDER")
    print("=" * 60)

    # Deliberately arbitrary insertion order.
    for job, report_date in heap_jobs:
        result = calculator.calculate(
            job,
            report_date,
            current,
        )

        queue.push(
            priority_key=result.priority_key,
            job_id=job.id,
            job_name=job.name,
            report_date=report_date,
        )

        print(
            f"INSERTED: job_id={job.id}, "
            f"name={job.name}, "
            f"priority_key={result.priority_key}"
        )

    ordered_items = queue.get_all()

    print()
    print("=" * 60)
    print("TEST 5 - HEAP ORDER")
    print("=" * 60)

    for item in ordered_items:
        print(item)

    actual_order = [
        item.job_id
        for item in ordered_items
    ]

    expected_order = [
        11,
        13,
        12,
        14,
    ]

    print()
    print("EXPECTED ORDER:", expected_order)
    print("ACTUAL ORDER:  ", actual_order)

    assert actual_order == expected_order, (
        f"Expected heap order {expected_order}, "
        f"got {actual_order}"
    )

    print()
    print("PASS: heap ordering follows priority_key.")

    # ---------------------------------------------------------
    # TEST 6
    #
    # Verify persisted-style list priority keys can be
    # normalized by the real PriorityQueue.
    # ---------------------------------------------------------

    queue.clear()

    queue.push(
        priority_key=[-2, 0, 0, 20],
        job_id=20,
        job_name="LIST_KEY",
    )

    queue.push(
        priority_key=[-1, 0, 0, 21],
        job_id=21,
        job_name="LIST_KEY_2",
    )

    normalized_order = [
        item.job_id
        for item in queue.get_all()
    ]

    assert normalized_order == [20, 21]

    print(
        "PASS: JSON/list priority keys are normalized correctly."
    )

    print()
    print("=" * 60)
    print("ALL PRIORITY TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    main()
