from contextlib import asynccontextmanager

from fastapi import FastAPI

from .routes import router
from .worker import shutdown_worker, start_worker


@asynccontextmanager
async def _lifespan(app: FastAPI):
    start_worker()
    yield
    shutdown_worker()


def create_app() -> FastAPI:
    app = FastAPI(
        title="SAM 3D Objects API",
        version="1.0.0",
        description=(
            "Upload an image to generate a 3D Gaussian Splat (**.ply**).  "
            "Generation is asynchronous—submit a job, poll its status, then "
            "download the artifact once `status` is `succeeded`."
        ),
        lifespan=_lifespan,
    )
    app.include_router(router, prefix="/v1")
    return app
