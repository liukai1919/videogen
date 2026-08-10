# The generic generation-job queue: the platform's second lane next to the
# frozen H3 render seam. Images and audio (and later more) run through one
# durable queue with the same disk-state style as renders, and share a single
# GPU gate with the render worker — this machine has one card, so a FLUX image
# and an H3 video must take turns, while a CPU TTS job may run alongside.
#
# Storage: work/.jobs/<job_id>/{job.json, output.*} (".jobs" has a dot, so it
# can never collide with a render directory).

from collections.abc import Callable
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Queue
import re
import shutil
import subprocess
from threading import Event, Lock, Thread
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from videogen_service.artifacts import write_bytes_atomic, write_json_atomic
from videogen_service.comfyui import ComfyUiClient, load_workflow, patch_workflow
from videogen_service.config import (
    Capability,
    ComfyCapability,
    CommandCapability,
    ServiceConfig,
)
from videogen_service.renderer import job_seed
from videogen_service.service import RenderNotFound, RenderService

JOBS_DIRNAME = ".jobs"
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# 成片合成是内建能力:FFmpeg 在本机把渲染视频、配音音轨和 SRT 字幕拼成成片。
# 不进 config,因为它不依赖任何模型,只要机器上有 ffmpeg。
COMPOSE_CAPABILITY_ID = "compose"
_FFMPEG_TIMEOUT_SECONDS = 1800.0

JobStatus = Literal["QUEUED", "RUNNING", "DONE", "FAILED"]

JOB_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
}


class JobError(RuntimeError):
    pass


class JobNotFound(JobError):
    pass


class JobConflict(JobError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class JobSpec(StrictModel):
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    capability: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    # t2i uses prompt (+ optional framing); tts uses text.
    prompt: str | None = Field(default=None, min_length=1, max_length=50_000)
    text: str | None = Field(default=None, min_length=1, max_length=20_000)
    width: int | None = Field(default=None, ge=32, le=8192)
    height: int | None = Field(default=None, ge=32, le=8192)
    seed: int | None = Field(default=None, ge=0)
    # compose uses these: the source render, an optional TTS job for the audio
    # track, and optional SRT text to burn in.
    render_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    audio_job_id: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$"
    )
    subtitles: str | None = Field(default=None, min_length=1, max_length=100_000)


class ComposeCapability(StrictModel):
    kind: Literal["compose"] = "compose"
    needs_gpu: bool = False


AnyCapability = Capability | ComposeCapability


class JobRecord(StrictModel):
    schema_version: Literal[1] = 1
    spec: JobSpec
    kind: str
    status: JobStatus
    error: str | None = None
    output: str | None = None
    created_at: str
    started_at: str | None = None
    updated_at: str


class JobView(StrictModel):
    job_id: str
    capability: str
    kind: str
    status: JobStatus
    error: str | None = None
    media_url: str | None = None
    prompt: str | None = None
    text: str | None = None
    width: int | None = None
    height: int | None = None
    seed: int | None = None
    render_id: str | None = None
    audio_job_id: str | None = None
    created_at: str
    updated_at: str


class JobRunner(Protocol):
    def run(
        self, spec: JobSpec, capability: AnyCapability, *, directory: Path
    ) -> str:
        """Executes the job, writes its output into `directory`, and returns
        the output filename."""
        ...


class LocalJobRunner:
    """Executes capabilities with this machine's own tools: ComfyUI workflows
    for images, a configured local command for TTS, FFmpeg for composition.
    Nothing leaves the box."""

    def __init__(
        self, config: ServiceConfig, *, renders: RenderService | None = None
    ) -> None:
        self._config = config
        self._renders = renders

    def run(
        self, spec: JobSpec, capability: AnyCapability, *, directory: Path
    ) -> str:
        if isinstance(capability, ComfyCapability):
            return self._run_comfy(spec, capability, directory=directory)
        if isinstance(capability, ComposeCapability):
            return self._run_compose(spec, directory=directory)
        return self._run_command(spec, capability, directory=directory)

    def _run_compose(self, spec: JobSpec, *, directory: Path) -> str:
        if self._renders is None:
            raise JobError("合成需要渲染服务在场")
        try:
            video = self._renders.media_path(spec.render_id or "")
        except RenderNotFound as error:
            raise JobError(str(error)) from error
        audio: Path | None = None
        if spec.audio_job_id is not None:
            audio = _audio_output(
                self._config.work_dir / JOBS_DIRNAME / spec.audio_job_id
            )
        subtitles_name: str | None = None
        if spec.subtitles is not None:
            subtitles_name = "subs.srt"
            write_bytes_atomic(
                directory / subtitles_name, spec.subtitles.encode("utf-8")
            )
        command = ffmpeg_command(
            video=video,
            audio=audio,
            subtitles=subtitles_name,
            output="output.mp4",
            mix_native=audio is not None and _has_audio_stream(video),
        )
        try:
            completed = subprocess.run(
                command,
                cwd=directory,
                capture_output=True,
                timeout=_FFMPEG_TIMEOUT_SECONDS,
                check=False,
            )
        except FileNotFoundError as error:
            raise JobError("找不到 ffmpeg,请先安装并放进 PATH") from error
        except subprocess.TimeoutExpired as error:
            raise JobError(
                f"ffmpeg 合成超时({_FFMPEG_TIMEOUT_SECONDS:.0f}s)"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-600:]
            raise JobError(f"ffmpeg 退出码 {completed.returncode}: {detail}")
        output = directory / "output.mp4"
        if not output.is_file() or output.stat().st_size == 0:
            raise JobError("ffmpeg 结束了但没有产出成片")
        return "output.mp4"

    def _run_comfy(
        self, spec: JobSpec, capability: ComfyCapability, *, directory: Path
    ) -> str:
        workflow = load_workflow(capability.workflow_file)
        values: dict[str, object] = {"prompt": spec.prompt}
        if "width" in capability.inputs and spec.width is not None:
            values["width"] = spec.width
        if "height" in capability.inputs and spec.height is not None:
            values["height"] = spec.height
        if "seed" in capability.inputs:
            values["seed"] = (
                spec.seed if spec.seed is not None else job_seed(spec.job_id)
            )
        patched = patch_workflow(
            workflow, pointers=capability.inputs, values=values
        )
        client = ComfyUiClient(
            base_url=self._config.comfyui.base_url,
            timeout_seconds=self._config.comfyui.timeout_seconds,
            poll_interval_seconds=self._config.comfyui.poll_interval_seconds,
        )
        output = client.render(patched)
        suffix = Path(output.filename).suffix.lower() or ".png"
        filename = f"output{suffix}"
        write_bytes_atomic(directory / filename, output.data)
        return filename

    def _run_command(
        self, spec: JobSpec, capability: CommandCapability, *, directory: Path
    ) -> str:
        text_file = directory / "text.txt"
        write_bytes_atomic(text_file, (spec.text or "").encode("utf-8"))
        output_file = directory / f"output{capability.output_suffix}"
        # Plain replace, not str.format: a command line may legitimately carry
        # other braces (JSON args, shell snippets) that must pass through.
        command = [
            argument.replace("{text_file}", str(text_file)).replace(
                "{output_file}", str(output_file)
            )
            for argument in capability.command
        ]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=capability.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise JobError(f"TTS 命令不存在: {command[0]}") from error
        except subprocess.TimeoutExpired as error:
            raise JobError(
                f"TTS 命令超时({capability.timeout_seconds:.0f}s)"
            ) from error
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-400:]
            raise JobError(
                f"TTS 命令退出码 {completed.returncode}: {detail or '无输出'}"
            )
        if not output_file.is_file() or output_file.stat().st_size == 0:
            raise JobError("TTS 命令结束了但没有产出音频文件")
        return output_file.name


class JobService:
    """Durable execution for generation jobs, mirroring the render queue."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        runner: JobRunner | None = None,
        # String annotation: threading.Lock is a factory function at runtime.
        gpu_gate: "Lock | None" = None,
    ) -> None:
        self._config = config
        self._runner = runner or LocalJobRunner(config)
        self._gpu_gate = gpu_gate or Lock()
        self._directory = config.work_dir / JOBS_DIRNAME
        self._lock = Lock()
        self._queue: Queue[JobSpec | None] = Queue()
        self._closing = Event()
        self._recover_interrupted()
        self._worker = Thread(
            target=self._work, daemon=True, name="videogen-job-worker"
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
                    self._fail(queued.job_id, "服务关闭前未开始,请重试")
            finally:
                self._queue.task_done()
        self._queue.put(None)
        self._worker.join(timeout=2)

    def submit(self, spec: JobSpec) -> JobView:
        if self._closing.is_set():
            raise JobConflict("服务正在关闭")
        capability = self._capability(spec.capability)
        if capability.kind == "t2i" and spec.prompt is None:
            raise JobError("t2i 任务需要 prompt")
        if capability.kind == "tts" and spec.text is None:
            raise JobError("tts 任务需要 text")
        if capability.kind == "compose":
            if spec.render_id is None:
                raise JobError("compose 任务需要 render_id")
            if spec.audio_job_id is None and spec.subtitles is None:
                raise JobError("compose 任务至少需要配音(audio_job_id)或字幕(subtitles)")
        with self._lock:
            if self._load(spec.job_id) is not None:
                raise JobConflict(f"job id {spec.job_id} 已存在")
            now = _now()
            self._save(
                JobRecord(
                    spec=spec,
                    kind=capability.kind,
                    status="QUEUED",
                    created_at=now,
                    updated_at=now,
                )
            )
            self._queue.put(spec)
        return self.get(spec.job_id)

    def get(self, job_id: str) -> JobView:
        with self._lock:
            record = self._require(job_id)
        return _view(record)

    def list(self) -> list[JobView]:
        """Newest first, like the render list."""
        with self._lock:
            records: list[JobRecord] = []
            for path in self._directory.glob("*/job.json"):
                record = self._read(path)
                if record is None or record.spec.job_id != path.parent.name:
                    continue
                records.append(record)
        records.sort(key=lambda record: record.created_at, reverse=True)
        return [_view(record) for record in records]

    def retry(self, job_id: str) -> JobView:
        if self._closing.is_set():
            raise JobConflict("服务正在关闭")
        with self._lock:
            record = self._require(job_id)
            if record.status in {"QUEUED", "RUNNING"}:
                raise JobConflict("这条任务还在队列里")
            self._save(
                record.model_copy(
                    update={"status": "QUEUED", "error": None, "updated_at": _now()}
                )
            )
            self._queue.put(record.spec)
        return self.get(job_id)

    def media_path(self, job_id: str) -> Path:
        with self._lock:
            record = self._require(job_id)
            if record.status != "DONE" or record.output is None:
                raise JobNotFound(f"job {job_id} 还没有可下载的产物")
            path = self._directory / job_id / record.output
            if not path.is_file():
                raise JobNotFound(f"job {job_id} 的产物文件不见了")
            return path

    def delete(self, job_id: str) -> None:
        with self._lock:
            record = self._require(job_id)
            if record.status in {"QUEUED", "RUNNING"}:
                raise JobConflict("排队或执行中的任务不能删除")
            directory = (self._directory / job_id).resolve()
            root = self._directory.resolve()
            if directory == root or root not in directory.parents:
                raise JobConflict(f"拒绝删除任务目录之外的路径: {directory}")
            shutil.rmtree(directory)

    def _work(self) -> None:
        while True:
            spec = self._queue.get()
            try:
                if spec is None:
                    return
                if self._closing.is_set():
                    self._fail(spec.job_id, "服务关闭前未开始,请重试")
                else:
                    self._run(spec)
            finally:
                self._queue.task_done()

    def _run(self, spec: JobSpec) -> None:
        try:
            capability = self._capability(spec.capability)
        except JobError as error:
            self._fail(spec.job_id, str(error))
            return
        self._change(
            spec.job_id,
            lambda record: record.model_copy(
                update={
                    "status": "RUNNING",
                    "error": None,
                    "started_at": _now(),
                    "updated_at": _now(),
                }
            ),
        )
        directory = self._directory / spec.job_id
        gate = self._gpu_gate if capability.needs_gpu else nullcontext()
        try:
            with gate:
                output = self._runner.run(spec, capability, directory=directory)
            self._change(
                spec.job_id,
                lambda record: record.model_copy(
                    update={
                        "status": "DONE",
                        "output": output,
                        "error": None,
                        "updated_at": _now(),
                    }
                ),
            )
        except Exception as error:
            self._fail(spec.job_id, f"{type(error).__name__}: {error}")

    def _capability(self, capability_id: str) -> AnyCapability:
        if capability_id == COMPOSE_CAPABILITY_ID:
            return ComposeCapability()
        capability = self._config.capabilities.get(capability_id)
        if capability is None:
            raise JobError(
                f"未配置的能力: {capability_id};可用: "
                f"{sorted([*self._config.capabilities, COMPOSE_CAPABILITY_ID])}"
            )
        return capability

    def _fail(self, job_id: str, message: str) -> None:
        self._change(
            job_id,
            lambda record: record.model_copy(
                update={"status": "FAILED", "error": message, "updated_at": _now()}
            ),
        )

    def _change(
        self, job_id: str, change: Callable[[JobRecord], JobRecord]
    ) -> None:
        with self._lock:
            record = self._load(job_id)
            if record is None:
                return
            self._save(change(record))

    def _recover_interrupted(self) -> None:
        for path in self._directory.glob("*/job.json"):
            record = self._read(path)
            if record is None or record.spec.job_id != path.parent.name:
                continue
            if record.status in {"QUEUED", "RUNNING"}:
                self._save(
                    record.model_copy(
                        update={
                            "status": "FAILED",
                            "error": "服务重启中断了任务,请重试",
                            "updated_at": _now(),
                        }
                    )
                )

    def _require(self, job_id: str) -> JobRecord:
        record = self._load(job_id)
        if record is None:
            raise JobNotFound(f"job {job_id} 不存在")
        return record

    def _load(self, job_id: str) -> JobRecord | None:
        if not _JOB_ID.fullmatch(job_id):
            raise JobNotFound("job id 无效")
        path = self._directory / job_id / "job.json"
        return self._read(path) if path.is_file() else None

    def _read(self, path: Path) -> JobRecord | None:
        try:
            return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, record: JobRecord) -> None:
        write_json_atomic(
            self._directory / record.spec.job_id / "job.json",
            record.model_dump(mode="json"),
        )


# H3 渲染自带原生环境音;压混时把它衬在解说下面而不是抹掉。0.3 是
# 人声在上、环境声可闻的常规底量,想要更沉浸可调大。
_NATIVE_BED_VOLUME = 0.3


def ffmpeg_command(
    *,
    video: Path,
    audio: Path | None,
    subtitles: str | None,
    output: str,
    mix_native: bool = False,
) -> list[str]:
    """The composition command, kept pure for tests. Runs with cwd at the job
    directory, so `subtitles`/`output` are bare filenames and the subtitles
    filter needs no path escaping; inputs stay absolute."""
    command = ["ffmpeg", "-hide_banner", "-y", "-i", str(video)]
    if audio is not None:
        command += ["-i", str(audio)]
    if subtitles is not None:
        # Burning subtitles re-encodes the video; without them the stream is
        # copied untouched.
        command += [
            "-vf",
            f"subtitles={subtitles}",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
        ]
    else:
        command += ["-c:v", "copy"]
    if audio is not None and mix_native:
        # H3 的原生音轨压低当床,解说铺在上面。duration=first 以画面原声
        # 为准:解说短了原声继续,长了跟着画面截断。normalize=0 保持各自
        # 音量,否则 amix 会把两路各砍一半。
        command += [
            "-filter_complex",
            f"[0:a]volume={_NATIVE_BED_VOLUME}[bed];"
            "[bed][1:a]amix=inputs=2:duration=first:normalize=0[aout]",
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            "-c:a",
            "aac",
        ]
    elif audio is not None:
        # 渲染没带音轨(旧片子/非 H3 能力):解说独占。No -shortest: a
        # narration briefer than the picture must not cut it.
        command += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac"]
    else:
        command += ["-map", "0:v:0", "-map", "0:a?", "-c:a", "copy"]
    command.append(output)
    return command


def _has_audio_stream(video: Path) -> bool:
    """ffprobe 探一眼渲染里有没有音轨。探不动(没装 ffprobe、文件怪)就当
    没有——退回解说独占的老行为,合成不因探测而失败。"""
    try:
        completed = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(video),
            ],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and bool(completed.stdout.strip())


def _audio_output(directory: Path) -> Path:
    for candidate in sorted(directory.glob("output.*")):
        if candidate.suffix.lower() in {".wav", ".mp3", ".flac", ".ogg"}:
            return candidate
    raise JobError(
        f"配音任务 {directory.name} 没有音频产物,确认它已经 DONE"
    )


def _view(record: JobRecord) -> JobView:
    spec = record.spec
    return JobView(
        job_id=spec.job_id,
        capability=spec.capability,
        kind=record.kind,
        status=record.status,
        error=record.error,
        media_url=(
            f"/v1/jobs/{spec.job_id}/media"
            if record.status == "DONE" and record.output is not None
            else None
        ),
        prompt=spec.prompt,
        text=spec.text,
        width=spec.width,
        height=spec.height,
        seed=spec.seed,
        render_id=spec.render_id,
        audio_job_id=spec.audio_job_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()
