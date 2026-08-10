import base64
from collections.abc import Callable
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import subprocess
import tempfile
import time
from typing import Any, NamedTuple, Protocol
from urllib.error import URLError
import urllib.request

from videogen_service.braingate import BrainGate
from videogen_service.comfyui import (
    ComfyUiCancelled,
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
# 时间轴段头,例如 [0s-6s] 或 [12s-18s续]:结尾的 续/接 标记表示这一镜是
# 上一镜同一动作的直接延续,跨块时用上一块的最后一帧做 i2v 锚点接力。
SHOT_HEADER = re.compile(
    r"^\[\s*(\d+(?:\.\d+)?)\s*s?\s*(?:[-–—~～]\s*(\d+(?:\.\d+)?)\s*s?\s*)?"
    r"\s*(续|接)?\s*\]\s*(.*)$"
)
PROGRESS_CEILING = 0.99


class RenderCancelled(RuntimeError):
    """用户主动取消。独立于 RenderError:失败要报错误原因,取消要报取消。"""


class RenderError(RuntimeError):
    pass


class Shot(NamedTuple):
    seconds: float
    prompt: str
    # 是否是上一镜同一动作的直接延续(时间轴上的 续/接 标记)。
    cont: bool = False


class Storyboard(NamedTuple):
    preamble: str
    shots: list[Shot]


class ChunkPlan(NamedTuple):
    """拆链渲染中的一块:anchored 表示这一块要用上一块的最后一帧做
    i2v 锚点接力(块内恰好一段,gen_image 模式要求每段都有锚图)。"""

    board: Storyboard
    anchored: bool = False


class Renderer(Protocol):
    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bytes: ...


class ProgressTracker:
    def __init__(self, *, passes: int) -> None:
        self._expected = max(1, passes)
        self._done = 0
        self._node: str | None = None
        self._value = 0
        self._peak = 0.0

    def update(self, event: ProgressEvent) -> RenderProgress:
        # A pass ends either when the same node restarts its step budget or when
        # a different node takes over; a story graph may run each shot on its own
        # sampler, and treating that as "still the first pass" freezes the bar.
        if self._node is not None and (
            self._node != event.node or event.value < self._value
        ):
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
    drafts: list[tuple[float, bool, list[str]]] = []
    for line in prompt.splitlines():
        match = SHOT_HEADER.match(line.strip())
        if match is None:
            (drafts[-1][2] if drafts else preamble).append(line)
            continue
        start, end, marker, text = match.groups()
        seconds = float(end) - float(start) if end is not None else float(start)
        drafts.append((seconds, marker is not None, [text] if text.strip() else []))
    if not drafts:
        return Storyboard("", [Shot(default_seconds, prompt.strip())])
    return Storyboard(
        "\n".join(preamble).strip(),
        [
            Shot(seconds, "\n".join(lines).strip(), cont)
            for seconds, cont, lines in drafts
        ],
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


MAX_REFERENCE_IMAGES = 9  # H3 官方上限:参考图 ≤9 张


def validate_render(
    request: RenderRequest | RenderValidationRequest,
    *,
    config: ServiceConfig,
    has_first_frame: bool,
    has_last_frame: bool,
    ref_count: int = 0,
    segment_ref_count: int = 0,
) -> RenderValidation:
    mode_settings = config.modes.get(request.mode)
    if mode_settings is None:
        raise RenderError(f"未配置的生成模式: {request.mode}")
    if ref_count and not mode_settings.accepts_refs:
        raise RenderError(f"{request.mode} 不接参考图(refs),参考生成请用 r2v 模式")
    if mode_settings.accepts_refs and not ref_count:
        raise RenderError(f"{request.mode} 需要至少一张参考图,提示词里用 <Picture 1> 引用")
    if ref_count > MAX_REFERENCE_IMAGES:
        raise RenderError(f"参考图最多 {MAX_REFERENCE_IMAGES} 张,给了 {ref_count} 张")
    timeline_mode = is_timeline_mode(mode_settings)
    if segment_ref_count:
        if not timeline_mode:
            raise RenderError("段级参考图(segment_refs)只在分镜模式下生效")
        if segment_ref_count > MAX_REFERENCE_IMAGES:
            raise RenderError(
                f"段级参考图最多 {MAX_REFERENCE_IMAGES} 张,给了 {segment_ref_count} 张"
            )
    if timeline_mode:
        if has_first_frame or has_last_frame:
            raise RenderError("分镜模式不接首尾帧,请用图生视频或首尾帧模式")
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
        # A mode without a pointer for a slot has nowhere to patch that image, so
        # accepting one here would only surface as a workflow error mid-render,
        # after the file had already been handed to ComfyUI.
        if has_first_frame and "first_frame" not in mode_settings.inputs:
            raise RenderError(f"{request.mode} 不接首帧图片,请换用图生视频或首尾帧")
        if has_last_frame and "last_frame" not in mode_settings.inputs:
            raise RenderError(f"{request.mode} 不接尾帧图片,请换用首尾帧模式")
        seconds = request.seconds
    return RenderValidation(
        mode=request.mode, timeline_mode=timeline_mode, seconds=seconds
    )


def split_board(board: Storyboard, *, config: ServiceConfig) -> list[ChunkPlan]:
    """把长分镜拆成若干块,每块的补齐总长不超过单条渲染的安全包络
    (renderer.max_seconds——本机 32GB 内存下实测的 OOM 红线)。块内仍是
    一条 Director 任务,享受跨段连贯。块与块之间默认是正常的剪辑切换;
    带 续/接 标记的镜头如果恰好落在块边界上,就单独成一块并用上一块的
    最后一帧做 i2v 锚点接力(标记落在块内时无需处理——Director 的跨段
    连贯已经覆盖)。总长上限依旧归 validate_storyboard 的
    max_total_seconds 管。"""
    fps = config.renderer.fps
    budget = align_length_frames(config.renderer.max_seconds, fps=fps)
    plans: list[ChunkPlan] = []
    current: list[Shot] = []
    used = 0

    def close() -> None:
        nonlocal current, used
        if current:
            plans.append(ChunkPlan(Storyboard(board.preamble, current)))
        current = []
        used = 0

    for shot in board.shots:
        frames = align_length_frames(shot.seconds, fps=fps)
        if current and used + frames > budget:
            close()
        if not current and plans and shot.cont:
            # 续接镜头开新块:自己单独成块并锚定上一块的收尾画面。
            plans.append(ChunkPlan(Storyboard(board.preamble, [shot]), True))
            continue
        current.append(shot)
        used += frames
    close()
    return plans


def concat_command(list_file: str, output: str) -> list[str]:
    """块与块同一工作流、同一编码参数,concat demuxer 直接流拷贝,
    不二压画质。Runs with cwd at the temp directory."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        list_file,
        "-c",
        "copy",
        output,
    ]


def extract_last_frame(data: bytes) -> str:
    """一段 mp4 的最后一帧,base64 PNG——接力块的 i2v 锚点。"""
    with tempfile.TemporaryDirectory(prefix="videogen-frame-") as tmp:
        root = Path(tmp)
        (root / "in.mp4").write_bytes(data)
        try:
            completed = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-y",
                    # 从末尾 1 秒起逐帧覆写同一文件,收尾即最后一帧。
                    "-sseof",
                    "-1",
                    "-i",
                    "in.mp4",
                    "-update",
                    "1",
                    "-frames:v",
                    "999",
                    "last.png",
                ],
                cwd=root,
                capture_output=True,
                timeout=120,
                check=False,
            )
        except FileNotFoundError as error:
            raise RenderError("帧接力需要 ffmpeg,请先安装并放进 PATH") from error
        except subprocess.TimeoutExpired as error:
            raise RenderError("提取接力帧超时(120s)") from error
        frame = root / "last.png"
        if completed.returncode != 0 or not frame.is_file():
            detail = completed.stderr.decode("utf-8", errors="replace")[-300:]
            raise RenderError(f"提取接力帧失败: {detail}")
        return base64.b64encode(frame.read_bytes()).decode("ascii")


def concat_videos(chunks: list[bytes]) -> bytes:
    if len(chunks) == 1:
        return chunks[0]
    with tempfile.TemporaryDirectory(prefix="videogen-concat-") as tmp:
        root = Path(tmp)
        names: list[str] = []
        for index, data in enumerate(chunks):
            name = f"chunk{index}.mp4"
            (root / name).write_bytes(data)
            names.append(name)
        (root / "list.txt").write_text(
            "".join(f"file '{name}'\n" for name in names), encoding="ascii"
        )
        try:
            completed = subprocess.run(
                concat_command("list.txt", "joined.mp4"),
                cwd=root,
                capture_output=True,
                timeout=600,
                check=False,
            )
        except FileNotFoundError as error:
            raise RenderError("分块拼接需要 ffmpeg,请先安装并放进 PATH") from error
        except subprocess.TimeoutExpired as error:
            raise RenderError("分块拼接超时(600s)") from error
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", errors="replace")[-400:]
            raise RenderError(f"分块拼接失败,ffmpeg 退出码 {completed.returncode}: {detail}")
        joined = root / "joined.mp4"
        if not joined.is_file() or joined.stat().st_size == 0:
            raise RenderError("分块拼接结束了但没有产出成片")
        return joined.read_bytes()


def build_timeline(
    board: Storyboard,
    *,
    config: ServiceConfig,
    width: int,
    height: int,
    anchor_b64: str | None = None,
    refs_b64: list[str] | None = None,
    segment_refs_b64: list[str] | None = None,
    task_type: str = "",
) -> dict[str, Any]:
    """anchor_b64 是上一块最后一帧的 base64 PNG:整块转成 gen_image 模式,
    首段(接力块只有一段)以它为 i2v 锚点,画面从这一帧无缝接着演。
    refs_b64 是 r2v 的参考图(身份锚点),挂在 global.refs 上,slot 顺序
    对应提示词里的 <Picture 1>…<Picture N>;task_type 覆盖全局任务类型
    (r2v 模式传 "r2v")。segment_refs_b64 挂到每一段自己的 refs 上
    (segment editMode 下段不继承 global.refs),节点会自动给段提示词
    加 <Picture N> 前缀——全片风格钥匙就走这条通道。"""
    fps = config.renderer.fps
    frames = storyboard_frames(board, fps=fps)
    long_edge = max(width, height)
    segment_refs = [
        {"index": index, "imageB64": image}
        for index, image in enumerate(segment_refs_b64 or [])
    ]
    segments: list[dict[str, Any]] = []
    start = 0
    for index, (shot, length) in enumerate(zip(board.shots, frames, strict=True)):
        segment: dict[str, Any] = {
            "id": f"s{index}",
            "start": start,
            "length": length,
            "frameCount": length,
            "prompt": shot_prompt(board, shot),
            "taskType": "",
            "refs": [dict(ref) for ref in segment_refs],
            "negativePrompt": "",
        }
        if anchor_b64 is not None and index == 0:
            # editMode=segment 下锚图挂在段上;节点收裸 base64。
            segment["taskType"] = "i2v"
            segment["genImage"] = {"imageB64": anchor_b64}
        segments.append(segment)
        start += length
    return {
        "version": TIMELINE_VERSION,
        "editMode": "segment",
        "timelineMode": "gen_image" if anchor_b64 is not None else "gen_blank",
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
            "taskType": (
                "i2v"
                if anchor_b64 is not None
                else (task_type or DIRECTOR_TASK_TYPE)
            ),
            "prompt": board.preamble,
            "refs": [
                {"index": index, "imageB64": image}
                for index, image in enumerate(refs_b64 or [])
            ],
            "referenceVideo": {},
            "continuousReference": config.renderer.continuous_reference,
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


def timeline_values(
    board: Storyboard,
    *,
    request: RenderRequest,
    config: ServiceConfig,
    seed: int,
    anchor_b64: str | None = None,
    refs_b64: list[str] | None = None,
    segment_refs_b64: list[str] | None = None,
    task_type: str = "",
) -> dict[str, Any]:
    """Workflow values for one Director job over `board` — the whole storyboard
    or one chunk of it. Only the preamble is global. Without one the shot text
    already lives in the segments, and falling back to the raw request would
    hand the director node the bracketed timecodes the storyboard was parsed
    from."""
    width = align_dimension(request.width)
    height = align_dimension(request.height)
    timeline = build_timeline(
        board,
        config=config,
        width=width,
        height=height,
        anchor_b64=anchor_b64,
        refs_b64=refs_b64,
        segment_refs_b64=segment_refs_b64,
        task_type=task_type,
    )
    return {
        "prompt": board.preamble,
        "width": width,
        "height": height,
        "length": timeline["totalFrames"],
        "seed": seed,
        TIMELINE_INPUT: json.dumps(timeline, ensure_ascii=False),
    }


def build_values(
    request: RenderRequest, *, config: ServiceConfig, uploaded: dict[str, str]
) -> dict[str, Any]:
    width = align_dimension(request.width)
    height = align_dimension(request.height)
    seed = request.seed if request.seed is not None else job_seed(request.render_id)
    mode_settings = config.modes.get(request.mode)
    if mode_settings is not None and is_timeline_mode(mode_settings):
        board = parse_storyboard(request.prompt, default_seconds=request.seconds)
        return timeline_values(board, request=request, config=config, seed=seed)
    values: dict[str, Any] = {
        "prompt": request.prompt,
        "width": width,
        "height": height,
        "length": align_length_frames(request.seconds, fps=config.renderer.fps),
        "seed": seed,
    }
    values.update(uploaded)
    return values


class H3Renderer:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        logger: logging.Logger | None = None,
        brain_gate: BrainGate | None = None,
    ) -> None:
        required = {"prompt", "width", "height", "length", "seed"}
        for mode, settings in config.modes.items():
            missing = required - set(settings.inputs)
            if missing:
                raise RenderError(f"{mode} workflow 缺少指针: {sorted(missing)}")
        self._config = config
        self._logger = logger or logging.getLogger("videogen_service")
        self._brain_gate = brain_gate

    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
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
            ref_count=len(request.refs),
            segment_ref_count=len(request.segment_refs),
        )
        # 超过单条安全包络的分镜拆成多块串行渲染再拼接;单块走原路。
        plans: list[ChunkPlan] = []
        if validation.timeline_mode:
            board = parse_storyboard(request.prompt, default_seconds=request.seconds)
            plans = split_board(board, config=config)
        passes = sum(len(plan.board.shots) for plan in plans) or 1
        # r2v 的参考图对每一块都全程生效:身份锚点不随分块变化。
        refs_b64 = [_encode_ref(path) for path in request.refs]
        # 段级参考图(风格钥匙等)同样贯穿每一块的每一段。
        segment_refs_b64 = [_encode_ref(path) for path in request.segment_refs]
        chunk_task = "r2v" if mode_settings.accepts_refs else ""
        if config.ollama.unload_before_render:
            # 卸载会打断正在生成的聊天/编剧调用;先等大脑歇口气再动手。
            if self._brain_gate is not None and not self._brain_gate.wait_idle(300.0):
                self._logger.warning("本地大脑生成超 300s 未歇,先卸载继续渲染")
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
            tracker = ProgressTracker(passes=passes)

            def report(event: ProgressEvent) -> None:
                if on_progress is not None:
                    on_progress(tracker.update(event))

            progress = report if on_progress is not None else None
            started = time.monotonic()
            base_seed = (
                request.seed
                if request.seed is not None
                else job_seed(request.render_id)
            )
            if plans:
                chunks: list[bytes] = []
                for index, plan in enumerate(plans):
                    # 块与块之间是天然的取消点:下一块还没提交,直接收手。
                    if should_cancel is not None and should_cancel():
                        raise RenderCancelled("渲染已取消")
                    anchor = (
                        extract_last_frame(chunks[-1])
                        if plan.anchored and chunks
                        else None
                    )
                    if len(plans) > 1:
                        self._logger.info(
                            "渲染分块 %d/%d %s (%d 段%s)",
                            index + 1,
                            len(plans),
                            request.render_id,
                            len(plan.board.shots),
                            ",帧接力" if anchor is not None else "",
                        )
                    workflow = patch_workflow(
                        load_workflow(mode_settings.workflow_file),
                        pointers=mode_settings.inputs,
                        # 每块换个种子,免得相邻块从同一噪声出发长得雷同。
                        values=timeline_values(
                            plan.board,
                            request=request,
                            config=config,
                            seed=base_seed + index,
                            anchor_b64=anchor,
                            refs_b64=refs_b64,
                            segment_refs_b64=segment_refs_b64,
                            task_type=chunk_task,
                        ),
                    )
                    output = client.render(
                        workflow, on_progress=progress, should_cancel=should_cancel
                    )
                    _require_mp4(output.filename)
                    chunks.append(output.data)
                data = concat_videos(chunks)
            else:
                workflow = patch_workflow(
                    load_workflow(mode_settings.workflow_file),
                    pointers=mode_settings.inputs,
                    values=build_values(request, config=config, uploaded=uploaded),
                )
                output = client.render(
                    workflow, on_progress=progress, should_cancel=should_cancel
                )
                _require_mp4(output.filename)
                data = output.data
        except ComfyUiCancelled as error:
            raise RenderCancelled(str(error)) from error
        except ComfyUiError as error:
            raise RenderError(str(error)) from error
        self._logger.info(
            "生成完成 %s (%.0fs, %.1fMB%s)",
            request.render_id,
            time.monotonic() - started,
            len(data) / 1_048_576,
            f", {len(plans)} 块拼接" if len(plans) > 1 else "",
        )
        return data


def _encode_ref(path: Path) -> str:
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as error:
        raise RenderError(f"参考图读不了: {path}({error})") from error


def _require_mp4(filename: str) -> None:
    if not filename.lower().endswith(".mp4"):
        raise RenderError(
            f"workflow 输出的不是 mp4({filename}),请确认末端是 SaveVideo 节点"
        )


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
