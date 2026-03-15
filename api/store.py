from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple

from .models import JobRecord, JobStatus


class InMemoryJobStore:
    """Thread-safe in-memory store for job records."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: Dict[str, JobRecord] = {}

    def add(self, job: JobRecord) -> None:
        with self._lock:
            self._jobs[job.job_id] = job

    def get(self, job_id: str) -> Optional[JobRecord]:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **kwargs) -> Optional[JobRecord]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in kwargs.items():
                setattr(job, key, value)
            return job

    def list_all(
        self,
        status_filter: Optional[JobStatus] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Tuple[int, List[JobRecord]]:
        with self._lock:
            items = list(self._jobs.values())
        if status_filter is not None:
            items = [j for j in items if j.status == status_filter]
        items.sort(key=lambda j: j.created_at, reverse=True)
        total = len(items)
        start = (page - 1) * page_size
        return total, items[start : start + page_size]


# Module-level singleton shared across the application
job_store = InMemoryJobStore()
