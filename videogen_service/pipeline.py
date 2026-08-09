# The pipeline runner: drives one project through 取字幕 → 出脚本 → 人工审阅 →
# 提交渲染 → 收产物, orchestrating only the machine's own components (yt-dlp,
# Ollama, ComfyUI) — no cloud API anywhere. The review gate is deliberate: the
# runner may propose, but only a person promotes a script into GPU time, the
# same power split the visual-director contract mandates.
#
# State lives in work/.projects/<project_id>/pipeline.json, one active run per
# project, written with the same atomic style as render records. The script
# stage runs on a worker thread; the render stage is delegated to the render
# queue and observed lazily on reads, so a service restart needs no watcher:
# RenderService's own recovery marks the render failed, and the next GET here
# sees it.

from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
import secrets
from threading import Event, Lock, Thread
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from videogen_service.artifacts import write_json_atomic
from videogen_service.config import ServiceConfig
from videogen_service.models import RenderSpec, RenderView, ScriptRequest
from videogen_service.projects import (
    PROJECTS_DIRNAME,
    ProjectDraft,
    ProjectNotFound,
    ProjectStore,
)
from videogen_service.scripting import ScriptStudio
from videogen_service.service import RenderNotFound, RenderService

PipelineStatus = Literal[
    "QUEUED",
    "SCRIPTING",
    "AWAITING_REVIEW",
    "RENDERING",
    "DONE",
    "REJECTED",
    "FAILED",
]
_ACTIVE: frozenset[str] = frozenset(
    {"QUEUED", "SCRIPTING", "AWAITING_REVIEW", "RENDERING"}
)


class PipelineError(RuntimeError):
    pass


class PipelineNotFound(PipelineError):
    pass


class PipelineConflict(PipelineError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PipelineRequest(StrictModel):
    """Everything one run needs up front: script knobs plus render framing."""

    url: str = Field(min_length=1, max_length=2_048)
    target_seconds: float | None = Field(default=None, gt=0)
    shot_seconds: float | None = Field(default=None, gt=0)
    max_shots: int | None = Field(default=None, gt=0)
    guidance: str | None = Field(default=None, max_length=2_000)
    style: str | None = Field(default=None, max_length=2_000)
    output_language: str | None = Field(default=None, min_length=1, max_length=40)
    skill: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    width: int = Field(default=864, ge=32, le=8192)
    height: int = Field(default=480, ge=32, le=8192)
    seed: int | None = Field(default=None, ge=0)


class PipelineReview(StrictModel):
    outcome: Literal["approved", "rejected"]
    reviewed_at: str
    reason: str | None = None
    # True when the reviewer edited the storyboard before approving; the
    # rendered prompt then differs from the archived draft.
    prompt_overridden: bool = False


class PipelineRecord(StrictModel):
    schema_version: Literal[1] = 1
    project_id: str
    request: PipelineRequest
    status: PipelineStatus
    draft_id: int | None = None
    render_id: str | None = None
    error: str | None = None
    review: PipelineReview | None = None
    created_at: str
    updated_at: str


class PipelineView(StrictModel):
    project_id: str
    status: PipelineStatus
    request: PipelineRequest
    draft_id: int | None = None
    render_id: str | None = None
    error: str | None = None
    review: PipelineReview | None = None
    created_at: str
    updated_at: str
    # Joined in for the console so one poll answers "what am I reviewing" and
    # "how is the render doing" without extra round-trips.
    draft: ProjectDraft | None = None
    render: RenderView | None = None


class PipelineApproveRequest(StrictModel):
    # The reviewer may hand back an edited storyboard; absent, the archived
    # draft renders as generated.
    prompt: str | None = Field(default=None, min_length=1, max_length=50_000)


class PipelineRejectRequest(StrictModel):
    reason: str | None = Field(default=None, max_length=2_000)


class PipelineService:
    """Owns the durable script→review→render flow, one run per project."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        renders: RenderService,
        projects: ProjectStore,
        studio: ScriptStudio,
    ) -> None:
        self._config = config
        self._renders = renders
        self._projects = projects
        self._studio = studio
        self._directory = config.work_dir / PROJECTS_DIRNAME
        self._lock = Lock()
        self._queue: Queue[str | None] = Queue()
        self._closing = Event()
        self._recover_interrupted()
        self._worker = Thread(
            target=self._work, daemon=True, name="videogen-pipeline-worker"
        )
        self._worker.start()

    def close(self) -> None:
        self._closing.set()
        while True:
            try:
                queued = self._queue.get_nowait()
            except Empty:
                break
            try:
                if queued is not None:
                    self._fail_for_shutdown(queued)
            finally:
                self._queue.task_done()
        self._queue.put(None)
        # The worker may be deep inside a minutes-long Ollama call. Unlike the
        # GPU render there is nothing to protect, so the daemon thread is
        # abandoned after a short grace and the interrupted record is turned
        # into a retryable failure by the next start's recovery.
        self._worker.join(timeout=2)

    def start(self, project_id: str, request: PipelineRequest) -> PipelineView:
        if self._closing.is_set():
            raise PipelineConflict("服务正在关闭")
        self._projects.get(project_id)  # 404 for a missing project
        with self._lock:
            existing = self._load(project_id)
            if existing is not None and existing.status in _ACTIVE:
                raise PipelineConflict(
                    f"项目已有一条进行中的流水线({existing.status})"
                )
            now = _now()
            self._save(
                PipelineRecord(
                    project_id=project_id,
                    request=request,
                    status="QUEUED",
                    created_at=now,
                    updated_at=now,
                )
            )
            self._queue.put(project_id)
        return self.get(project_id)

    def get(self, project_id: str) -> PipelineView:
        with self._lock:
            record = self._require(project_id)
            record = self._sync_render(record)
            draft = self._draft(record)
        render: RenderView | None = None
        if record.render_id is not None:
            try:
                render = self._renders.get(record.render_id)
            except RenderNotFound:
                render = None
        return PipelineView(
            **record.model_dump(exclude={"schema_version"}),
            draft=draft,
            render=render,
        )

    def approve(self, project_id: str, *, prompt: str | None) -> PipelineView:
        with self._lock:
            record = self._require(project_id)
            if record.status != "AWAITING_REVIEW":
                raise PipelineConflict(
                    f"流水线不在等待审阅状态({record.status})"
                )
            draft = self._draft(record)
            if draft is None:
                raise PipelineConflict("流水线的草稿不见了,请清除后重新起稿")
            final_prompt = prompt.strip() if prompt and prompt.strip() else None
            spec = RenderSpec(
                render_id=_mint_render_id(),
                mode=draft.result.mode,
                prompt=final_prompt or draft.result.prompt,
                width=record.request.width,
                height=record.request.height,
                seconds=draft.result.seconds,
                seed=record.request.seed,
            )
            # RenderError from validation (a bad hand-edit) propagates before
            # any state changes, so the run stays reviewable.
            self._renders.submit(spec, first_frame=None, last_frame=None)
            self._projects.link_render(project_id, spec.render_id)
            self._save(
                record.model_copy(
                    update={
                        "status": "RENDERING",
                        "render_id": spec.render_id,
                        "review": PipelineReview(
                            outcome="approved",
                            reviewed_at=_now(),
                            prompt_overridden=final_prompt is not None
                            and final_prompt != draft.result.prompt,
                        ),
                        "error": None,
                        "updated_at": _now(),
                    }
                )
            )
        return self.get(project_id)

    def reject(self, project_id: str, *, reason: str | None) -> PipelineView:
        with self._lock:
            record = self._require(project_id)
            if record.status != "AWAITING_REVIEW":
                raise PipelineConflict(
                    f"流水线不在等待审阅状态({record.status})"
                )
            self._save(
                record.model_copy(
                    update={
                        "status": "REJECTED",
                        "review": PipelineReview(
                            outcome="rejected", reviewed_at=_now(), reason=reason
                        ),
                        "updated_at": _now(),
                    }
                )
            )
        return self.get(project_id)

    def retry(self, project_id: str) -> PipelineView:
        if self._closing.is_set():
            raise PipelineConflict("服务正在关闭")
        with self._lock:
            record = self._require(project_id)
            if record.status != "FAILED":
                raise PipelineConflict(
                    f"只有失败的流水线可以重试({record.status})"
                )
            if record.render_id is not None:
                # The script survived; only the render needs another go.
                self._renders.retry(record.render_id)
                self._save(
                    record.model_copy(
                        update={
                            "status": "RENDERING",
                            "error": None,
                            "updated_at": _now(),
                        }
                    )
                )
            else:
                self._save(
                    record.model_copy(
                        update={
                            "status": "QUEUED",
                            "error": None,
                            "updated_at": _now(),
                        }
                    )
                )
                self._queue.put(project_id)
        return self.get(project_id)

    def delete(self, project_id: str) -> None:
        """Abandons the run. A render already on the GPU keeps going and stays
        linked to the project; a script stage in flight discards its result."""
        with self._lock:
            self._require(project_id)
            self._record_path(project_id).unlink(missing_ok=True)

    def _work(self) -> None:
        while True:
            project_id = self._queue.get()
            try:
                if project_id is None:
                    return
                if self._closing.is_set():
                    self._fail_for_shutdown(project_id)
                else:
                    self._run(project_id)
            finally:
                self._queue.task_done()

    def _run(self, project_id: str) -> None:
        with self._lock:
            record = self._load(project_id)
            if record is None or record.status != "QUEUED":
                return  # cancelled or replaced while waiting
            self._save(
                record.model_copy(
                    update={
                        "status": "SCRIPTING",
                        "error": None,
                        "updated_at": _now(),
                    }
                )
            )
        script_request = _script_request(record.request, project_id=project_id)
        try:
            result = self._studio.create(script_request)
            draft = self._projects.add_draft(
                project_id, request=script_request, result=result
            )
        except ProjectNotFound:
            return  # the project vanished mid-run; nowhere to put the draft
        except Exception as error:
            self._change(
                project_id,
                lambda run: run.model_copy(
                    update={
                        "status": "FAILED",
                        "error": f"{type(error).__name__}: {error}",
                        "updated_at": _now(),
                    }
                ),
            )
            return
        self._change(
            project_id,
            lambda run: run.model_copy(
                update={
                    "status": "AWAITING_REVIEW",
                    "draft_id": draft.draft_id,
                    "error": None,
                    "updated_at": _now(),
                }
            )
            if run.status == "SCRIPTING"
            else run,
        )

    def _sync_render(self, record: PipelineRecord) -> PipelineRecord:
        """Folds the render's terminal state into the run, lazily on reads."""
        if record.status != "RENDERING" or record.render_id is None:
            return record
        try:
            render = self._renders.get(record.render_id)
        except RenderNotFound:
            updated = record.model_copy(
                update={
                    "status": "FAILED",
                    "error": "渲染记录不见了,请清除后重新起稿",
                    "updated_at": _now(),
                }
            )
            self._save(updated)
            return updated
        if render.status == "DONE":
            updated = record.model_copy(
                update={"status": "DONE", "error": None, "updated_at": _now()}
            )
        elif render.status == "FAILED":
            updated = record.model_copy(
                update={
                    "status": "FAILED",
                    "error": render.error or "渲染失败",
                    "updated_at": _now(),
                }
            )
        else:
            return record
        self._save(updated)
        return updated

    def _draft(self, record: PipelineRecord) -> ProjectDraft | None:
        if record.draft_id is None:
            return None
        try:
            project = self._projects.get(record.project_id)
        except ProjectNotFound:
            return None
        for draft in project.drafts:
            if draft.draft_id == record.draft_id:
                return draft
        return None

    def _fail_for_shutdown(self, project_id: str) -> None:
        self._change(
            project_id,
            lambda run: run.model_copy(
                update={
                    "status": "FAILED",
                    "error": "服务关闭前未开始,请重试",
                    "updated_at": _now(),
                }
            ),
        )

    def _recover_interrupted(self) -> None:
        for path in self._directory.glob("*/pipeline.json"):
            record = self._read(path)
            if record is None or record.project_id != path.parent.name:
                continue
            if record.status in {"QUEUED", "SCRIPTING"}:
                self._save(
                    record.model_copy(
                        update={
                            "status": "FAILED",
                            "error": "服务重启中断了流水线,请重试",
                            "updated_at": _now(),
                        }
                    )
                )
            # AWAITING_REVIEW is pure state and keeps. RENDERING also keeps:
            # RenderService's own recovery failed the render, and the next
            # read folds that in through _sync_render.

    def _change(
        self, project_id: str, change: Callable[[PipelineRecord], PipelineRecord]
    ) -> None:
        with self._lock:
            record = self._load(project_id)
            if record is None:
                return
            self._save(change(record))

    def _require(self, project_id: str) -> PipelineRecord:
        record = self._load(project_id)
        if record is None:
            raise PipelineNotFound(f"项目 {project_id} 没有流水线")
        return record

    def _load(self, project_id: str) -> PipelineRecord | None:
        path = self._record_path(project_id)
        return self._read(path) if path.is_file() else None

    def _read(self, path: Path) -> PipelineRecord | None:
        try:
            return PipelineRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def _record_path(self, project_id: str) -> Path:
        return self._directory / project_id / "pipeline.json"

    def _save(self, record: PipelineRecord) -> None:
        write_json_atomic(
            self._record_path(record.project_id), record.model_dump(mode="json")
        )


def _script_request(request: PipelineRequest, *, project_id: str) -> ScriptRequest:
    """Only fields the run actually set are forwarded, so skill defaults keep
    applying exactly as they do on the manual /v1/scripts path."""
    payload: dict[str, object] = {"url": request.url, "project_id": project_id}
    for field in (
        "target_seconds",
        "shot_seconds",
        "max_shots",
        "guidance",
        "style",
        "output_language",
        "skill",
    ):
        value = getattr(request, field)
        if value is not None:
            payload[field] = value
    return ScriptRequest.model_validate(payload)


def _mint_render_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"pl_{stamp}_{secrets.token_hex(3)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
