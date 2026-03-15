from contextlib import asynccontextmanager
from pathlib import Path

import fastapi
from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from .routes import router
from .worker import shutdown_worker, start_worker


@asynccontextmanager
async def _lifespan(app: FastAPI):
    start_worker()
    yield
    shutdown_worker()


def _resolve_swagger_static_dir() -> Path:
    """Resolve local Swagger UI assets from workspace/package locations."""
    required_files = ("swagger-ui.css", "swagger-ui-bundle.js")
    repo_root = Path(__file__).resolve().parent.parent

    # 1) Preferred: repository-local assets under ./static
    candidate_dirs = [
        repo_root / "static",
        Path(fastapi.__file__).resolve().parent / "static",
    ]

    # 2) Optional fallback: swagger_ui_bundle package assets
    try:
        import swagger_ui_bundle  # type: ignore[import]

        candidate_dirs.append(Path(swagger_ui_bundle.swagger_ui_path))
    except Exception:
        pass

    for static_dir in candidate_dirs:
        if all((static_dir / name).exists() for name in required_files):
            return static_dir

    raise RuntimeError(
        "Missing local Swagger UI assets. Expected both files in one of: "
        + ", ".join(str(p) for p in candidate_dirs)
    )


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
        docs_url=None,
    )

    static_dir = _resolve_swagger_static_dir()
    openapi_url = app.openapi_url or "/openapi.json"
    oauth2_redirect_url = app.swagger_ui_oauth2_redirect_url or "/docs/oauth2-redirect"

    app.mount("/static", StaticFiles(directory=str(static_dir)), name="swagger-static")

    @app.get("/docs", include_in_schema=False)
    async def custom_swagger_ui_html() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url=openapi_url,
            title=f"{app.title} - Swagger UI",
            oauth2_redirect_url=oauth2_redirect_url,
            swagger_css_url="/static/swagger-ui.css",
            swagger_js_url="/static/swagger-ui-bundle.js",
        )

    @app.get(oauth2_redirect_url, include_in_schema=False)
    async def swagger_ui_redirect() -> HTMLResponse:
        return get_swagger_ui_oauth2_redirect_html()

    app.include_router(router, prefix="/v1")
    return app
