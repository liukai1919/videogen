# Claude Code as a headless worker: `claude -p --output-format json` reads the
# prompt from stdin and prints exactly one JSON document — no terminal UI.
# Security posture (section 20): in non-interactive mode every tool that needs
# permission is simply denied, so by default this is a read-only reasoning
# worker confined to its working_dir. To let it edit files for a coding task,
# grant tools explicitly in config, e.g. args: ["--allowedTools", "Edit,Write"].
# --dangerously-skip-permissions is never set here and should stay that way.

from collections.abc import Sequence
import json
from pathlib import Path
from typing import Any, ClassVar

from videogen_service.agents.base import CliAgentAdapter, Outcome, error_tail

# Fields of Claude's result document worth surfacing to callers.
_METADATA_KEYS = ("session_id", "total_cost_usd", "num_turns", "subtype")


class ClaudeCodeAdapter(CliAgentAdapter):
    default_command: ClassVar[list[str]] = ["claude"]

    def _task_argv(self, scratch: Path, extra: Sequence[str]) -> list[str]:
        return ["-p", "--output-format", "json", *extra]

    def _interpret(
        self, *, exit_code: int, stdout: str, stderr: str, scratch: Path
    ) -> Outcome:
        document = _document(stdout)
        metadata: dict[str, Any] = {}
        if document is not None:
            metadata = {
                key: document[key] for key in _METADATA_KEYS if key in document
            }
        if exit_code != 0:
            detail = (
                str(document.get("result"))
                if document is not None and document.get("result")
                else error_tail(stderr, stdout)
            )
            return Outcome(None, f"claude 退出码 {exit_code}: {detail}", metadata)
        if document is None:
            # Exit 0 but not the JSON we asked for (old CLI?): keep the text
            # rather than discard a paid answer.
            text = stdout.strip()
            error = None if text else "claude 正常退出但没有任何输出"
            return Outcome(text or None, error, {"parse": "raw"})
        if document.get("is_error"):
            detail = str(
                document.get("result") or document.get("subtype") or "未知错误"
            )
            return Outcome(None, f"claude 报错: {detail}", metadata)
        result = document.get("result")
        text = str(result).strip() if isinstance(result, str) else stdout.strip()
        return Outcome(text or None, None if text else "claude 的回答是空的", metadata)


def _document(stdout: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
