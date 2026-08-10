# OpenAI Codex CLI as a headless worker: `codex exec` runs one non-interactive
# turn. The final assistant message is written to --output-last-message (the
# stable machine seam); stdout carries the transcript and is kept as
# raw_output. Security posture (section 20): --sandbox read-only keeps it a
# reasoning worker; a coding task that must write files can override via
# config args (e.g. ["--sandbox", "workspace-write"]).

from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from videogen_service.agents.base import CliAgentAdapter, Outcome, error_tail

_LAST_MESSAGE = "last_message.txt"


class CodexAdapter(CliAgentAdapter):
    default_command: ClassVar[list[str]] = ["codex"]

    def _task_argv(self, scratch: Path, extra: Sequence[str]) -> list[str]:
        return [
            "exec",
            "--sandbox",
            "read-only",
            # working_dir 是个隔离的工作区,不必是 git 仓库。
            "--skip-git-repo-check",
            "--output-last-message",
            str(scratch / _LAST_MESSAGE),
            *extra,
            # "-" 表示 prompt 从 stdin 读,绕开 Windows 的 argv 长度限制。
            "-",
        ]

    def _interpret(
        self, *, exit_code: int, stdout: str, stderr: str, scratch: Path
    ) -> Outcome:
        if exit_code != 0:
            return Outcome(
                None, f"codex 退出码 {exit_code}: {error_tail(stderr, stdout)}", {}
            )
        last = scratch / _LAST_MESSAGE
        if last.is_file():
            message = last.read_text(encoding="utf-8", errors="replace").strip()
            if message:
                return Outcome(message, None, {})
        # Older codex without the flag, or an empty answer: stdout as best effort.
        text = stdout.strip()
        return Outcome(
            text or None,
            None if text else "codex 正常退出但没有任何输出",
            {"parse": "raw"},
        )
