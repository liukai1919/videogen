# External agent worker layer: cloud coding/reasoning CLIs (Claude Code,
# Codex; Phase 2: Grok, Gemini) as headless workers behind one interface.
#
#   Cloud agents  = reasoning layer   (this package)
#   Our service   = orchestration     (videogen_service.agent + pipeline)
#   5090/ComfyUI  = execution layer   (renders, jobs)
#
# Usage:
#     manager = build_manager(config)          # from config.yaml's agents:
#     result = await manager.run("claude", task="…", context={…})
#     result = await manager.run_with_fallback(task="…")

from videogen_service.agents.base import (
    AgentAdapter,
    CliAgentAdapter,
    compose_prompt,
)
from videogen_service.agents.config import AgentDefaults, AgentWorkerConfig
from videogen_service.agents.exceptions import (
    AgentConfigError,
    AgentWorkerError,
    UnknownAgent,
)
from videogen_service.agents.manager import AgentManager, build_manager
from videogen_service.agents.models import AgentResult

__all__ = [
    "AgentAdapter",
    "AgentConfigError",
    "AgentDefaults",
    "AgentManager",
    "AgentResult",
    "AgentWorkerConfig",
    "AgentWorkerError",
    "CliAgentAdapter",
    "UnknownAgent",
    "build_manager",
    "compose_prompt",
]
