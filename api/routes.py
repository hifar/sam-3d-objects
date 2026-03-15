"""All REST endpoints for the SAM 3D Objects generation API.

Endpoint summary
-----------------
POST   /v1/jobs                             Create a job (upload image → enqueue)
GET    /v1/jobs                             List jobs (filterable by status, paginated)
GET    /v1/jobs/{job_id}                    Get job status
GET    /v1/jobs/{job_id}/result             Get result artifact manifest
GET    /v1/jobs/{job_id}/artifacts/ply      Download splat.ply
GET    /v1/jobs/{job_id}/artifacts/mesh_glb Download mesh.glb (optional)
DELETE /v1/jobs/{job_id}                    Cancel a queued job
GET    /v1/health                           Health check
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Security, UploadFile, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from . import worker
from .app_config import load_app_config
from .models import ArtifactInfo, JobListOut, JobOut, JobRecord, JobResultOut, JobStatus
from .store import job_store
from .worker import STORAGE_ROOT

router = APIRouter()

_cfg = load_app_config()
_bearer = HTTPBearer(auto_error=False)


def _require_auth(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    """FastAPI dependency that enforces Bearer-Token auth when AuthMode=true."""
    if not _cfg.auth_mode:
        return
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header. Expected: Bearer <token>",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if credentials.credentials not in _cfg.api_keys:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API key",
        )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
_MAX_IMAGE_MB = 20
_MAX_IMAGE_BYTES = _MAX_IMAGE_MB * 1024 * 1024


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _to_out(job: JobRecord) -> JobOut:
    duration_ms: Optional[int] = None
    if job.started_at and job.finished_at:
        duration_ms = int((job.finished_at - job.started_at).total_seconds() * 1000)

    queue_pos: Optional[int] = None
    if job.status == JobStatus.QUEUED:
        queue_pos = worker.queue_position(job.job_id)

    return JobOut(
        job_id=job.job_id,
        status=job.status,
        progress_stage=job.progress_stage,
        queue_position=queue_pos,
        created_at=job.created_at.isoformat(),
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
        duration_ms=duration_ms,
        error_message=job.error_message,
    )


def _require_job(job_id: str) -> JobRecord:
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


def _require_succeeded(job: JobRecord) -> None:
    if job.status != JobStatus.SUCCEEDED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is not completed yet (current status: {job.status})",
        )


# ---------------------------------------------------------------------------
# Utility endpoints
# ---------------------------------------------------------------------------
@router.get("/health", tags=["Utility"])
def health() -> dict:
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


# ---------------------------------------------------------------------------
# Job endpoints
# ---------------------------------------------------------------------------
@router.post(
    "/jobs",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobOut,
    tags=["Jobs"],
    dependencies=[Depends(_require_auth)],
    summary="Upload image and create a 3D generation job",
    description=(
        "Upload a PNG/JPEG/WebP image (and an optional binary mask) to start an "
        "asynchronous Gaussian Splat generation job.  Poll **GET /v1/jobs/{job_id}** "
        "until `status` is `succeeded`, then download the PLY via "
        "**GET /v1/jobs/{job_id}/artifacts/ply**."
    ),
)
async def create_job(
    image: UploadFile = File(..., description="Input image (PNG / JPEG / WebP, ≤ 20 MB)"),
    mask: Optional[UploadFile] = File(
        None,
        description="Optional binary mask – white = foreground (same format as image).",
    ),
    seed: Optional[int] = Form(None, description="Random seed for reproducibility"),
    generate_mesh: bool = Form(
        False,
        description="Also generate mesh.glb (adds significant processing time)",
    ),
    with_texture_baking: bool = Form(
        False,
        description="Bake textures into GLB (very slow, only relevant when generate_mesh=true)",
    ),
) -> JobOut:
    # ---- Validate image --------------------------------------------------
    if image.content_type not in _ALLOWED_IMAGE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Unsupported image content-type '{image.content_type}'. "
                f"Allowed: {sorted(_ALLOWED_IMAGE_TYPES)}"
            ),
        )
    image_bytes = await image.read()
    if len(image_bytes) > _MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Image exceeds the {_MAX_IMAGE_MB} MB size limit",
        )

    # ---- Validate mask (optional) ----------------------------------------
    mask_bytes: Optional[bytes] = None
    if mask is not None:
        if mask.content_type not in _ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unsupported mask content-type '{mask.content_type}'",
            )
        mask_bytes = await mask.read()

    # ---- Create per-job storage layout -----------------------------------
    job_id = str(uuid.uuid4())
    job_dir = STORAGE_ROOT / "jobs" / job_id
    inputs_dir = job_dir / "inputs"
    outputs_dir = job_dir / "outputs"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)

    (inputs_dir / "image.png").write_bytes(image_bytes)
    if mask_bytes is not None:
        (inputs_dir / "mask.png").write_bytes(mask_bytes)

    # ---- Register job record --------------------------------------------
    job = JobRecord(
        job_id=job_id,
        status=JobStatus.QUEUED,
        seed=seed,
        generate_mesh=generate_mesh,
        with_texture_baking=with_texture_baking,
        created_at=datetime.now(timezone.utc),
        inputs_dir=str(inputs_dir),
        outputs_dir=str(outputs_dir),
    )
    job_store.add(job)

    # ---- Enqueue --------------------------------------------------------
    position = worker.submit(job_id)
    job_store.update(job_id, progress_stage=f"queued (position {position})")

    return _to_out(job)


@router.get(
    "/jobs",
    response_model=JobListOut,
    tags=["Jobs"],
    dependencies=[Depends(_require_auth)],
    summary="List all jobs (paginated, filterable by status)",
)
def list_jobs(
    job_status: Optional[JobStatus] = Query(
        None, alias="status", description="Filter by job status"
    ),
    page: int = Query(1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(20, ge=1, le=100, description="Items per page"),
) -> JobListOut:
    total, items = job_store.list_all(
        status_filter=job_status, page=page, page_size=page_size
    )
    return JobListOut(total=total, items=[_to_out(j) for j in items])


@router.get(
    "/jobs/{job_id}",
    response_model=JobOut,
    tags=["Jobs"],
    dependencies=[Depends(_require_auth)],
    summary="Get job status and progress",
)
def get_job(job_id: str) -> JobOut:
    return _to_out(_require_job(job_id))


@router.delete(
    "/jobs/{job_id}",
    status_code=status.HTTP_200_OK,
    tags=["Jobs"],
    dependencies=[Depends(_require_auth)],
    summary="Cancel a queued job",
    description="Only jobs in `queued` state can be canceled. Running jobs cannot be interrupted.",
)
def cancel_job(job_id: str) -> dict:
    job = _require_job(job_id)
    if job.status == JobStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot cancel a running job. Wait for it to finish.",
        )
    if job.status in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Job is already in terminal state: {job.status}",
        )
    worker.cancel(job_id)
    return {"job_id": job_id, "status": "canceled"}


# ---------------------------------------------------------------------------
# Result & artifact endpoints
# ---------------------------------------------------------------------------
@router.get(
    "/jobs/{job_id}/result",
    response_model=JobResultOut,
    tags=["Artifacts"],
    dependencies=[Depends(_require_auth)],
    summary="Get the artifact manifest of a completed job",
)
def get_result(job_id: str) -> JobResultOut:
    job = _require_job(job_id)
    _require_succeeded(job)

    artifacts: dict[str, ArtifactInfo] = {}
    outputs_dir = Path(job.outputs_dir)

    ply_path = outputs_dir / "splat.ply"
    if ply_path.exists():
        artifacts["ply"] = ArtifactInfo(
            url=f"/v1/jobs/{job_id}/artifacts/ply",
            size=ply_path.stat().st_size,
        )

    glb_path = outputs_dir / "mesh.glb"
    if glb_path.exists():
        artifacts["mesh_glb"] = ArtifactInfo(
            url=f"/v1/jobs/{job_id}/artifacts/mesh_glb",
            size=glb_path.stat().st_size,
        )

    return JobResultOut(job_id=job_id, artifacts=artifacts)


@router.get(
    "/jobs/{job_id}/artifacts/ply",
    tags=["Artifacts"],
    dependencies=[Depends(_require_auth)],
    summary="Download the Gaussian Splat PLY file",
)
def download_ply(job_id: str) -> FileResponse:
    job = _require_job(job_id)
    _require_succeeded(job)

    ply_path = Path(job.outputs_dir) / "splat.ply"
    if not ply_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="PLY artifact not found on disk",
        )
    return FileResponse(
        path=str(ply_path),
        media_type="application/octet-stream",
        filename=f"splat_{job_id[:8]}.ply",
    )


@router.get(
    "/jobs/{job_id}/artifacts/mesh_glb",
    tags=["Artifacts"],
    dependencies=[Depends(_require_auth)],
    summary="Download the mesh GLB file (only available when generate_mesh=true)",
)
def download_glb(job_id: str) -> FileResponse:
    job = _require_job(job_id)
    _require_succeeded(job)

    glb_path = Path(job.outputs_dir) / "mesh.glb"
    if not glb_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="GLB artifact not found. Was generate_mesh=true when submitting?",
        )
    return FileResponse(
        path=str(glb_path),
        media_type="model/gltf-binary",
        filename=f"mesh_{job_id[:8]}.glb",
    )
