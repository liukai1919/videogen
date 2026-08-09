from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import ValidationError
from urllib.parse import urlparse

from videogen_service.config import ServiceConfig, load_config
from videogen_service.models import (
    ProjectCreateRequest,
    ProjectRenderLink,
    RenderSpec,
    RenderSummary,
    RenderValidation,
    RenderValidationRequest,
    RenderView,
    ScriptRequest,
    ScriptResult,
)
from videogen_service.projects import (
    ProjectConflict,
    ProjectError,
    ProjectNotFound,
    ProjectRecord,
    ProjectStore,
    ProjectSummary,
)
from videogen_service.renderer import H3Renderer, RenderError, Renderer
from videogen_service.scripting import ScriptError, ScriptStudio, ScriptUnavailable
from videogen_service.service import RenderConflict, RenderNotFound, RenderService
from videogen_service.skills import SkillLibrary

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_REFERENCE_BYTES = 25 * 1024 * 1024
_STATIC_DIR = Path(__file__).parent / "static"
_STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


def _static_file(name: str, media_type: str) -> FileResponse:
    # Serving by name from a fixed table keeps the console's few assets out of
    # reach of path traversal without pulling in a whole static-files mount.
    path = _STATIC_DIR / name
    if Path(name).name != name or not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


def create_app(
    config: ServiceConfig | None = None,
    *,
    renderer: Renderer | None = None,
    studio: ScriptStudio | None = None,
) -> FastAPI:
    settings = config or load_config()
    skills = SkillLibrary(settings.skills_dir)
    service = RenderService(
        settings,
        renderer or H3Renderer(settings),
        studio=studio or ScriptStudio(settings, skills=skills),
    )
    projects = ProjectStore(settings.work_dir)

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

    @app.get("/", response_class=HTMLResponse)
    def console() -> FileResponse:
        return _static_file("videogen.html", "text/html; charset=utf-8")

    @app.get("/static/{name}")
    def static_file(name: str) -> FileResponse:
        media_type = _STATIC_TYPES.get(Path(name).suffix)
        if media_type is None:
            raise HTTPException(status_code=404, detail="not found")
        return _static_file(name, media_type)

    @app.get("/health")
    def health() -> dict[str, object]:
        return service.health()

    @app.post("/v1/validate", response_model=RenderValidation)
    def validate(request: RenderValidationRequest) -> RenderValidation:
        try:
            return service.validate(request)
        except RenderError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/scripts/config")
    def script_config() -> dict[str, object]:
        return service.script_config()

    @app.post("/v1/scripts", response_model=ScriptResult)
    def script(request: ScriptRequest) -> ScriptResult:
        # Captions and the local LLM both take minutes; FastAPI runs this sync
        # handler on its worker threads, so the render queue keeps draining.
        try:
            if request.project_id is not None:
                # Checked up front so a typo'd project fails in milliseconds,
                # not after minutes of captions and Ollama.
                projects.get(request.project_id)
            result = service.script(request)
            if request.project_id is not None:
                projects.add_draft(
                    request.project_id, request=request, result=result
                )
            return result
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ScriptUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (ScriptError, RenderError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/skills")
    def list_skills() -> list[dict[str, object]]:
        return [
            {
                "skill_id": skill.skill_id,
                "name": skill.meta.name,
                "description": skill.meta.description,
                "category": skill.meta.category,
                "defaults": skill.meta.defaults.model_dump(exclude_none=True),
            }
            for skill in skills.list()
        ]

    @app.get("/v1/projects", response_model=list[ProjectSummary])
    def list_projects() -> list[ProjectSummary]:
        return projects.list()

    @app.post(
        "/v1/projects",
        response_model=ProjectRecord,
        status_code=status.HTTP_201_CREATED,
    )
    def create_project(request: ProjectCreateRequest) -> ProjectRecord:
        try:
            return projects.create(
                project_id=request.project_id, name=request.name
            )
        except ProjectConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ProjectError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/projects/{project_id}", response_model=ProjectRecord)
    def get_project(project_id: str) -> ProjectRecord:
        try:
            return projects.get(project_id)
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.delete(
        "/v1/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT
    )
    def delete_project(project_id: str) -> Response:
        try:
            projects.delete(project_id)
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ProjectConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/v1/projects/{project_id}/renders", response_model=ProjectRecord)
    def link_project_render(
        project_id: str, request: ProjectRenderLink
    ) -> ProjectRecord:
        try:
            # The render must exist before it can be archived under a project;
            # service.get raises the same not-found the render routes use.
            service.get(request.render_id)
            return projects.link_render(project_id, request.render_id)
        except (ProjectNotFound, RenderNotFound) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

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

    @app.get("/v1/renders", response_model=list[RenderSummary])
    def list_renders() -> list[RenderSummary]:
        return service.list_renders()

    @app.post(
        "/v1/renders/{render_id}/retry",
        response_model=RenderView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_render(render_id: str) -> RenderView:
        try:
            return service.retry(render_id)
        except RenderNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RenderConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

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
