from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from videogen_service.config import RenderMode


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenderSpec(StrictModel):
    render_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    mode: RenderMode
    prompt: str = Field(min_length=1, max_length=50_000)
    width: int = Field(ge=32, le=8192)
    height: int = Field(ge=32, le=8192)
    seconds: float = Field(gt=0)
    seed: int | None = Field(default=None, ge=0)


class RenderValidationRequest(StrictModel):
    mode: RenderMode
    prompt: str = Field(min_length=1, max_length=50_000)
    width: int = Field(ge=32, le=8192)
    height: int = Field(ge=32, le=8192)
    seconds: float = Field(gt=0)
    seed: int | None = Field(default=None, ge=0)
    has_first_frame: bool = False
    has_last_frame: bool = False


class RenderValidation(StrictModel):
    mode: RenderMode
    timeline_mode: bool
    seconds: float


class RenderRequest(RenderSpec):
    first_frame: Path | None = None
    last_frame: Path | None = None
    # r2v 的参考图(身份锚点),按序对应提示词里的 <Picture 1>…<Picture N>。
    refs: list[Path] = Field(default_factory=list)


class RenderProgress(StrictModel):
    percent: float = Field(ge=0, le=1)
    step: int = Field(ge=0)
    total_steps: int = Field(gt=0)
    pass_index: int = Field(gt=0)
    pass_total: int = Field(gt=0)


RenderStatus = Literal["QUEUED", "RUNNING", "DONE", "FAILED"]


class RenderRecord(StrictModel):
    schema_version: Literal[1] = 1
    render_id: str
    fingerprint: str
    status: RenderStatus
    progress: RenderProgress | None = None
    error: str | None = None
    created_at: str
    # Stamped each time the worker picks the render up, so the console can show
    # how long the card has been busy without counting queue time. Absent in
    # state files written before the console existed.
    started_at: str | None = None
    updated_at: str


class RenderSummary(StrictModel):
    """A render as the console lists it: state plus the spec that produced it.

    Kept apart from RenderView because that one is a frozen contract with the
    sibling control plane, which rejects fields it does not know.
    """

    render_id: str
    status: RenderStatus
    progress: RenderProgress | None = None
    error: str | None = None
    media_url: str | None = None
    mode: RenderMode
    prompt: str
    width: int
    height: int
    seconds: float
    seed: int | None = None
    has_first_frame: bool = False
    has_last_frame: bool = False
    created_at: str
    updated_at: str
    elapsed_seconds: float | None = None


class ScriptOptions(StrictModel):
    """The storyboard knobs shared by every script source (YouTube, 文档)."""

    target_seconds: float | None = Field(default=None, gt=0)
    shot_seconds: float | None = Field(default=None, gt=0)
    max_shots: int | None = Field(default=None, gt=0)
    # Extra direction for the summariser, e.g. "面向初中生,多讲生活里的例子".
    guidance: str | None = Field(default=None, max_length=2_000)
    style: str | None = Field(default=None, max_length=2_000)
    output_language: str = Field(default="中文", min_length=1, max_length=40)
    # Named preset from skills/: fills the fields above where they are unset
    # and injects its SKILL.md into the summary prompt.
    skill: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    # When set, the finished script is also archived as a draft of the project.
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")


class ScriptRequest(ScriptOptions):
    url: str = Field(min_length=1, max_length=2_048)


class ScriptShot(StrictModel):
    seconds: float = Field(gt=0)
    prompt: str = Field(min_length=1)
    narration: str = ""
    # 该镜是否是上一镜同一动作的直接延续:时间轴上写成 [a s-b s续],
    # 拆链渲染在块边界处会用上一块的最后一帧做 i2v 接力。
    continues: bool = False


class ScriptSource(StrictModel):
    # Discriminator with a default, so records written before document sources
    # existed still parse as the YouTube shape they always were.
    kind: Literal["youtube"] = "youtube"
    video_id: str
    title: str
    url: str
    subtitle_language: str
    automatic_captions: bool
    duration_seconds: float
    transcript_chars: int
    transcript_truncated: bool


class DocumentSource(StrictModel):
    """A local file (PDF/Word/纯文本) whose text seeded the storyboard."""

    kind: Literal["document"] = "document"
    filename: str
    chars: int
    truncated: bool


class ScriptResult(StrictModel):
    source: ScriptSource | DocumentSource = Field(discriminator="kind")
    title: str
    summary: str
    mode: RenderMode
    # Ready to post to /v1/renders as-is: preamble plus [0s-6s] shot headers.
    prompt: str
    seconds: float
    shots: list[ScriptShot]


class ProjectCreateRequest(StrictModel):
    # Optional so a page can mint its own id the way it already does for
    # renders; left out, the service mints one.
    project_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,80}$")
    name: str = Field(min_length=1, max_length=120)


class ProjectRenderLink(StrictModel):
    render_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class MemoryAddRequest(StrictModel):
    text: str = Field(min_length=1, max_length=500)


class JobSaveAssetRequest(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="生成图", max_length=40)


class ChatCreateRequest(StrictModel):
    title: str | None = Field(default=None, max_length=120)


class ChatSendRequest(StrictModel):
    text: str = Field(min_length=1, max_length=20_000)
    # 这一轮由哪个外部 Agent worker 回答(claude/codex…);省略 = 本地大脑。
    agent: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,40}$")


class AssetFromRenderRequest(StrictModel):
    """Archive a submitted render's reference frame as a reusable asset."""

    render_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    slot: Literal["first", "last"]
    name: str = Field(min_length=1, max_length=120)
    category: str = Field(default="参考帧", max_length=40)


class RenderView(StrictModel):
    render_id: str
    status: RenderStatus
    progress: RenderProgress | None = None
    error: str | None = None
    media_url: str | None = None
