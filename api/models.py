from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Job lifecycle states
# ---------------------------------------------------------------------------
class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


# ---------------------------------------------------------------------------
# Internal job record (not a Pydantic model – stored in memory dict)
# ---------------------------------------------------------------------------
@dataclass
class JobRecord:
    job_id: str
    status: JobStatus
    seed: Optional[int]
    generate_mesh: bool
    with_texture_baking: bool
    created_at: datetime
    inputs_dir: str = ""
    outputs_dir: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    progress_stage: Optional[str] = None
    error_message: Optional[str] = None


# ---------------------------------------------------------------------------
# API response schemas
# ---------------------------------------------------------------------------
class ArtifactInfo(BaseModel):
    url: str
    size: int  # bytes


class JobOut(BaseModel):
    job_id: str
    status: JobStatus
    progress_stage: Optional[str] = None
    queue_position: Optional[int] = None
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    duration_ms: Optional[int] = None
    error_message: Optional[str] = None


class JobListOut(BaseModel):
    total: int
    items: List[JobOut]


class JobResultOut(BaseModel):
    job_id: str
    artifacts: Dict[str, ArtifactInfo]
