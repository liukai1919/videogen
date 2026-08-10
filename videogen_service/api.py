from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock
from typing import AsyncIterator

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import ValidationError
from urllib.parse import quote, urlparse

from videogen_service.agent import (
    AgentService,
    AgentUnavailable,
    ChatClient,
    ChatNotFound,
    ChatRecord,
    ChatSummary,
    ToolBox,
)
from videogen_service.agents import UnknownAgent, build_manager
from videogen_service.assets import (
    MEDIA_TYPES,
    AssetError,
    AssetNotFound,
    AssetStore,
    AssetView,
    asset_view,
)
from videogen_service.config import ServiceConfig, load_config
from videogen_service.documents import DocumentError, extract_text
from videogen_service.export import build_project_export
from videogen_service.jobs import (
    COMPOSE_CAPABILITY_ID,
    JOB_MEDIA_TYPES,
    JobConflict,
    JobError,
    JobNotFound,
    JobRunner,
    JobService,
    JobSpec,
    JobView,
    LocalJobRunner,
)
from videogen_service.memory import MemoryEntry, MemoryNotFound, MemoryStore
from videogen_service.models import (
    AssetFromRenderRequest,
    ChatCreateRequest,
    ChatSendRequest,
    JobSaveAssetRequest,
    MemoryAddRequest,
    ProjectCreateRequest,
    ProjectRenderLink,
    RenderSpec,
    ScriptOptions,
    RenderSummary,
    RenderValidation,
    RenderValidationRequest,
    RenderView,
    ScriptRequest,
    ScriptResult,
)
from videogen_service.pipeline import (
    PipelineApproveRequest,
    PipelineConflict,
    PipelineNotFound,
    PipelineRejectRequest,
    PipelineRequest,
    PipelineService,
    PipelineView,
)
from videogen_service.projects import (
    ProjectConflict,
    ProjectError,
    ProjectNotFound,
    ProjectRecord,
    ProjectStore,
    ProjectSummary,
)
from videogen_service.braingate import BrainGate
from videogen_service.renderer import (
    H3Renderer,
    RenderError,
    Renderer,
    mode_capability_schema,
)
from videogen_service.scripting import ScriptError, ScriptStudio, ScriptUnavailable
from videogen_service.service import RenderConflict, RenderNotFound, RenderService
from videogen_service.skills import SkillLibrary

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_MAX_REFERENCE_BYTES = 25 * 1024 * 1024
_MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
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
    job_runner: JobRunner | None = None,
    chat_client: ChatClient | None = None,
) -> FastAPI:
    settings = config or load_config()
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    skills = SkillLibrary(settings.skills_dir)
    memory = MemoryStore(settings.work_dir)
    # 本地大脑在途生成的守卫:渲染启动的 unload 等它说完话再卸模型。
    brain_gate = BrainGate()
    workshop = studio or ScriptStudio(
        settings, skills=skills, memory=memory, brain_gate=brain_gate
    )
    gpu_gate = Lock()
    service = RenderService(
        settings,
        renderer or H3Renderer(settings, brain_gate=brain_gate),
        studio=workshop,
        gpu_gate=gpu_gate,
    )
    projects = ProjectStore(settings.work_dir)
    assets = AssetStore(settings.work_dir)
    pipeline = PipelineService(
        settings, renders=service, projects=projects, studio=workshop
    )
    jobs = JobService(
        settings,
        runner=job_runner or LocalJobRunner(settings, renders=service),
        gpu_gate=gpu_gate,
    )
    # 外部 Agent worker 层(Claude Code/Codex):云端推理,不碰渲染执行。
    agent_workers = build_manager(settings)
    agent = AgentService(
        settings,
        toolbox=ToolBox(
            settings,
            renders=service,
            jobs=jobs,
            studio=workshop,
            assets=assets,
            memory=memory,
            skills=skills,
        ),
        client=chat_client,
        memory=memory,
        workers=agent_workers,
        brain_gate=brain_gate,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            # The pipeline submits renders, so it stops taking work first.
            pipeline.close()
            jobs.close()
            service.close()

    app = FastAPI(title="VideoTube Videogen", version="0.1.0", lifespan=lifespan)
    app.state.render_service = service
    app.state.agent_workers = agent_workers

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

    @app.get("/workspace", response_class=HTMLResponse)
    def workspace() -> FileResponse:
        return _static_file("workspace.html", "text/html; charset=utf-8")

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
                "version": skill.meta.version,
                "use_when": skill.meta.use_when,
                "not_for": skill.meta.not_for,
                "references": skill.references,
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

    @app.post("/v1/scripts/document", response_model=ScriptResult)
    async def script_from_document(
        file: UploadFile = File(...),
        target_seconds: float | None = Form(None),
        shot_seconds: float | None = Form(None),
        max_shots: int | None = Form(None),
        guidance: str | None = Form(None),
        style: str | None = Form(None),
        output_language: str | None = Form(None),
        skill: str | None = Form(None),
        project_id: str | None = Form(None),
    ) -> ScriptResult:
        # 本地文档(PDF/Word/文本)起稿:抽文本在本机完成,之后走和 YouTube
        # 字幕完全相同的 Ollama 总结路径。
        payload: dict[str, object] = {}
        for key, value in (
            ("target_seconds", target_seconds),
            ("shot_seconds", shot_seconds),
            ("max_shots", max_shots),
            ("guidance", guidance),
            ("style", style),
            ("output_language", output_language),
            ("skill", skill),
            ("project_id", project_id),
        ):
            if value is not None and str(value).strip():
                payload[key] = value
        try:
            options = ScriptOptions.model_validate(payload)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=error.errors()) from error
        if not file.filename:
            raise HTTPException(status_code=400, detail="需要一个文档文件")
        data = await file.read(_MAX_DOCUMENT_BYTES + 1)
        if len(data) > _MAX_DOCUMENT_BYTES:
            raise HTTPException(status_code=413, detail="文档不能超过 20MB")
        filename = Path(file.filename).name
        try:
            text = extract_text(filename, data)
            if options.project_id is not None:
                projects.get(options.project_id)
            result = workshop.create_from_document(
                options, filename=filename, text=text
            )
            if options.project_id is not None:
                projects.add_draft(
                    options.project_id, request=options, result=result
                )
            return result
        except DocumentError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ScriptUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        except (ScriptError, RenderError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/projects/{project_id}/export")
    def export_project(project_id: str) -> Response:
        try:
            project = projects.get(project_id)
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        filename, payload = build_project_export(
            project, config=settings, renders=service
        )
        return Response(
            content=payload,
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f"attachment; filename*=UTF-8''{quote(filename)}"
                ),
                "Cache-Control": "no-cache",
            },
        )

    @app.post(
        "/v1/chats", response_model=ChatRecord, status_code=status.HTTP_201_CREATED
    )
    def create_chat(request: ChatCreateRequest) -> ChatRecord:
        return agent.create(title=request.title)

    @app.get("/v1/chats", response_model=list[ChatSummary])
    def list_chats() -> list[ChatSummary]:
        return agent.list()

    @app.get("/v1/chats/{chat_id}", response_model=ChatRecord)
    def get_chat(chat_id: str) -> ChatRecord:
        try:
            return agent.get(chat_id)
        except ChatNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/v1/chats/{chat_id}/messages", response_model=ChatRecord)
    def send_chat(chat_id: str, request: ChatSendRequest) -> ChatRecord:
        # One agent turn can take minutes of local model time; this sync
        # handler runs on the worker threadpool like /v1/scripts does.
        try:
            return agent.send(chat_id, request.text, agent=request.agent)
        except ChatNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except UnknownAgent as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except AgentUnavailable as error:
            raise HTTPException(status_code=503, detail=str(error)) from error

    @app.delete("/v1/chats/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_chat(chat_id: str) -> Response:
        try:
            agent.delete(chat_id)
        except ChatNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/capabilities")
    def list_capabilities() -> list[dict[str, object]]:
        """The platform's catalog: what this machine can generate and where to
        submit it. H3 video modes ride the frozen render seam; everything else
        goes through /v1/jobs."""
        catalog: list[dict[str, object]] = [
            {
                "capability_id": mode,
                "kind": mode,
                "output": "video",
                "submit_via": "renders",
                "needs_gpu": True,
                # 参数事实表:与 validate_render 同源,替代提示词硬编码。
                "schema": mode_capability_schema(
                    settings.modes[mode], config=settings
                ),
            }
            for mode in sorted(settings.modes)
        ]
        catalog += [
            {
                "capability_id": capability_id,
                "kind": capability.kind,
                "output": "image" if capability.kind == "t2i" else "audio",
                "submit_via": "jobs",
                "needs_gpu": capability.needs_gpu,
            }
            for capability_id, capability in sorted(
                settings.capabilities.items()
            )
        ]
        # 成片合成是内建能力:渲染视频 + 配音 + 字幕 → FFmpeg 出成片。
        catalog.append(
            {
                "capability_id": COMPOSE_CAPABILITY_ID,
                "kind": "compose",
                "output": "video",
                "submit_via": "jobs",
                "needs_gpu": False,
            }
        )
        return catalog

    @app.get("/v1/agents")
    async def list_agent_workers() -> list[dict[str, object]]:
        """外部 Agent worker 目录:配置了谁、CLI 是否真的装好能跑。"""
        health = await agent_workers.health_check()
        return [
            {
                "agent": name,
                "enabled": worker.enabled,
                "available": health.get(name, False),
                "timeout_seconds": worker.timeout_seconds,
                "primary": name == settings.agent_defaults.primary,
            }
            for name, worker in sorted(settings.agents.items())
        ]

    @app.post(
        "/v1/jobs", response_model=JobView, status_code=status.HTTP_202_ACCEPTED
    )
    def submit_job(spec: JobSpec) -> JobView:
        try:
            return jobs.submit(spec)
        except JobConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except JobError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/jobs", response_model=list[JobView])
    def list_jobs() -> list[JobView]:
        return jobs.list()

    @app.get("/v1/jobs/{job_id}", response_model=JobView)
    def get_job(job_id: str) -> JobView:
        try:
            return jobs.get(job_id)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.api_route(
        "/v1/jobs/{job_id}/media", methods=["GET", "HEAD"], name="job_media"
    )
    def job_media(job_id: str) -> FileResponse:
        try:
            path = jobs.media_path(job_id)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(
            path,
            media_type=JOB_MEDIA_TYPES.get(
                path.suffix.lower(), "application/octet-stream"
            ),
            filename=f"{job_id}{path.suffix}",
            content_disposition_type="inline",
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.post(
        "/v1/jobs/{job_id}/retry",
        response_model=JobView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_job(job_id: str) -> JobView:
        try:
            return jobs.retry(job_id)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except JobConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.delete("/v1/jobs/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_job(job_id: str) -> Response:
        try:
            jobs.delete(job_id)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except JobConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/v1/jobs/{job_id}/save-asset",
        response_model=AssetView,
        status_code=status.HTTP_201_CREATED,
    )
    def save_job_asset(job_id: str, request: JobSaveAssetRequest) -> AssetView:
        """Archives a finished image job's output into the asset center."""
        try:
            path = jobs.media_path(job_id)
        except JobNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        try:
            return asset_view(
                assets.create(
                    name=request.name,
                    category=request.category,
                    filename=path.name,
                    data=path.read_bytes(),
                )
            )
        except (AssetError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/v1/memory", response_model=list[MemoryEntry])
    def list_memory() -> list[MemoryEntry]:
        return memory.list()

    @app.post(
        "/v1/memory",
        response_model=MemoryEntry,
        status_code=status.HTTP_201_CREATED,
    )
    def add_memory(request: MemoryAddRequest) -> MemoryEntry:
        return memory.add(request.text)

    @app.delete("/v1/memory/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_memory(entry_id: str) -> Response:
        try:
            memory.remove(entry_id)
        except MemoryNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get("/v1/assets", response_model=list[AssetView])
    def list_assets() -> list[AssetView]:
        return [asset_view(record) for record in assets.list()]

    @app.post(
        "/v1/assets", response_model=AssetView, status_code=status.HTTP_201_CREATED
    )
    async def create_asset(
        name: str = Form(..., min_length=1, max_length=120),
        category: str = Form("", max_length=40),
        file: UploadFile = File(...),
    ) -> AssetView:
        upload = await _read_upload(file)
        if upload is None:
            raise HTTPException(status_code=400, detail="资产需要一张图片")
        filename, data = upload
        try:
            return asset_view(
                assets.create(
                    name=name, category=category, filename=filename, data=data
                )
            )
        except AssetError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/v1/assets/from-render",
        response_model=AssetView,
        status_code=status.HTTP_201_CREATED,
    )
    def create_asset_from_render(request: AssetFromRenderRequest) -> AssetView:
        try:
            path = service.frame_path(request.render_id, request.slot)
            return asset_view(
                assets.create(
                    name=request.name,
                    category=request.category,
                    filename=path.name,
                    data=path.read_bytes(),
                )
            )
        except RenderNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (AssetError, OSError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.api_route(
        "/v1/assets/{asset_id}/media", methods=["GET", "HEAD"], name="asset_media"
    )
    def asset_media(asset_id: str) -> FileResponse:
        try:
            path = assets.media_path(asset_id)
        except AssetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return FileResponse(
            path,
            media_type=MEDIA_TYPES.get(path.suffix.lower(), "image/png"),
            headers={"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"},
        )

    @app.delete("/v1/assets/{asset_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_asset(asset_id: str) -> Response:
        try:
            assets.delete(asset_id)
        except AssetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except AssetError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/v1/projects/{project_id}/pipeline",
        response_model=PipelineView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def start_pipeline(project_id: str, request: PipelineRequest) -> PipelineView:
        try:
            return pipeline.start(project_id, request)
        except ProjectNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PipelineConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/v1/projects/{project_id}/pipeline", response_model=PipelineView)
    def get_pipeline(project_id: str) -> PipelineView:
        try:
            return pipeline.get(project_id)
        except PipelineNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/v1/projects/{project_id}/pipeline/approve",
        response_model=PipelineView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def approve_pipeline(
        project_id: str, request: PipelineApproveRequest
    ) -> PipelineView:
        try:
            return pipeline.approve(project_id, prompt=request.prompt)
        except PipelineNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (PipelineConflict, RenderConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except RenderError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/v1/projects/{project_id}/pipeline/reject", response_model=PipelineView
    )
    def reject_pipeline(
        project_id: str, request: PipelineRejectRequest
    ) -> PipelineView:
        try:
            return pipeline.reject(project_id, reason=request.reason)
        except PipelineNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except PipelineConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/v1/projects/{project_id}/pipeline/retry",
        response_model=PipelineView,
        status_code=status.HTTP_202_ACCEPTED,
    )
    def retry_pipeline(project_id: str) -> PipelineView:
        try:
            return pipeline.retry(project_id)
        except (PipelineNotFound, RenderNotFound) as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (PipelineConflict, RenderConflict) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.delete(
        "/v1/projects/{project_id}/pipeline",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_pipeline(project_id: str) -> Response:
        try:
            pipeline.delete(project_id)
        except PipelineNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return Response(status_code=status.HTTP_204_NO_CONTENT)

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
        # Additive, optional: reference frames straight from the asset center.
        # The sibling control plane never sends these, so the frozen contract
        # is untouched; an uploaded file wins over an asset id.
        first_frame_asset: str | None = Form(None),
        last_frame_asset: str | None = Form(None),
        # r2v 的身份参考图:资产 id,逗号分隔,顺序即 <Picture 1>…<Picture N>。
        ref_assets: str | None = Form(None),
        # 挂到分镜每一段的参考图(风格钥匙等):资产 id,逗号分隔。
        segment_ref_assets: str | None = Form(None),
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
            refs = [
                frame
                for asset_id in (ref_assets or "").split(",")
                if asset_id.strip()
                and (frame := _asset_frame(assets, asset_id.strip())) is not None
            ]
            segment_refs = [
                frame
                for asset_id in (segment_ref_assets or "").split(",")
                if asset_id.strip()
                and (frame := _asset_frame(assets, asset_id.strip())) is not None
            ]
            return service.submit(
                spec,
                first_frame=await _read_upload(first_frame)
                or _asset_frame(assets, first_frame_asset),
                last_frame=await _read_upload(last_frame)
                or _asset_frame(assets, last_frame_asset),
                refs=refs or None,
                segment_refs=segment_refs or None,
            )
        except AssetNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
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

    @app.post("/v1/renders/{render_id}/cancel", response_model=RenderView)
    def cancel_render(render_id: str) -> RenderView:
        # 冻结契约不动:新增端点,终态沿用 FAILED + 可读错误文案。
        try:
            return service.cancel(render_id)
        except RenderNotFound as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except RenderConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

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


def _asset_frame(
    assets: AssetStore, asset_id: str | None
) -> tuple[str, bytes] | None:
    """Loads an asset as if it had been uploaded; the render keeps its own copy
    of the bytes, so deleting the asset later never breaks the render."""
    if not asset_id or not asset_id.strip():
        return None
    path = assets.media_path(asset_id.strip())
    return path.name, path.read_bytes()
