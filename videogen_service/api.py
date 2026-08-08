from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import ValidationError
from urllib.parse import urlparse

from videogen_service.config import ServiceConfig, load_config
from videogen_service.director import PromptDirector, UnknownDirector, build_directors
from videogen_service.models import (
    EnhanceRequest,
    EnhanceResponse,
    RenderSpec,
    RenderValidation,
    RenderValidationRequest,
    RenderView,
)
from videogen_service.renderer import H3Renderer, RenderError, Renderer
from videogen_service.service import (
    DirectorUnavailable,
    RenderConflict,
    RenderNotFound,
    RenderService,
)

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_REFERENCE_BYTES = 25 * 1024 * 1024


def _build_director(settings: ServiceConfig) -> PromptDirector | None:
    if settings.director is None or not settings.director.enabled:
        return None
    providers = build_directors(settings.director)
    if not providers:
        # Every configured writer failed to start. /v1/enhance answers 503 and
        # rendering carries on untouched.
        return None
    return PromptDirector(
        providers,
        settings=settings.director,
        renderer=settings.renderer,
        cache_dir=settings.work_dir / "prompts",
    )


def create_app(
    config: ServiceConfig | None = None,
    *,
    renderer: Renderer | None = None,
    director: PromptDirector | None = None,
) -> FastAPI:
    settings = config or load_config()
    service = RenderService(
        settings,
        renderer or H3Renderer(settings),
        director or _build_director(settings),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            service.close()

    app = FastAPI(title="VideoTube Videogen", version="0.1.0", lifespan=lifespan)
    app.state.render_service = service

    @app.middleware("http")
    async def refuse_cross_site_requests(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        origin = request.headers.get("origin")
        if origin is not None and urlparse(origin).hostname not in _LOOPBACK_HOSTS:
            return PlainTextResponse("cross-site request refused", status_code=403)
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, object]:
        return service.health()

    @app.post("/v1/validate", response_model=RenderValidation)
    def validate(request: RenderValidationRequest) -> RenderValidation:
        try:
            return service.validate(request)
        except RenderError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/v1/enhance", response_model=EnhanceResponse)
    def enhance(request: EnhanceRequest) -> EnhanceResponse:
        # Always 200 with something submittable when a director is configured:
        # a rewrite that fails verification comes back with its warnings, and a
        # provider outage comes back as the original prompt.
        try:
            return service.enhance(request)
        except UnknownDirector as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except DirectorUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.post(
        "/v1/renders",
        response_model=RenderView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit(
        render_id: str = Form(...),
        mode: str = Form(...),
        prompt: str = Form(...),
        width: int = Form(...),
        height: int = Form(...),
        seconds: float = Form(...),
        seed: int | None = Form(None),
        first_frame: UploadFile | None = File(None),
        last_frame: UploadFile | None = File(None),
    ) -> RenderView:
        try:
            spec = RenderSpec.model_validate(
                {
                    "render_id": render_id,
                    "mode": mode,
                    "prompt": prompt,
                    "width": width,
                    "height": height,
                    "seconds": seconds,
                    "seed": seed,
                }
            )
            return service.submit(
                spec,
                first_frame=await _read_upload(first_frame),
                last_frame=await _read_upload(last_frame),
            )
        except RenderError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RenderConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error

    @app.get("/v1/renders/{render_id}", response_model=RenderView)
    def get_render(render_id: str) -> RenderView:
        try:
            return service.get(render_id)
        except RenderNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.api_route(
        "/v1/renders/{render_id}/media", methods=["GET", "HEAD"], name="media"
    )
    def media(render_id: str) -> FileResponse:
        try:
            path = service.media_path(render_id)
        except RenderNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(
            path,
            media_type="video/mp4",
            filename=path.name,
            content_disposition_type="inline",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.delete(
        "/v1/renders/{render_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    def delete_render(render_id: str) -> Response:
        try:
            service.delete(render_id)
        except RenderNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RenderConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    return app


async def _read_upload(upload: UploadFile | None) -> tuple[str, bytes] | None:
    if upload is None or not upload.filename:
        return None
    data = await upload.read(_MAX_REFERENCE_BYTES + 1)
    if len(data) > _MAX_REFERENCE_BYTES:
        raise HTTPException(status_code=413, detail="参考图不能超过 25MB")
    return Path(upload.filename).name, data
