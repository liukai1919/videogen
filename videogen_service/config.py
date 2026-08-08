import os
from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

RenderMode = Literal["t2v", "i2v", "flf2v", "story"]
DirectorProvider = Literal["anthropic", "openai", "ollama"]
_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


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


class DirectorProviderConfig(StrictModel):
    """One selectable writer. The key it is filed under is what the UI shows."""

    provider: DirectorProvider
    model: str
    # Leave unset to let the SDK find its own credentials — ANTHROPIC_API_KEY or
    # an `ant auth login` profile, OPENAI_API_KEY, and nothing at all for
    # ollama. Name a variable here only to override that.
    api_key_env: str | None = None
    # Required for ollama; for the hosted providers this points them at a
    # gateway or compatible endpoint instead of the vendor's own.
    base_url: str | None = None
    timeout_seconds: float = Field(gt=0, default=120.0)
    # Reasoning counts against this on current models, so leave headroom.
    max_tokens: int = Field(gt=0, default=16_000)

    def api_key(self) -> str | None:
        if self.api_key_env is None:
            return None
        return os.environ.get(self.api_key_env) or None


class DirectorConfig(StrictModel):
    """Who rewrites plain language into H3's prompt format, and how it is checked."""

    enabled: bool = True
    guide_file: Path
    default: str
    providers: dict[str, DirectorProviderConfig]
    max_attempts: int = Field(ge=1, le=5, default=2)
    require_verbatim_dialogue: bool = True

    @model_validator(mode="after")
    def validate_providers(self) -> "DirectorConfig":
        if not self.providers:
            raise ValueError("director.providers must not be empty")
        unusable = sorted(name for name in self.providers if not _PROVIDER_ID.match(name))
        if unusable:
            raise ValueError(f"director.providers keys must be [a-z0-9_-]: {unusable}")
        if self.default not in self.providers:
            raise ValueError(
                f"director.default {self.default!r} is not one of "
                f"{sorted(self.providers)}"
            )
        return self


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
