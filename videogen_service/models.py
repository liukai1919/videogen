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


class ScriptRequest(StrictModel):
    url: str = Field(min_length=1, max_length=2_048)
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


class ScriptShot(StrictModel):
    seconds: float = Field(gt=0)
    prompt: str = Field(min_length=1)
    narration: str = ""


class ScriptSource(StrictModel):
    video_id: str
    title: str
    url: str
    subtitle_language: str
    automatic_captions: bool
    duration_seconds: float
    transcript_chars: int
    transcript_truncated: bool


class ScriptResult(StrictModel):
    source: ScriptSource
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


class RenderView(StrictModel):
    render_id: str
    status: RenderStatus
    progress: RenderProgress | None = None
    error: str | None = None
    media_url: str | None = None
