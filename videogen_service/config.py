import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

RenderMode = Literal["t2v", "i2v", "flf2v", "story"]
DirectorProvider = Literal["anthropic", "ollama"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ComfyUiConfig(StrictModel):
    base_url: str
    timeout_seconds: float = Field(gt=0)
    poll_interval_seconds: float = Field(gt=0)


class RendererConfig(StrictModel):
    fps: int = Field(gt=0)
    min_seconds: float = Field(gt=0)
    max_seconds: float = Field(gt=0)
    max_total_seconds: float = Field(gt=0)
    max_shots: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_ranges(self) -> "RendererConfig":
        if self.min_seconds >= self.max_seconds:
            raise ValueError("renderer min_seconds must be below max_seconds")
        if self.max_total_seconds < self.max_seconds:
            raise ValueError("renderer max_total_seconds must reach max_seconds")
        return self


class OllamaConfig(StrictModel):
    unload_before_render: bool
    base_url: str
    model: str


class DirectorConfig(StrictModel):
    """Who rewrites plain language into H3's prompt format, and how it is checked."""

    enabled: bool = True
    provider: DirectorProvider = "anthropic"
    model: str
    guide_file: Path
    # Only read when provider is "anthropic"; unset is fine, the SDK also
    # resolves an `ant auth login` profile.
    api_key_env: str = "ANTHROPIC_API_KEY"
    # Only read when provider is "ollama".
    base_url: str = "http://127.0.0.1:11434"
    timeout_seconds: float = Field(gt=0, default=120.0)
    # Thinking counts against this on current Claude models, so leave headroom.
    max_tokens: int = Field(gt=0, default=16_000)
    max_attempts: int = Field(ge=1, le=5, default=2)
    require_verbatim_dialogue: bool = True

    def api_key(self) -> str | None:
        return os.environ.get(self.api_key_env) or None


class ModeConfig(StrictModel):
    workflow_file: Path
    inputs: dict[str, list[str]]

    @model_validator(mode="after")
    def validate_inputs(self) -> "ModeConfig":
        required = {"prompt", "width", "height", "length", "seed"}
        # Small fake configs used at the API seam need only identify their mode;
        # production is checked when H3Renderer is constructed.
        if not self.inputs:
            raise ValueError("mode inputs must not be empty")
        unknown = set(self.inputs) - required - {"first_frame", "last_frame", "timeline"}
        if unknown:
            raise ValueError(f"unknown workflow inputs: {sorted(unknown)}")
        return self


class ServiceConfig(StrictModel):
    host: str
    port: int = Field(ge=1, le=65_535)
    work_dir: Path
    comfyui: ComfyUiConfig
    renderer: RendererConfig
    ollama: OllamaConfig
    modes: dict[RenderMode, ModeConfig]
    director: DirectorConfig | None = None

    @model_validator(mode="after")
    def validate_modes(self) -> "ServiceConfig":
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("unauthenticated videogen service must listen on loopback")
        if not self.modes:
            raise ValueError("at least one render mode is required")
        return self


def load_config(path: str | Path = "config.yaml") -> ServiceConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file)
    config = ServiceConfig.model_validate(raw)
    base_dir = config_path.parent
    work_dir = _resolve_path(base_dir, config.work_dir)
    modes = {
        mode: settings.model_copy(
            update={"workflow_file": _resolve_path(base_dir, settings.workflow_file)}
        )
        for mode, settings in config.modes.items()
    }
    director = (
        None
        if config.director is None
        else config.director.model_copy(
            update={"guide_file": _resolve_path(base_dir, config.director.guide_file)}
        )
    )
    return config.model_copy(
        update={"work_dir": work_dir, "modes": modes, "director": director}
    )


def _resolve_path(base_dir: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()
