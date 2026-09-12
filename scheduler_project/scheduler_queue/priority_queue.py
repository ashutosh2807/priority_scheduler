import heapq
from dataclasses import dataclass, field


@dataclass(order=True)
class QueueItem:
    """
    Item stored inside the priority heap.

    The derived sort key applies an optional operator rank, followed by the
    scheduler's priority_key. Eligibility and durable READY state stay outside
    this heap.

    job_id is included as a deterministic tie-breaker so that
    two jobs with the same priority are still ordered
    consistently.
    """

    _sort_key: tuple = field(init=False, repr=False, compare=True)

    priority_key: tuple = field(compare=False)

    job_id: int = field(compare=True)

    # A master job may have several READY report-date occurrences at once
    # (for example DAILY + FORTNIGHTLY).  It participates only as the final
    # deterministic tie-breaker after the existing priority/job-id fields.
    occurrence_key: str = field(
        default="",
        compare=True,
    )

    job_name: str = field(
        default="",
        compare=False,
    )

    report_date: object = field(
        default=None,
        compare=False,
    )

    operator_rank: object = field(default=None, compare=False)

    def __post_init__(self):
        self._sort_key = (
            (0, self.operator_rank, self.priority_key)
            if self.operator_rank is not None
            else (1, 0, self.priority_key)
        )


class PriorityQueue:
    """
    In-memory priority heap.

    Important:

        This heap is DERIVED state.

        Persistent READY records in SQLite remain the
        authoritative state.

    Therefore the heap can safely be rebuilt after:

        - scheduler restart
        - application restart
        - queue corruption
        - priority recalculation

    The queue should never be treated as the source of truth.
    """

    def __init__(self, order_provider=None):
        self._heap = []
        self.order_provider = order_provider
        self._operator_order = {}

    def apply_operator_order(self):
        """Derive ordering from durable occurrence intents; never add work."""
        self._operator_order = self.order_provider() if self.order_provider else {}
        for item in self._heap:
            item.operator_rank = self._operator_order.get(item.occurrence_key)
            item.__post_init__()
        heapq.heapify(self._heap)

    # =========================================================
    # Push
    # =========================================================

    def push(
        self,
        priority_key,
        job_id,
        job_name="",
        report_date=None,
        occurrence_key=None,
    ):
        """
        Add or replace a job in the heap.

        If the same occurrence key already exists, the old entry is
        removed first.  Old callers which do not pass an occurrence key keep
        their historic one-entry-per-job behavior.

        This prevents duplicate queue entries when a job's
        priority is recalculated.
        """

        job_id = int(job_id)

        occurrence_key = str(
            occurrence_key
            if occurrence_key is not None
            else "job:{0}".format(job_id)
        )

        # Prevent duplicate entries for the same durable occurrence without
        # collapsing sibling report dates from the same master job.
        self.remove(
            job_id=job_id,
            occurrence_key=occurrence_key,
        )

        priority_key = self._normalize_priority_key(
            priority_key
        )

        item = QueueItem(
            priority_key=priority_key,
            job_id=job_id,
            occurrence_key=occurrence_key,
            job_name=job_name or "",
            report_date=report_date,
            operator_rank=self._operator_order.get(occurrence_key),
        )

        heapq.heappush(
            self._heap,
            item,
        )

        return item

    # =========================================================
    # Pop
    # =========================================================

    def pop(self):
        """
        Remove and return the highest-priority job.

        Returns
        -------
        QueueItem or None
        """

        if not self._heap:
            return None

        return heapq.heappop(
            self._heap
        )

    # =========================================================
    # Peek
    # =========================================================

    def peek(self):
        """
        Return the highest-priority job without removing it.

        Returns
        -------
        QueueItem or None
        """

        if not self._heap:
            return None

        return self._heap[0]

    # =========================================================
    # Remove
    # =========================================================

    def remove(
        self,
        job_id=None,
        occurrence_key=None,
    ):
        """
        Remove a job from the heap by job ID, or exactly one occurrence when
        ``occurrence_key`` is supplied.

        Returns
        -------
        bool
            True if the job was removed.
            False if it was not present.
        """

        if occurrence_key is None:
            if job_id is None:
                return False
            job_id = int(job_id)
        else:
            occurrence_key = str(occurrence_key)

        original_size = len(
            self._heap
        )

        self._heap = [
            item
            for item in self._heap
            if (
                item.occurrence_key != occurrence_key
                if occurrence_key is not None
                else item.job_id != job_id
            )
        ]

        if len(self._heap) == original_size:
            return False

        heapq.heapify(
            self._heap
        )

        return True

    # =========================================================
    # Contains
    # =========================================================

    def contains(
        self,
        job_id=None,
        occurrence_key=None,
    ):
        """
        Check whether a job exists in the heap.
        """

        if occurrence_key is not None:
            occurrence_key = str(occurrence_key)
            return any(
                item.occurrence_key == occurrence_key
                for item in self._heap
            )

        if job_id is None:
            return False

        job_id = int(job_id)
        return any(item.job_id == job_id for item in self._heap)

    # =========================================================
    # Clear
    # =========================================================

    def clear(self):
        """
        Remove all jobs from the heap.
        """

        self._heap.clear()

    # =========================================================
    # Rebuild
    # =========================================================

    def rebuild(
        self,
        ready_jobs,
    ):
        """
        Completely rebuild the heap from persistent READY jobs.

        This is the preferred operation after each scheduler
        cycle because READY records are the source of truth.

        ready_jobs may contain ReadyJob objects with:

            job_id
            job_name
            report_date
            priority_key

        or compatible objects exposing those attributes.
        """

        self.clear()

        self._operator_order = self.order_provider() if self.order_provider else {}

        if ready_jobs is None:
            return

        for ready_job in ready_jobs:

            priority_key = getattr(
                ready_job,
                "priority_key",
                None,
            )

            if priority_key is None:
                continue

            priority_key = self._normalize_priority_key(
                priority_key
            )

            job_id = getattr(
                ready_job,
                "job_id",
                None,
            )

            if job_id is None:
                continue

            self.push(
                priority_key=priority_key,
                job_id=job_id,
                job_name=getattr(
                    ready_job,
                    "job_name",
                    "",
                ),
                report_date=getattr(
                    ready_job,
                    "report_date",
                    None,
                ),
                occurrence_key=getattr(
                    ready_job,
                    "occurrence_key",
                    None,
                ),
            )

    # =========================================================
    # Get all
    # =========================================================

    def get_all(self):
        """
        Return all heap items ordered by priority.

        This does not modify the heap.
        """

        return sorted(
            self._heap
        )

    # =========================================================
    # Size
    # =========================================================

    def size(self):
        """
        Return the number of jobs in the heap.
        """

        return len(
            self._heap
        )

    # =========================================================
    # Empty
    # =========================================================

    def is_empty(self):
        """
        Return True when the heap contains no jobs.
        """

        return len(
            self._heap
        ) == 0

    # =========================================================
    # Priority-key normalization
    # =========================================================

    @staticmethod
    def _normalize_priority_key(
        priority_key,
    ):
        """
        Normalize a priority key loaded from SQLite/JSON.

        Example:

            [1, 10, 0, 5]

        becomes:

            (1, 10, 0, 5)

        Nested lists are converted recursively to tuples.

        This is important because priority_key is persisted
        as JSON and JSON converts tuples into lists.
        """

        if isinstance(
            priority_key,
            tuple,
        ):
            return tuple(
                PriorityQueue._normalize_priority_key(
                    value
                )
                for value in priority_key
            )

        if isinstance(
            priority_key,
            list,
        ):
            return tuple(
                PriorityQueue._normalize_priority_key(
                    value
                )
                for value in priority_key
            )

        return priority_key
