"""Background worker that processes image-to-3D generation jobs one at a time.

Design notes
------------
* A single Python thread pulls jobs from an unbounded FIFO queue.
* Concurrency is deliberately limited to 1 to avoid CUDA context conflicts
  (the inference pipeline is NOT thread-safe and requires ~32 GB VRAM).
* The Inference model is loaded once on first use (lazy) so the API server
  starts fast even when GPU/model-weights are not immediately available.
* To cancel a *queued* job, add its id to the cancel-set before it is
  dequeued; running jobs cannot be interrupted in v1.
"""

from __future__ import annotations

import os
import queue
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import logging

import numpy as np
from PIL import Image

from .models import JobStatus
from .store import job_store

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (overridable via env vars)
# ---------------------------------------------------------------------------
SAM3D_CONFIG_FILE = os.environ.get("SAM3D_CONFIG_FILE", "checkpoints/hf/pipeline.yaml")
STORAGE_ROOT = Path(os.environ.get("SAM3D_STORAGE_ROOT", "storage"))

# ---------------------------------------------------------------------------
# Module-level singletons
# ---------------------------------------------------------------------------
_job_queue: queue.Queue[Optional[str]] = queue.Queue()
_cancel_set: set = set()
_cancel_lock = threading.Lock()
_worker_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()

_inference: Optional[Any] = None
_inference_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Inference loader (lazy, thread-safe)
# ---------------------------------------------------------------------------
def _load_inference():
    """Load the Inference wrapper once.  Heavy imports happen here so the
    API process starts without touching CUDA / model weights."""
    global _inference

    # notebook/inference.py sets os.environ["CUDA_HOME"] = os.environ["CONDA_PREFIX"]
    # at import time.  Ensure CONDA_PREFIX exists for uv-based environments.
    if "CONDA_PREFIX" not in os.environ:
        fallback = os.environ.get("CUDA_HOME", "/usr/local/cuda")
        os.environ["CONDA_PREFIX"] = fallback
        logger.warning(
            "CONDA_PREFIX not set; using fallback '{}' for CUDA_HOME", fallback
        )

    config_path = Path(SAM3D_CONFIG_FILE)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Pipeline config not found at '{config_path.resolve()}'. "
            "Set the SAM3D_CONFIG_FILE environment variable to the correct path."
        )

    # Add notebook/ directory to path so `from inference import Inference` works
    notebook_dir = str(Path(__file__).parent.parent / "notebook")
    if notebook_dir not in sys.path:
        sys.path.insert(0, notebook_dir)

    from inference import Inference  # type: ignore[import]

    logger.info("Loading inference model from {} …", config_path)
    _inference = Inference(str(config_path), compile=False)
    logger.info("Inference model ready.")


def _get_inference() -> Any:
    with _inference_lock:
        if _inference is None:
            _load_inference()
    return _inference


# ---------------------------------------------------------------------------
# Queue helpers (public API used by routes)
# ---------------------------------------------------------------------------
def submit(job_id: str) -> int:
    """Enqueue a job and return its approximate 1-based position."""
    _job_queue.put(job_id)
    return _job_queue.qsize()


def cancel(job_id: str) -> bool:
    """Mark a queued job for cancellation.  Returns False when the job is
    already in a terminal / running state."""
    job = job_store.get(job_id)
    if job is None or job.status != JobStatus.QUEUED:
        return False
    with _cancel_lock:
        _cancel_set.add(job_id)
    job_store.update(
        job_id,
        status=JobStatus.CANCELED,
        finished_at=datetime.now(timezone.utc),
        progress_stage=None,
    )
    return True


def queue_position(job_id: str) -> Optional[int]:
    """Return the current 1-based position in the queue, or None if not queued."""
    items = list(_job_queue.queue)
    try:
        return items.index(job_id) + 1
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Core job processing
# ---------------------------------------------------------------------------
def _run_job(job_id: str) -> None:
    job = job_store.get(job_id)
    if job is None:
        return

    # Skip if canceled before dequeue
    with _cancel_lock:
        if job_id in _cancel_set:
            _cancel_set.discard(job_id)
            logger.info("[{}] Skipped (canceled before start)", job_id)
            return

    job_store.update(
        job_id,
        status=JobStatus.RUNNING,
        started_at=datetime.now(timezone.utc),
        progress_stage="loading_model",
    )
    logger.info("[{}] Starting (seed={})", job_id, job.seed)

    try:
        inference = _get_inference()

        # ---- Load inputs ------------------------------------------------
        job_store.update(job_id, progress_stage="preprocessing")
        inputs_dir = Path(job.inputs_dir)
        image = np.array(Image.open(inputs_dir / "image.png").convert("RGB"))

        mask_path = inputs_dir / "mask.png"
        if mask_path.exists():
            mask = np.array(Image.open(mask_path).convert("L")).astype(np.float32) / 255.0
        else:
            mask = np.ones(image.shape[:2], dtype=np.float32)

        # ---- Run pipeline -----------------------------------------------
        job_store.update(job_id, progress_stage="running_stage1")
        logger.info("[{}] Calling pipeline …", job_id)

        # Call the underlying pipeline directly so we can pass per-job flags
        preprocessed = inference.merge_mask_to_rgba(image, mask)
        output = inference._pipeline.run(  # noqa: SLF001
            preprocessed,
            None,
            job.seed,
            stage1_only=False,
            with_mesh_postprocess=job.generate_mesh,
            with_texture_baking=job.with_texture_baking,
            with_layout_postprocess=False,
            use_vertex_color=True,
            stage1_inference_steps=None,
            pointmap=None,
        )

        # ---- Save artifacts ---------------------------------------------
        outputs_dir = Path(job.outputs_dir)
        outputs_dir.mkdir(parents=True, exist_ok=True)

        job_store.update(job_id, progress_stage="saving_ply")
        ply_path = outputs_dir / "splat.ply"
        output["gs"].save_ply(str(ply_path))
        logger.info("[{}] PLY saved ({} bytes)", job_id, ply_path.stat().st_size)

        if job.generate_mesh and output.get("glb") is not None:
            job_store.update(job_id, progress_stage="saving_mesh")
            glb_path = outputs_dir / "mesh.glb"
            output["glb"].export(str(glb_path))
            logger.info("[{}] GLB saved ({} bytes)", job_id, glb_path.stat().st_size)

        job_store.update(
            job_id,
            status=JobStatus.SUCCEEDED,
            progress_stage="done",
            finished_at=datetime.now(timezone.utc),
        )
        logger.info("[{}] Completed successfully", job_id)

    except Exception as exc:
        short_msg = f"{type(exc).__name__}: {exc}"
        logger.error("[{}] Failed: {}\n{}", job_id, short_msg, traceback.format_exc())
        job_store.update(
            job_id,
            status=JobStatus.FAILED,
            progress_stage=None,
            error_message=short_msg[:500],
            finished_at=datetime.now(timezone.utc),
        )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------
def _worker_loop() -> None:
    logger.info("Background worker started (single-GPU mode)")
    while not _stop_event.is_set():
        try:
            job_id = _job_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        if job_id is None:  # shutdown sentinel
            break
        try:
            _run_job(job_id)
        finally:
            _job_queue.task_done()
    logger.info("Background worker stopped")


# ---------------------------------------------------------------------------
# Lifecycle (called from FastAPI lifespan)
# ---------------------------------------------------------------------------
def start_worker() -> None:
    global _worker_thread
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="sam3d-worker", daemon=True
    )
    _worker_thread.start()
    logger.info("Worker thread launched")


def shutdown_worker() -> None:
    logger.info("Shutting down worker …")
    _stop_event.set()
    _job_queue.put(None)  # wake up a blocked .get()
    if _worker_thread is not None:
        _worker_thread.join(timeout=10)
    logger.info("Worker shutdown complete")
