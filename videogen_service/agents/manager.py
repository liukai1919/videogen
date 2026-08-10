# The single seam business code talks to. Nothing above this file may spawn a
# CLI itself: it asks the manager for an agent by name, or lets the configured
# primary/fallback chain decide. One task goes to one agent (section 12);
# review() is the deliberate exception and runs a named set in parallel.

import asyncio
from collections.abc import Mapping, Sequence
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from videogen_service.agents.adapters import ClaudeCodeAdapter, CodexAdapter
from videogen_service.agents.base import AgentAdapter, CliAgentAdapter
from videogen_service.agents.config import AgentDefaults
from videogen_service.agents.exceptions import AgentConfigError, UnknownAgent
from videogen_service.agents.models import AgentResult

if TYPE_CHECKING:
    # Runtime import would be circular: the root config module imports
    # agents.config, which initialises this package.
    from videogen_service.config import ServiceConfig

LOGGER = logging.getLogger("videogen_service.agents")

# Adapter per agent id this build knows how to construct. Phase 2 adds
# "grok" and "gemini" here — one line each, no business code changes.
_ADAPTERS: dict[str, type[CliAgentAdapter]] = {
    "claude": ClaudeCodeAdapter,
    "codex": CodexAdapter,
}


class AgentManager:
    def __init__(self, *, defaults: AgentDefaults | None = None) -> None:
        self._adapters: dict[str, AgentAdapter] = {}
        self._defaults = defaults or AgentDefaults()

    def register(self, name: str, adapter: AgentAdapter) -> None:
        self._adapters[name] = adapter

    def adapter(self, name: str) -> AgentAdapter:
        adapter = self._adapters.get(name)
        if adapter is None:
            raise UnknownAgent(
                f"没有注册的 agent: {name};已注册: {self.registered_agents()}"
            )
        return adapter

    def registered_agents(self) -> list[str]:
        return sorted(self._adapters)

    def available_agents(self) -> list[str]:
        return sorted(
            name for name, adapter in self._adapters.items() if adapter.enabled
        )

    async def health_check(self) -> dict[str, bool]:
        """Which registered CLIs are actually installed and runnable. Never
        raises — a missing agent must not take the video service down."""
        names = self.registered_agents()
        checks = await asyncio.gather(
            *(self._adapters[name].health_check() for name in names)
        )
        return dict(zip(names, checks))

    async def run(
        self,
        agent: str,
        task: str,
        context: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> AgentResult:
        """One task, one agent. Runtime failures come back inside AgentResult;
        only an unregistered name raises."""
        return await self.adapter(agent).run(task, context=context, timeout=timeout)

    async def run_with_fallback(
        self,
        task: str,
        agents: Sequence[str] | None = None,
        context: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> AgentResult:
        """Tries each agent in order until one succeeds; None → the configured
        primary + fallback chain. Every attempt is recorded in the returned
        result's metadata["attempts"]."""
        chain = list(agents) if agents else [
            self._defaults.primary,
            *self._defaults.fallback,
        ]
        attempts: list[dict[str, Any]] = []
        last: AgentResult | None = None
        for name in dict.fromkeys(chain):
            if name not in self._adapters:
                attempts.append({"agent": name, "error": "未注册,跳过"})
                continue
            result = await self.run(name, task, context=context, timeout=timeout)
            attempts.append(
                {
                    "agent": name,
                    "success": result.success,
                    "error": result.error,
                    "duration_ms": result.duration_ms,
                }
            )
            if result.success:
                result.metadata["attempts"] = attempts
                return result
            LOGGER.warning(
                "agent=%s 失败(%s),fallback 到下一个", name, result.error
            )
            last = result
        if last is None:
            raise UnknownAgent(f"fallback 链里没有任何已注册的 agent: {chain}")
        last.metadata["attempts"] = attempts
        return last

    async def review(
        self,
        task: str,
        agents: Sequence[str],
        context: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, AgentResult]:
        """Multi-agent review: the same task to several agents in parallel,
        every answer returned. Judging/debate is deliberately not built yet
        (section 13) — callers compare the answers themselves."""
        names = list(dict.fromkeys(agents))
        for name in names:
            self.adapter(name)  # unknown names fail before any quota is spent
        results = await asyncio.gather(
            *(self.run(name, task, context=context, timeout=timeout) for name in names)
        )
        return dict(zip(names, results))


def build_manager(config: "ServiceConfig") -> AgentManager:
    """The manager as config.yaml describes it. An unsupported agent id fails
    here — at startup — not in the middle of a task."""
    manager = AgentManager(defaults=config.agent_defaults)
    default_workspace = config.work_dir / ".agents" / "workspace"
    for name, settings in sorted(config.agents.items()):
        adapter_type = _ADAPTERS.get(name)
        if adapter_type is None:
            raise AgentConfigError(
                f"不认识的 agent '{name}';目前支持: {sorted(_ADAPTERS)}"
            )
        workspace: Path = settings.working_dir or default_workspace
        manager.register(name, adapter_type(name, settings, workspace=workspace))
    return manager
