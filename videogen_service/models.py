from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from videogen_service.config import RenderMode


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class H3Prompt(StrictModel):
    """The three fields MiniMax H3 reads. Field docs double as the LLM's schema."""

    integrated_multimodal_description: str = Field(
        description=(
            "The shot-by-shot body, opening with [Shot 1] and a style word. Later "
            "shots start '[Shot N] At MM:SS.mmm, the camera cuts to'. Carries "
            "action, camera moves, speaker IDs and <d> dialogue."
        )
    )
    overall_soundscape: str = Field(
        description=(
            "1-4 sentences of ambience and action sound effects for the whole "
            "clip. Never dialogue, singing or in-scene music. 'N/A' if silent."
        )
    )
    non_diegetic_music: str = Field(
        description=(
            "1-3 sentences of score the characters cannot hear: instruments, "
            "tempo, rhythm, dynamics. Never mood words. 'N/A' if there is none."
        )
    )

    def render(self) -> str:
        return (
            f"integrated_multimodal_description: {self.integrated_multimodal_description}"
            f"\n\noverall_soundscape: {self.overall_soundscape}"
            f"\n\nnon_diegetic_music: {self.non_diegetic_music}"
        )


class EnhanceRequest(StrictModel):
    mode: RenderMode
    prompt: str = Field(min_length=1, max_length=50_000)
    seconds: float = Field(gt=0)
    # One of the ids /health advertises. Omitted means the configured default.
    provider: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")


class EnhanceResponse(StrictModel):
    prompt: str
    enhanced: bool
    provider: str
    fields: H3Prompt | None = None
    warnings: list[str] = Field(default_factory=list)
    seconds: float


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
    updated_at: str


class RenderView(StrictModel):
    render_id: str
    status: RenderStatus
    progress: RenderProgress | None = None
    error: str | None = None
    media_url: str | None = None
