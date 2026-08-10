# Configuration models for the external agent worker layer. They live apart
# from the root ServiceConfig module so adapters and tests can import them
# without pulling in the whole service configuration.

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AgentWorkerConfig(StrictModel):
    """One external CLI worker (claude/codex/…)."""

    enabled: bool = True
    # Executable name/path, optionally with fixed leading arguments
    # (e.g. ["python", "fake_cli.py"] in tests). None → the adapter's default.
    command: str | list[str] | None = None
    # Extra CLI arguments appended after the adapter's own headless flags,
    # e.g. ["--allowedTools", "Edit"] to let Claude Code touch files.
    args: list[str] = Field(default_factory=list)
    # 每次外部调用的硬超时(section 18):subprocess 永远不允许挂死。
    timeout_seconds: float = Field(default=300, gt=0)
    # The only directory the CLI runs in (section 20). None → the service's
    # work_dir/.agents/workspace, an empty sandbox with nothing to leak.
    working_dir: Path | None = None

    @model_validator(mode="after")
    def validate_command(self) -> "AgentWorkerConfig":
        if isinstance(self.command, list) and not self.command:
            raise ValueError("agent command 列表不能为空")
        return self


class AgentDefaults(StrictModel):
    """Which worker takes a task first, and who picks it up on failure."""

    primary: str = "claude"
    fallback: list[str] = Field(default_factory=lambda: ["codex"])
