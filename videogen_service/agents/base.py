# Base classes for external agent workers — Claude Code, Codex CLI, and
# whatever comes next — used as headless reasoning workers. This is a separate
# thing from videogen_service.agent (the local Ollama chat brain): workers here
# receive one task, answer once, and exit; they never drive our tools directly
# (section 14 — plans come back here, our orchestrator executes them).
#
# AgentAdapter is deliberately not CLI-shaped (section 21): an API-backed or
# local adapter subclasses it directly; CliAgentAdapter adds the subprocess
# machinery every CLI worker shares — resolve the executable, feed the prompt
# over stdin (Windows argv limits are real), enforce the timeout, and kill the
# whole process tree on timeout or cancellation so no orphan burns quota.

from abc import ABC, abstractmethod
import asyncio
from collections.abc import Mapping, Sequence
import json
import logging
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, ClassVar, NamedTuple

from videogen_service.agents.config import AgentWorkerConfig
from videogen_service.agents.models import AgentResult, mint_task_id

LOGGER = logging.getLogger("videogen_service.agents")

_HEALTH_TIMEOUT_SECONDS = 30.0
_ERROR_TAIL_CHARS = 400


class AgentAdapter(ABC):
    """One external reasoning worker. run() never raises for runtime failures
    (missing CLI, timeout, non-zero exit) — it reports them in AgentResult so
    a fallback chain can read them and move on."""

    name: str = "agent"

    @property
    def enabled(self) -> bool:
        return True

    @abstractmethod
    async def run(
        self,
        task: str,
        context: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> AgentResult: ...

    @abstractmethod
    async def health_check(self) -> bool: ...


class Outcome(NamedTuple):
    """What a CLI adapter distils from a finished process: the extracted
    answer, or an error, plus per-CLI metadata."""

    output: str | None
    error: str | None
    metadata: dict[str, Any]


def compose_prompt(task: str, context: Mapping[str, Any] | None) -> str:
    """The task plus optional structured context in one prompt. Context is
    fenced JSON so every CLI reads the same shape."""

    if not context:
        return task
    rendered = json.dumps(dict(context), ensure_ascii=False, indent=2, default=str)
    return f"{task}\n\n## Context\n```json\n{rendered}\n```"


class CliAgentAdapter(AgentAdapter):
    """Shared subprocess plumbing; subclasses supply the headless argv and
    interpret the process output."""

    # Executable plus fixed leading args when config leaves command unset.
    default_command: ClassVar[list[str]] = []

    def __init__(
        self, name: str, settings: AgentWorkerConfig, *, workspace: Path
    ) -> None:
        self.name = name
        self._settings = settings
        self._workspace = workspace

    @property
    def enabled(self) -> bool:
        return self._settings.enabled

    def command(self) -> list[str]:
        raw = self._settings.command
        if raw is None:
            return list(self.default_command)
        return [raw] if isinstance(raw, str) else list(raw)

    def executable(self) -> str | None:
        """Full path of the CLI, or None when it is not installed. which()
        also resolves Windows launcher shims (claude.cmd, codex.exe)."""
        return shutil.which(self.command()[0])

    async def health_check(self) -> bool:
        if not self.enabled:
            return False
        executable = self.executable()
        if executable is None:
            return False
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *self.command()[1:],
                "--version",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **_spawn_kwargs(),
            )
        except OSError:
            return False
        try:
            await asyncio.wait_for(process.communicate(), _HEALTH_TIMEOUT_SECONDS)
        except TimeoutError:
            _terminate(process)
            await process.wait()
            return False
        except asyncio.CancelledError:
            _terminate(process)
            await process.wait()
            raise
        return process.returncode == 0

    async def run(
        self,
        task: str,
        context: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> AgentResult:
        task_id = mint_task_id()
        started = time.monotonic()
        limit = timeout if timeout is not None else self._settings.timeout_seconds
        if not self.enabled:
            return self._finish(
                task_id, started, error=f"agent {self.name} 已在配置里停用"
            )
        executable = self.executable()
        if executable is None:
            return self._finish(
                task_id,
                started,
                error=f"命令 {self.command()[0]} 不存在,先安装这个 CLI",
            )
        prompt = compose_prompt(task, context)
        self._workspace.mkdir(parents=True, exist_ok=True)
        # 日志不带 prompt 原文(section 17):它可能包含敏感内容,debug 才有。
        LOGGER.info(
            "agent=%s task_id=%s 启动(prompt %d 字,限时 %.0fs)",
            self.name,
            task_id,
            len(prompt),
            limit,
        )
        LOGGER.debug("agent=%s task_id=%s prompt:\n%s", self.name, task_id, prompt)
        # Per-run scratch dir for CLI byproducts (e.g. codex --output-last-message)
        # so nothing lands in the workspace the CLI is pointed at.
        with tempfile.TemporaryDirectory(prefix=f"agent-{self.name}-") as scratch_name:
            scratch = Path(scratch_name)
            argv = [
                executable,
                *self.command()[1:],
                *self._task_argv(scratch, self._settings.args),
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self._workspace,
                    **_spawn_kwargs(),
                )
            except OSError as error:
                return self._finish(
                    task_id, started, error=f"启动 {self.name} CLI 失败: {error}"
                )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(prompt.encode("utf-8")), limit
                )
            except TimeoutError:
                _terminate(process)
                await process.wait()
                return self._finish(
                    task_id,
                    started,
                    error=f"超时({limit:.0f}s),已终止 {self.name} CLI",
                    metadata={"timeout": True},
                )
            except asyncio.CancelledError:
                # 主任务取消时(section 19),外部进程必须跟着死。
                _terminate(process)
                await process.wait()
                raise
            outcome = self._interpret(
                exit_code=process.returncode or 0,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                scratch=scratch,
            )
        return self._finish(
            task_id,
            started,
            output=outcome.output,
            error=outcome.error,
            raw_output=stdout_bytes.decode("utf-8", errors="replace"),
            metadata={"exit_code": process.returncode, **outcome.metadata},
        )

    def _task_argv(self, scratch: Path, extra: Sequence[str]) -> list[str]:
        """Arguments after the base command that make this CLI run one
        headless task, prompt on stdin; `extra` is config args."""
        raise NotImplementedError

    def _interpret(
        self, *, exit_code: int, stdout: str, stderr: str, scratch: Path
    ) -> Outcome:
        raise NotImplementedError

    def _finish(
        self,
        task_id: str,
        started: float,
        *,
        output: str | None = None,
        error: str | None = None,
        raw_output: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentResult:
        duration_ms = int((time.monotonic() - started) * 1000)
        result = AgentResult(
            success=error is None,
            agent=self.name,
            output=output,
            error=error,
            raw_output=raw_output,
            duration_ms=duration_ms,
            metadata={"task_id": task_id, **(metadata or {})},
        )
        LOGGER.info(
            "agent=%s task_id=%s 结束 success=%s duration_ms=%d%s",
            self.name,
            task_id,
            result.success,
            duration_ms,
            "" if error is None else f" error={error}",
        )
        LOGGER.debug("agent=%s task_id=%s output:\n%s", self.name, task_id, output)
        return result


def error_tail(*candidates: str) -> str:
    """The last part of the first non-empty stream — where CLIs put the reason."""
    for candidate in candidates:
        text = candidate.strip()
        if text:
            return text[-_ERROR_TAIL_CHARS:]
    return "无输出"


def _spawn_kwargs() -> dict[str, Any]:
    # Own process group/session, so _terminate can take down the CLI *and*
    # every helper it forked (node CLIs spawn several).
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass
