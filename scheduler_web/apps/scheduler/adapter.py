from abc import ABC, abstractmethod


class SchedulerAdapter(ABC):
    """Django's narrow contract for a remote scheduler service."""

    @abstractmethod
    def get_status(self): ...

    @abstractmethod
    def get_jobs(self): ...

    @abstractmethod
    def get_job(self, job_id): ...

    @abstractmethod
    def get_queue(self): ...

    @abstractmethod
    def get_executions(self): ...

    @abstractmethod
    def control(self, job_id, action, override_datetime=None): ...
