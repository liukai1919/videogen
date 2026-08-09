# The one result type every external agent call returns. Upper layers read
# success/output/error and never touch a CLI's own output format.

from dataclasses import dataclass, field
from datetime import UTC, datetime
import secrets
from typing import Any


@dataclass
class AgentResult:
    success: bool
    agent: str
    # The agent's final answer, already extracted from whatever the CLI printed.
    output: str | None = None
    error: str | None = None
    # The CLI's untouched stdout, for debugging and audits.
    raw_output: str | None = None
    duration_ms: int | None = None
    # task_id, exit_code, per-CLI extras (session_id, cost…), fallback attempts.
    metadata: dict[str, Any] = field(default_factory=dict)


def mint_task_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"agent_{stamp}_{secrets.token_hex(3)}"
