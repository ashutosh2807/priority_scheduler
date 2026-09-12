from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional


@dataclass
class ExecutionJob:
    """One persisted Oracle execution attempt."""

    id: Optional[int] = None
    job_id: Optional[int] = None
    job_name: Optional[str] = None
    procedure_name: Optional[str] = None
    report_date: Optional[date] = None
    status: str = "RUNNING"
    attempt_no: int = 1
    count: Optional[int] = None
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    error_type: Optional[str] = None
    duration_seconds: Optional[float] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
