from collections.abc import Callable
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import time
from typing import Any, NamedTuple, Protocol
from urllib.error import URLError
import urllib.request

from videogen_service.comfyui import (
    ComfyUiClient,
    ComfyUiError,
    ProgressEvent,
    load_workflow,
    patch_workflow,
)
from videogen_service.config import ModeConfig, ServiceConfig
from videogen_service.models import (
    RenderProgress,
    RenderRequest,
    RenderValidation,
    RenderValidationRequest,
)

LENGTH_STEP = 17
LENGTH_OFFSET = 5
DIMENSION_MULTIPLE = 32
TIMELINE_INPUT = "timeline"
DIRECTOR_TASK_TYPE = "t2v — 文生视频(Text to Video)"
TIMELINE_VERSION = 4
SHOT_HEADER = re.compile(
    r"^\[\s*(\d+(?:\.\d+)?)\s*s?\s*(?:[-–—~～]\s*(\d+(?:\.\d+)?)\s*s?\s*)?\]\s*(.*)$"
)
PROGRESS_CEILING = 0.99


class RenderError(RuntimeError):
    pass


class Shot(NamedTuple):
    seconds: float
    prompt: str


class Storyboard(NamedTuple):
    preamble: str
    shots: list[Shot]


class Renderer(Protocol):
    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
    ) -> bytes: ...


class ProgressTracker:
    def __init__(self, *, passes: int) -> None:
        self._expected = max(1, passes)
        self._done = 0
        self._node: str | None = None
        self._value = 0
        self._peak = 0.0

    def update(self, event: ProgressEvent) -> RenderProgress:
        if self._node == event.node and event.value < self._value:
            self._done += 1
        self._node = event.node
        self._value = event.value
        total = max(self._expected, self._done + 1)
        percent = min(
            (self._done + event.value / event.maximum) / total, PROGRESS_CEILING
        )
        self._peak = max(self._peak, percent)
        return RenderProgress(
            percent=self._peak,
            step=event.value,
            total_steps=event.maximum,
            pass_index=self._done + 1,
            pass_total=total,
        )


def align_length_frames(seconds: float, *, fps: int) -> int:
    requested = max(LENGTH_OFFSET, round(seconds * fps))
    steps = math.ceil((requested - LENGTH_OFFSET) / LENGTH_STEP)
    return max(0, steps) * LENGTH_STEP + LENGTH_OFFSET


def align_dimension(value: int) -> int:
    aligned = round(value / DIMENSION_MULTIPLE) * DIMENSION_MULTIPLE
    return max(DIMENSION_MULTIPLE, aligned)


def job_seed(render_id: str) -> int:
    digest = hashlib.sha256(render_id.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") & 0x7FFF_FFFF_FFFF_FFFF


def is_timeline_mode(settings: ModeConfig) -> bool:
    return TIMELINE_INPUT in settings.inputs


def parse_storyboard(prompt: str, *, default_seconds: float) -> Storyboard:
    preamble: list[str] = []
    drafts: list[tuple[float, list[str]]] = []
    for line in prompt.splitlines():
        match = SHOT_HEADER.match(line.strip())
        if match is None:
            (drafts[-1][1] if drafts else preamble).append(line)
            continue
        start, end, text = match.groups()
        seconds = float(end) - float(start) if end is not None else float(start)
        drafts.append((seconds, [text] if text.strip() else []))
    if not drafts:
        return Storyboard("", [Shot(default_seconds, prompt.strip())])
    return Storyboard(
        "\n".join(preamble).strip(),
        [Shot(seconds, "\n".join(lines).strip()) for seconds, lines in drafts],
    )


def storyboard_frames(board: Storyboard, *, fps: int) -> list[int]:
    return [align_length_frames(shot.seconds, fps=fps) for shot in board.shots]


def storyboard_seconds(board: Storyboard, *, fps: int) -> float:
    return sum(storyboard_frames(board, fps=fps)) / fps


def validate_storyboard(board: Storyboard, *, config: ServiceConfig) -> None:
    settings = config.renderer
    if not board.shots:
        raise RenderError("分镜是空的,至少要写一段")
    if len(board.shots) > settings.max_shots:
        raise RenderError(f"分镜最多 {settings.max_shots} 段,这份有 {len(board.shots)} 段")
    for index, shot in enumerate(board.shots, start=1):
        if not (settings.min_seconds <= shot.seconds <= settings.max_seconds):
            raise RenderError(
                f"第 {index} 段是 {shot.seconds:g} 秒,每段要在 "
                f"{settings.min_seconds:g}-{settings.max_seconds:g} 秒之间"
            )
        if not shot.prompt and not board.preamble:
            raise RenderError(f"第 {index} 段没有画面描述")
    total = storyboard_seconds(board, fps=settings.fps)
    if total > settings.max_total_seconds:
        raise RenderError(
            f"分镜总时长 {total:.1f} 秒,超过上限 {settings.max_total_seconds:g} 秒"
        )


def validate_render(
    request: RenderRequest | RenderValidationRequest,
    *,
    config: ServiceConfig,
    has_first_frame: bool,
    has_last_frame: bool,
) -> RenderValidation:
    mode_settings = config.modes.get(request.mode)
    if mode_settings is None:
        raise RenderError(f"未配置的生成模式: {request.mode}")
    timeline_mode = is_timeline_mode(mode_settings)
    if timeline_mode:
        if has_first_frame or has_last_frame:
            raise RenderError("分镜模式还不接参考图,请用图生视频或首尾帧")
        board = parse_storyboard(request.prompt, default_seconds=request.seconds)
        validate_storyboard(board, config=config)
        seconds = storyboard_seconds(board, fps=config.renderer.fps)
    else:
        if not (config.renderer.min_seconds <= request.seconds <= config.renderer.max_seconds):
            raise RenderError(
                f"时长要在 {config.renderer.min_seconds:g}-{config.renderer.max_seconds:g} "
                f"秒之间: {request.seconds:g}"
            )
        if "first_frame" in mode_settings.inputs and not has_first_frame:
            raise RenderError(f"{request.mode} 需要一张首帧图片")
        if "last_frame" in mode_settings.inputs and not has_last_frame:
            raise RenderError("首尾帧模式需要尾帧图片")
        seconds = request.seconds
    return RenderValidation(
        mode=request.mode, timeline_mode=timeline_mode, seconds=seconds
    )


def build_timeline(
    board: Storyboard, *, config: ServiceConfig, width: int, height: int
) -> dict[str, Any]:
    fps = config.renderer.fps
    frames = storyboard_frames(board, fps=fps)
    long_edge = max(width, height)
    segments: list[dict[str, Any]] = []
    start = 0
    for index, (shot, length) in enumerate(zip(board.shots, frames, strict=True)):
        segments.append(
            {
                "id": f"s{index}",
                "start": start,
                "length": length,
                "frameCount": length,
                "prompt": shot_prompt(board, shot),
                "taskType": "",
                "refs": [],
                "negativePrompt": "",
            }
        )
        start += length
    return {
        "version": TIMELINE_VERSION,
        "editMode": "segment",
        "timelineMode": "gen_blank",
        "totalFrames": start,
        "frameRate": float(fps),
        "width": width,
        "height": height,
        "refMaxSize": long_edge,
        "output": {
            "mode": "fixed",
            "longEdge": long_edge,
            "width": width,
            "height": height,
            "maxExportFrames": 0,
            "exportMode": "all",
            "audioMode": "generate",
        },
        "videoClips": [],
        "video": {
            "fileName": "",
            "videoFile": "",
            "subfolder": "",
            "type": "input",
            "frames": [],
            "frameMap": [],
        },
        "global": {
            "taskType": DIRECTOR_TASK_TYPE,
            "prompt": board.preamble,
            "refs": [],
            "referenceVideo": {},
            "continuousReference": False,
        },
        "segments": segments,
        "gen": {"defaultFrameCount": frames[0] if frames else 0},
    }


def shot_prompt(board: Storyboard, shot: Shot) -> str:
    if not board.preamble:
        return shot.prompt
    if not shot.prompt:
        return board.preamble
    return f"{board.preamble}\n\n{shot.prompt}"


def build_values(
    request: RenderRequest, *, config: ServiceConfig, uploaded: dict[str, str]
) -> dict[str, Any]:
    width = align_dimension(request.width)
    height = align_dimension(request.height)
    values: dict[str, Any] = {
        "prompt": request.prompt,
        "width": width,
        "height": height,
        "length": align_length_frames(request.seconds, fps=config.renderer.fps),
        "seed": request.seed if request.seed is not None else job_seed(request.render_id),
    }
    mode_settings = config.modes.get(request.mode)
    if mode_settings is not None and is_timeline_mode(mode_settings):
        board = parse_storyboard(request.prompt, default_seconds=request.seconds)
        timeline = build_timeline(board, config=config, width=width, height=height)
        values["prompt"] = board.preamble or request.prompt
        values["length"] = timeline["totalFrames"]
        values[TIMELINE_INPUT] = json.dumps(timeline, ensure_ascii=False)
        return values
    values.update(uploaded)
    return values


class H3Renderer:
    def __init__(self, config: ServiceConfig, *, logger: logging.Logger | None = None) -> None:
        required = {"prompt", "width", "height", "length", "seed"}
        for mode, settings in config.modes.items():
            missing = required - set(settings.inputs)
            if missing:
                raise RenderError(f"{mode} workflow 缺少指针: {sorted(missing)}")
        self._config = config
        self._logger = logger or logging.getLogger("videogen_service")

    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
    ) -> bytes:
        config = self._config
        mode_settings = config.modes.get(request.mode)
        if mode_settings is None:
            raise RenderError(f"未配置的生成模式: {request.mode}")
        validation = validate_render(
            request,
            config=config,
            has_first_frame=request.first_frame is not None,
            has_last_frame=request.last_frame is not None,
        )
        passes = 1
        if validation.timeline_mode:
            passes = len(
                parse_storyboard(request.prompt, default_seconds=request.seconds).shots
            )
        if config.ollama.unload_before_render:
            release_ollama_vram(config, logger=self._logger)
        client = ComfyUiClient(
            base_url=config.comfyui.base_url,
            timeout_seconds=config.comfyui.timeout_seconds,
            poll_interval_seconds=config.comfyui.poll_interval_seconds,
        )
        try:
            uploaded = (
                {} if validation.timeline_mode else _upload_references(request, client=client)
            )
            workflow = patch_workflow(
                load_workflow(mode_settings.workflow_file),
                pointers=mode_settings.inputs,
                values=build_values(request, config=config, uploaded=uploaded),
            )
            tracker = ProgressTracker(passes=passes)

            def report(event: ProgressEvent) -> None:
                if on_progress is not None:
                    on_progress(tracker.update(event))

            started = time.monotonic()
            output = client.render(
                workflow, on_progress=report if on_progress is not None else None
            )
        except ComfyUiError as error:
            raise RenderError(str(error)) from error
        if not output.filename.lower().endswith(".mp4"):
            raise RenderError(
                f"workflow 输出的不是 mp4({output.filename}),请确认末端是 SaveVideo 节点"
            )
        self._logger.info(
            "生成完成 %s (%.0fs, %.1fMB)",
            request.render_id,
            time.monotonic() - started,
            len(output.data) / 1_048_576,
        )
        return output.data


def _upload_references(
    request: RenderRequest, *, client: ComfyUiClient
) -> dict[str, str]:
    uploaded: dict[str, str] = {}
    for name, path in (
        ("first_frame", request.first_frame),
        ("last_frame", request.last_frame),
    ):
        if path is None:
            continue
        if not path.is_file():
            raise RenderError(f"参考图不存在: {path}")
        uploaded[name] = client.upload_image(path)
    return uploaded


def release_ollama_vram(config: ServiceConfig, *, logger: logging.Logger) -> None:
    payload = json.dumps(
        {"model": config.ollama.model, "keep_alive": 0}, ensure_ascii=False
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{config.ollama.base_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            response.read()
    except (TimeoutError, URLError, OSError) as error:
        logger.warning("卸载 Ollama 显存失败,继续生成: %s", error)
        return
    logger.info("已卸载 Ollama 模型 %s,显存交给 H3", config.ollama.model)
