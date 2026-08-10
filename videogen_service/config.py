from pathlib import Path
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from videogen_service.agents.config import AgentDefaults, AgentWorkerConfig

RenderMode = Literal["t2v", "i2v", "flf2v", "story", "r2v"]


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
    # story 模式的跨段连贯:Director 节点把上一段尾帧和外观参考带进下一段,
    # 角色/场景跨段一致的工程解。代价是每段画布多出前缀帧,显存/内存占用
    # 上升——本机 32GB 红线下先默认关,试通过再开。
    continuous_reference: bool = False

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


class ScriptConfig(StrictModel):
    """Turning a YouTube URL into a storyboard: captions in, shot list out."""

    enabled: bool = True
    # Falls back to ollama.model so a single-model install configures nothing.
    model: str | None = None
    temperature: float = Field(default=0.4, ge=0, le=2)
    timeout_seconds: float = Field(default=600, gt=0)
    fetch_timeout_seconds: float = Field(default=60, gt=0)
    subtitle_languages: list[str] = Field(
        default_factory=lambda: ["zh-Hans", "zh", "en"], min_length=1
    )
    max_transcript_chars: int = Field(default=24_000, gt=0)
    max_source_seconds: float = Field(default=14_400, gt=0)
    target_seconds: float = Field(default=60, gt=0)
    shot_seconds: float = Field(default=6, gt=0)
    max_shots: int = Field(default=8, gt=0)
    # 中性电影感兜底:题材气质由 Skill 或请求的 style 供给,这里不预设
    # 题材(科普质感曾作为默认漏进氛围片,R2 S9 记档)。
    style: str = (
        "写实电影质感,光线与色调全片统一,画面里不要出现文字、字幕或水印"
    )
    cookies_file: Path | None = None
    proxy: str | None = None


class ComfyCapability(StrictModel):
    """A ComfyUI-backed generation capability, e.g. FLUX 文生图."""

    kind: Literal["t2i"]
    workflow_file: Path
    # Same "<node_id>.<field>" pointers the render modes use.
    inputs: dict[str, list[str]]
    needs_gpu: bool = True

    @model_validator(mode="after")
    def validate_inputs(self) -> "ComfyCapability":
        if "prompt" not in self.inputs:
            raise ValueError("t2i capability inputs must include prompt")
        unknown = set(self.inputs) - {
            "prompt",
            "negative_prompt",
            "width",
            "height",
            "seed",
        }
        if unknown:
            raise ValueError(f"unknown capability inputs: {sorted(unknown)}")
        return self


class CommandCapability(StrictModel):
    """A local-command capability, e.g. TTS 配音. The command template runs on
    this machine with {text_file} and {output_file} substituted; no cloud API
    is ever involved."""

    kind: Literal["tts"]
    command: list[str] = Field(min_length=1)
    output_suffix: str = ".wav"
    timeout_seconds: float = Field(default=600, gt=0)
    needs_gpu: bool = False

    @model_validator(mode="after")
    def validate_command(self) -> "CommandCapability":
        joined = " ".join(self.command)
        if "{text_file}" not in joined or "{output_file}" not in joined:
            raise ValueError(
                "tts capability command must reference {text_file} and {output_file}"
            )
        if not self.output_suffix.startswith("."):
            raise ValueError("output_suffix must start with a dot")
        return self


Capability = ComfyCapability | CommandCapability


class ModeConfig(StrictModel):
    workflow_file: Path
    inputs: dict[str, list[str]]
    # 参考主体生视频(r2v):接受参考图并要求至少一张,提示词用 <Picture N>
    # 引用。需要 ref2va 权重的 workflow;fl2va 系模式保持 False。
    accepts_refs: bool = False

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
    # 具名创作预设(SKILL.md + meta.yaml)所在目录;不存在时 Skill 列表为空。
    skills_dir: Path = Path("skills")
    comfyui: ComfyUiConfig
    renderer: RendererConfig
    ollama: OllamaConfig
    script: ScriptConfig = Field(default_factory=ScriptConfig)
    modes: dict[RenderMode, ModeConfig]
    # 平台能力注册表:除 H3 视频(modes)之外的本地生成能力(文生图、TTS…)。
    capabilities: dict[str, Capability] = Field(default_factory=dict)
    # 外部 Agent worker(Claude Code / Codex CLI):云端推理层,按名字点用。
    # 本地生成不经过它们;没配置时整个层不存在,一切照旧。
    agents: dict[str, AgentWorkerConfig] = Field(default_factory=dict)
    agent_defaults: AgentDefaults = Field(default_factory=AgentDefaults)

    @model_validator(mode="after")
    def validate_modes(self) -> "ServiceConfig":
        if self.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("unauthenticated videogen service must listen on loopback")
        if not self.modes:
            raise ValueError("at least one render mode is required")
        for capability_id in self.capabilities:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", capability_id):
                raise ValueError(
                    f"capability id 只能是字母数字下划线连字符: {capability_id}"
                )
        # 配了 agent 却让一个没配的当 primary,fallback 链一启动就空转。
        # fallback 里未配置的名字允许存在(运行时跳过),primary 必须真实。
        if self.agents and self.agent_defaults.primary not in self.agents:
            raise ValueError(
                f"agent_defaults.primary '{self.agent_defaults.primary}' "
                f"不在 agents 配置里: {sorted(self.agents)}"
            )
        # A generated storyboard is submitted through the same /v1/renders seam
        # as a hand-written one, so it has to fit the renderer's own limits.
        if self.script.max_shots > self.renderer.max_shots:
            raise ValueError("script max_shots must not exceed renderer max_shots")
        if not (
            self.renderer.min_seconds
            <= self.script.shot_seconds
            <= self.renderer.max_seconds
        ):
            raise ValueError(
                "script shot_seconds must sit inside the renderer's shot range"
            )
        return self


def load_config(path: str | Path = "config.yaml") -> ServiceConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as config_file:
        raw = yaml.safe_load(config_file)
    config = ServiceConfig.model_validate(raw)
    base_dir = config_path.parent
    work_dir = _resolve_path(base_dir, config.work_dir)
    skills_dir = _resolve_path(base_dir, config.skills_dir)
    modes = {
        mode: settings.model_copy(
            update={"workflow_file": _resolve_path(base_dir, settings.workflow_file)}
        )
        for mode, settings in config.modes.items()
    }
    script = config.script
    if script.cookies_file is not None:
        script = script.model_copy(
            update={"cookies_file": _resolve_path(base_dir, script.cookies_file)}
        )
    capabilities: dict[str, Capability] = {}
    for capability_id, capability in config.capabilities.items():
        if isinstance(capability, ComfyCapability):
            capability = capability.model_copy(
                update={
                    "workflow_file": _resolve_path(
                        base_dir, capability.workflow_file
                    )
                }
            )
        capabilities[capability_id] = capability
    agents = {
        name: (
            worker.model_copy(
                update={"working_dir": _resolve_path(base_dir, worker.working_dir)}
            )
            if worker.working_dir is not None
            else worker
        )
        for name, worker in config.agents.items()
    }
    return config.model_copy(
        update={
            "work_dir": work_dir,
            "skills_dir": skills_dir,
            "modes": modes,
            "script": script,
            "capabilities": capabilities,
            "agents": agents,
        }
    )


def _resolve_path(base_dir: Path, path: Path) -> Path:
    return path.resolve() if path.is_absolute() else (base_dir / path).resolve()
