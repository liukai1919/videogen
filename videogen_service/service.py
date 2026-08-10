from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
from threading import Event, Lock, Thread

from videogen_service.artifacts import write_bytes_atomic, write_json_atomic
from videogen_service.config import ServiceConfig
from videogen_service.models import (
    RenderProgress,
    RenderRecord,
    RenderRequest,
    RenderSpec,
    RenderSummary,
    RenderValidation,
    RenderValidationRequest,
    RenderView,
    ScriptRequest,
    ScriptResult,
)
from videogen_service.renderer import (
    LENGTH_OFFSET,
    LENGTH_STEP,
    SHOT_HEADER,
    RenderCancelled,
    Renderer,
    validate_render,
)
from videogen_service.scripting import ScriptStudio

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_RENDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


class RenderNotFound(RuntimeError):
    pass


class RenderConflict(RuntimeError):
    pass


class RenderService:
    """Owns durable render execution behind the HTTP service boundary."""

    def __init__(
        self,
        config: ServiceConfig,
        renderer: Renderer,
        *,
        studio: ScriptStudio | None = None,
        # threading.Lock is a factory, not a class, so the annotation must stay
        # a string to avoid evaluating Lock | None at runtime.
        gpu_gate: "Lock | None" = None,
    ) -> None:
        self._config = config
        self._renderer = renderer
        self._studio = studio or ScriptStudio(config)
        # One card, many workers: the render queue and the generation-job queue
        # (jobs.py) take turns through this shared gate.
        self._gpu_gate = gpu_gate or Lock()
        self._state_lock = Lock()
        self._queue: Queue[RenderRequest | None] = Queue()
        self._closing = Event()
        # 取消请求的登记簿:worker 出队时跳过,渲染器在块边界/轮询点自查。
        self._cancel_requests: set[str] = set()
        self._config.work_dir.mkdir(parents=True, exist_ok=True)
        self._recover_interrupted()
        self._worker = Thread(
            target=self._work,
            daemon=True,
            name="videogen-render-worker",
        )
        self._worker.start()

    def health(self) -> dict[str, object]:
        return {
            "status": "ok",
            "modes": sorted(self._config.modes),
            "timeline_modes": sorted(
                mode
                for mode, settings in self._config.modes.items()
                if "timeline" in settings.inputs
            ),
            "max_total_seconds": self._config.renderer.max_total_seconds,
            "max_shots": self._config.renderer.max_shots,
            "fps": self._config.renderer.fps,
            "min_seconds": self._config.renderer.min_seconds,
            "max_seconds": self._config.renderer.max_seconds,
            "length_step": LENGTH_STEP,
            "length_offset": LENGTH_OFFSET,
            "shot_header_pattern": SHOT_HEADER.pattern,
        }

    def script_config(self) -> dict[str, object]:
        # Deliberately not folded into /health: the sibling control plane parses
        # that payload with extra="forbid", so it is a frozen contract.
        settings = self._config.script
        return {
            "enabled": settings.enabled,
            "model": settings.model or self._config.ollama.model,
            "subtitle_languages": list(settings.subtitle_languages),
            "target_seconds": settings.target_seconds,
            "shot_seconds": settings.shot_seconds,
            "max_shots": settings.max_shots,
            "max_source_seconds": settings.max_source_seconds,
        }

    def close(self) -> None:
        with self._state_lock:
            self._closing.set()
        while True:
            try:
                queued = self._queue.get_nowait()
            except Empty:
                break
            try:
                if queued is not None:
                    self._fail_for_shutdown(queued.render_id)
            finally:
                self._queue.task_done()
        self._queue.put(None)
        # ComfyUI's interrupt endpoint is global to the whole instance and this
        # machine also uses it for cover art. Graceful shutdown therefore waits
        # for our active H3 render instead of risking interruption of another
        # project's prompt; a forced process kill is recovered on next start.
        self._worker.join()

    def validate(self, request: RenderValidationRequest) -> RenderValidation:
        return validate_render(
            request,
            config=self._config,
            has_first_frame=request.has_first_frame,
            has_last_frame=request.has_last_frame,
        )

    def script(self, request: ScriptRequest) -> ScriptResult:
        return self._studio.create(request)

    def submit(
        self,
        spec: RenderSpec,
        *,
        first_frame: tuple[str, bytes] | None,
        last_frame: tuple[str, bytes] | None,
        refs: list[tuple[str, bytes]] | None = None,
    ) -> RenderView:
        if self._closing.is_set():
            raise RenderConflict("渲染服务正在关闭")
        validation = validate_render(
            RenderRequest(**spec.model_dump()),
            config=self._config,
            has_first_frame=first_frame is not None,
            has_last_frame=last_frame is not None,
            ref_count=len(refs or []),
        )
        # The validated duration is canonical: storyboard requests are aligned
        # shot-by-shot and the control plane must observe that exact total.
        spec = spec.model_copy(update={"seconds": validation.seconds})
        fingerprint = _fingerprint(
            spec, first_frame=first_frame, last_frame=last_frame, refs=refs
        )
        with self._state_lock:
            if self._closing.is_set():
                raise RenderConflict("渲染服务正在关闭")
            existing = self._load_record(spec.render_id)
            if existing is not None:
                if existing.fingerprint != fingerprint:
                    raise RenderConflict(
                        f"render id {spec.render_id} 已属于另一份请求"
                    )
                if existing.status in {"QUEUED", "RUNNING", "DONE"}:
                    if existing.status == "DONE" and not (
                        self._render_dir(spec.render_id) / "output.mp4"
                    ).is_file():
                        existing = existing.model_copy(update={"status": "FAILED"})
                    else:
                        return self._view(existing)
            request = self._store_request(
                spec, first_frame=first_frame, last_frame=last_frame, refs=refs
            )
            now = _now()
            record = RenderRecord(
                render_id=spec.render_id,
                fingerprint=fingerprint,
                status="QUEUED",
                progress=None,
                error=None,
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
            )
            self._save_record(record)
            self._queue.put(request)
        return self.get(spec.render_id)

    def get(self, render_id: str) -> RenderView:
        with self._state_lock:
            record = self._load_record(render_id)
            if record is None:
                raise RenderNotFound(f"render {render_id} 不存在")
            return self._view(record)

    def list_renders(self) -> list[RenderSummary]:
        """Newest first, for the console's job list."""
        with self._state_lock:
            summaries: list[RenderSummary] = []
            for path in self._config.work_dir.glob("*/state.json"):
                record = self._read_record(path)
                if record is None or record.render_id != path.parent.name:
                    continue
                request = self._read_request(path.parent)
                if request is None:
                    continue
                summaries.append(self._summary(record, request))
        summaries.sort(key=lambda summary: summary.created_at, reverse=True)
        return summaries

    def retry(self, render_id: str) -> RenderView:
        """Re-queue a finished render from the request already on disk."""
        with self._state_lock:
            if self._closing.is_set():
                raise RenderConflict("渲染服务正在关闭")
            record = self._load_record(render_id)
            if record is None:
                raise RenderNotFound(f"render {render_id} 不存在")
            if record.status in {"QUEUED", "RUNNING"}:
                raise RenderConflict("这条任务还在队列里")
            request = self._read_request(self._render_dir(render_id))
            if request is None:
                raise RenderNotFound(f"render {render_id} 的请求已经不在了")
            self._save_record(
                record.model_copy(
                    update={
                        "status": "QUEUED",
                        "progress": None,
                        "error": None,
                        "updated_at": _now(),
                    }
                )
            )
            self._queue.put(request)
        return self.get(render_id)

    def cancel(self, render_id: str) -> RenderView:
        """取消一条渲染。QUEUED 直接标终态等 worker 跳过;RUNNING 登记
        取消请求,渲染器在轮询点收手(若正跑的 ComfyUI prompt 确认是
        我们的,顺带请求中断)。终态沿用 FAILED + 可读错误文案,不给
        冻结契约引入新状态值。"""
        with self._state_lock:
            record = self._load_record(render_id)
            if record is None:
                raise RenderNotFound(f"render {render_id} 不存在")
            if record.status not in {"QUEUED", "RUNNING"}:
                raise RenderConflict(f"这条渲染已经结束({record.status}),没有可取消的")
            self._cancel_requests.add(render_id)
        if record.status == "QUEUED":
            # worker 还没接手;先标终态,worker 出队时看登记簿跳过执行。
            self._change(
                render_id,
                lambda rec: rec.model_copy(
                    update={
                        "status": "FAILED",
                        "progress": None,
                        "error": "已取消(用户请求,未开始渲染)",
                        "updated_at": _now(),
                    }
                ),
            )
        return self.get(render_id)

    def frame_path(self, render_id: str, slot: str) -> Path:
        """Where a submitted render's reference frame lives, for archival
        into the asset center."""
        with self._state_lock:
            if self._load_record(render_id) is None:
                raise RenderNotFound(f"render {render_id} 不存在")
            request = self._read_request(self._render_dir(render_id))
            if request is None:
                raise RenderNotFound(f"render {render_id} 的请求已经不在了")
            path = request.first_frame if slot == "first" else request.last_frame
            if path is None or not path.is_file():
                slot_name = "首帧" if slot == "first" else "尾帧"
                raise RenderNotFound(f"render {render_id} 没有{slot_name}参考图")
            return path

    def media_path(self, render_id: str) -> Path:
        with self._state_lock:
            record = self._load_record(render_id)
            if record is None:
                raise RenderNotFound(f"render {render_id} 不存在")
            path = self._render_dir(render_id) / "output.mp4"
            if record.status != "DONE" or not path.is_file():
                raise RenderNotFound(f"render {render_id} 还没有可下载的视频")
            return path

    def delete(self, render_id: str) -> None:
        with self._state_lock:
            record = self._load_record(render_id)
            if record is None:
                raise RenderNotFound(f"render {render_id} 不存在")
            if record.status in {"QUEUED", "RUNNING"}:
                raise RenderConflict("排队或生成中的 render 不能删除")
            directory = self._render_dir(render_id)
            root = self._config.work_dir.resolve()
            resolved = directory.resolve()
            if resolved == root or root not in resolved.parents:
                raise RenderConflict(f"拒绝删除工作目录之外的路径: {resolved}")
            shutil.rmtree(resolved)

    def _work(self) -> None:
        while True:
            request = self._queue.get()
            try:
                if request is None:
                    return
                if self._closing.is_set():
                    self._fail_for_shutdown(request.render_id)
                elif request.render_id in self._cancel_requests:
                    # 排队期已被取消,cancel() 标好了终态,这里只清登记。
                    with self._state_lock:
                        self._cancel_requests.discard(request.render_id)
                else:
                    self._run(request)
            finally:
                self._queue.task_done()

    def _run(self, request: RenderRequest) -> None:
        render_id = request.render_id
        try:
            self._change(
                render_id,
                lambda record: record.model_copy(
                    update={
                        "status": "RUNNING",
                        "progress": None,
                        "error": None,
                        "started_at": _now(),
                        "updated_at": _now(),
                    }
                ),
            )

            def report(progress: RenderProgress) -> None:
                self._change(
                    render_id,
                    lambda record: record.model_copy(
                        update={"progress": progress, "updated_at": _now()}
                    ),
                )

            with self._gpu_gate:
                output = self._renderer.render(
                    request,
                    on_progress=report,
                    should_cancel=lambda: render_id in self._cancel_requests,
                )
            write_bytes_atomic(self._render_dir(render_id) / "output.mp4", output)
            self._change(
                render_id,
                lambda record: record.model_copy(
                    update={
                        "status": "DONE",
                        "progress": None,
                        "error": None,
                        "updated_at": _now(),
                    }
                ),
            )
        except RenderCancelled:
            self._change(
                render_id,
                lambda record: record.model_copy(
                    update={
                        "status": "FAILED",
                        "progress": None,
                        "error": "已取消(用户请求)",
                        "updated_at": _now(),
                    }
                ),
            )
        except Exception as error:
            self._change(
                render_id,
                lambda record: record.model_copy(
                    update={
                        "status": "FAILED",
                        "progress": None,
                        "error": f"{type(error).__name__}: {error}",
                        "updated_at": _now(),
                    }
                ),
            )
        finally:
            with self._state_lock:
                self._cancel_requests.discard(render_id)

    def _fail_for_shutdown(self, render_id: str) -> None:
        self._change(
            render_id,
            lambda record: record.model_copy(
                update={
                    "status": "FAILED",
                    "progress": None,
                    "error": "服务关闭前未开始渲染,请从控制台重试",
                    "updated_at": _now(),
                }
            ),
        )

    def _change(
        self, render_id: str, change: Callable[[RenderRecord], RenderRecord]
    ) -> None:
        with self._state_lock:
            record = self._load_record(render_id)
            if record is None:
                return
            self._save_record(change(record))

    def _store_request(
        self,
        spec: RenderSpec,
        *,
        first_frame: tuple[str, bytes] | None,
        last_frame: tuple[str, bytes] | None,
        refs: list[tuple[str, bytes]] | None = None,
    ) -> RenderRequest:
        directory = self._render_dir(spec.render_id)
        directory.mkdir(parents=True, exist_ok=True)
        first_path = self._store_frame(directory, "first", first_frame)
        last_path = self._store_frame(directory, "last", last_frame)
        ref_paths = [
            path
            for index, ref in enumerate(refs or [])
            if (path := self._store_frame(directory, f"ref{index}", ref)) is not None
        ]
        request = RenderRequest(
            **spec.model_dump(),
            first_frame=first_path,
            last_frame=last_path,
            refs=ref_paths,
        )
        write_json_atomic(
            directory / "request.json", request.model_dump(mode="json")
        )
        return request

    def _store_frame(
        self, directory: Path, slot: str, frame: tuple[str, bytes] | None
    ) -> Path | None:
        if frame is None:
            return None
        filename, data = frame
        suffix = Path(filename).suffix.lower() or ".png"
        if suffix not in _IMAGE_SUFFIXES:
            raise RenderConflict(f"参考图格式不支持: {suffix}")
        destination = directory / f"{slot}{suffix}"
        write_bytes_atomic(destination, data)
        return destination

    def _read_record(self, path: Path) -> RenderRecord | None:
        try:
            return RenderRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _read_request(self, directory: Path) -> RenderRequest | None:
        path = directory / "request.json"
        try:
            return RenderRequest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _recover_interrupted(self) -> None:
        for path in self._config.work_dir.glob("*/state.json"):
            record = self._read_record(path)
            if record is None:
                continue
            if record.render_id != path.parent.name or not _RENDER_ID.fullmatch(
                record.render_id
            ):
                continue
            if record.status not in {"QUEUED", "RUNNING"}:
                continue
            self._save_record(
                record.model_copy(
                    update={
                        "status": "FAILED",
                        "progress": None,
                        "error": "服务重启中断了渲染,请从控制台重试",
                        "updated_at": _now(),
                    }
                )
            )

    def _render_dir(self, render_id: str) -> Path:
        if not _RENDER_ID.fullmatch(render_id):
            raise RenderNotFound("render id 无效")
        return self._config.work_dir / render_id

    def _record_path(self, render_id: str) -> Path:
        return self._render_dir(render_id) / "state.json"

    def _load_record(self, render_id: str) -> RenderRecord | None:
        path = self._record_path(render_id)
        if not path.is_file():
            return None
        return RenderRecord.model_validate_json(path.read_text(encoding="utf-8"))

    def _save_record(self, record: RenderRecord) -> None:
        write_json_atomic(
            self._record_path(record.render_id), record.model_dump(mode="json")
        )

    @staticmethod
    def _summary(record: RenderRecord, request: RenderRequest) -> RenderSummary:
        view = RenderService._view(record)
        return RenderSummary(
            **view.model_dump(),
            mode=request.mode,
            prompt=request.prompt,
            width=request.width,
            height=request.height,
            seconds=request.seconds,
            seed=request.seed,
            has_first_frame=request.first_frame is not None,
            has_last_frame=request.last_frame is not None,
            created_at=record.created_at,
            updated_at=record.updated_at,
            elapsed_seconds=_elapsed_seconds(record),
        )

    @staticmethod
    def _view(record: RenderRecord) -> RenderView:
        return RenderView(
            render_id=record.render_id,
            status=record.status,
            progress=record.progress,
            error=record.error,
            media_url=(
                f"/v1/renders/{record.render_id}/media"
                if record.status == "DONE"
                else None
            ),
        )


def _fingerprint(
    spec: RenderSpec,
    *,
    first_frame: tuple[str, bytes] | None,
    last_frame: tuple[str, bytes] | None,
    refs: list[tuple[str, bytes]] | None = None,
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            spec.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    )
    for frame in (first_frame, last_frame, *(refs or [])):
        digest.update(b"\0")
        if frame is not None:
            digest.update(frame[1])
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _elapsed_seconds(record: RenderRecord) -> float | None:
    """How long an in-flight render has been on the card, for the progress bar."""
    if record.status != "RUNNING" or record.started_at is None:
        return None
    try:
        started = datetime.fromisoformat(record.started_at)
    except ValueError:
        return None
    return max(0.0, (datetime.now(UTC) - started).total_seconds())
