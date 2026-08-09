# The external agent worker layer, tested against fake CLIs (tiny Python
# scripts standing in for claude/codex) so no real subscription quota is ever
# spent here. Real-CLI checks live at the bottom: --version probes run
# whenever the CLI is installed; actual tasks only with AGENT_CLI_TESTS=1.

import asyncio
from collections.abc import Mapping
import json
import os
from pathlib import Path
import shutil
import sys
import textwrap
from typing import Any

from fastapi.testclient import TestClient
import pytest

from tests.test_agent import FakeRunner as JobFakeRunner
from tests.test_agent import ScriptedChat
from tests.test_agent import make_config as make_agent_config
from tests.test_api import FakeRenderer, make_config
from videogen_service.agents import (
    AgentConfigError,
    AgentDefaults,
    AgentManager,
    AgentResult,
    AgentWorkerConfig,
    UnknownAgent,
    build_manager,
)
from videogen_service.agents.adapters import ClaudeCodeAdapter, CodexAdapter
from videogen_service.agents.base import AgentAdapter
from videogen_service.api import create_app


def script_command(tmp_path: Path, name: str, body: str) -> list[str]:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return [sys.executable, str(path)]


_FAKE_CLAUDE_OK = """
    import json, sys
    if "--version" in sys.argv[1:]:
        print("fake-claude 1.0.0"); sys.exit(0)
    prompt = sys.stdin.read()
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "echo:" + prompt, "session_id": "sess-1",
        "total_cost_usd": 0.001, "num_turns": 1,
    }))
"""

_FAKE_CLAUDE_QUOTA = """
    import json, sys
    if "--version" in sys.argv[1:]:
        print("fake-claude 1.0.0"); sys.exit(0)
    sys.stdin.read()
    print(json.dumps({"type": "result", "is_error": True, "result": "配额用完了"}))
"""

_FAKE_CRASH = """
    import sys
    if "--version" in sys.argv[1:]:
        sys.exit(0)
    sys.stdin.read()
    print("boom", file=sys.stderr)
    sys.exit(3)
"""

_FAKE_PLAIN_TEXT = """
    import sys
    if "--version" in sys.argv[1:]:
        sys.exit(0)
    sys.stdin.read()
    print("plain text answer")
"""

_FAKE_SLEEPER = """
    import sys, time
    if "--version" in sys.argv[1:]:
        sys.exit(0)
    time.sleep(30)
"""

_FAKE_CODEX_OK = """
    import sys
    args = sys.argv[1:]
    if "--version" in args:
        print("fake-codex 1.0.0"); sys.exit(0)
    prompt = sys.stdin.read()
    path = args[args.index("--output-last-message") + 1]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("codex answer: " + prompt.strip())
    print("transcript noise")
"""

_FAKE_CODEX_NO_FILE = """
    import sys
    if "--version" in sys.argv[1:]:
        sys.exit(0)
    sys.stdin.read()
    print("stdout answer")
"""


def claude_adapter(
    tmp_path: Path, command: list[str], **overrides: Any
) -> ClaudeCodeAdapter:
    settings = AgentWorkerConfig(command=command, **overrides)
    return ClaudeCodeAdapter("claude", settings, workspace=tmp_path / "ws")


def codex_adapter(
    tmp_path: Path, command: list[str], **overrides: Any
) -> CodexAdapter:
    settings = AgentWorkerConfig(command=command, **overrides)
    return CodexAdapter("codex", settings, workspace=tmp_path / "ws")


# --- adapter unit tests: mock CLI success / failure modes ---


def test_claude_adapter_extracts_the_json_result(tmp_path: Path) -> None:
    adapter = claude_adapter(
        tmp_path, script_command(tmp_path, "ok.py", _FAKE_CLAUDE_OK)
    )
    result = asyncio.run(adapter.run("写一个分镜方案"))

    assert result.success
    assert result.agent == "claude"
    assert result.output is not None and "写一个分镜方案" in result.output
    assert result.raw_output is not None and json.loads(result.raw_output)
    assert result.metadata["session_id"] == "sess-1"
    assert result.metadata["exit_code"] == 0
    assert result.metadata["task_id"].startswith("agent_")
    assert result.duration_ms is not None and result.duration_ms >= 0


def test_context_reaches_the_prompt_as_fenced_json(tmp_path: Path) -> None:
    adapter = claude_adapter(
        tmp_path, script_command(tmp_path, "ok.py", _FAKE_CLAUDE_OK)
    )
    result = asyncio.run(
        adapter.run("检查这条流水线", context={"pipeline": "h3", "shots": 3})
    )

    assert result.success
    assert result.output is not None
    assert "检查这条流水线" in result.output
    assert '"pipeline": "h3"' in result.output


def test_claude_adapter_reports_is_error_documents(tmp_path: Path) -> None:
    adapter = claude_adapter(
        tmp_path, script_command(tmp_path, "quota.py", _FAKE_CLAUDE_QUOTA)
    )
    result = asyncio.run(adapter.run("任务"))

    assert not result.success
    assert result.error is not None and "配额用完了" in result.error


def test_claude_adapter_reports_non_zero_exit(tmp_path: Path) -> None:
    adapter = claude_adapter(
        tmp_path, script_command(tmp_path, "crash.py", _FAKE_CRASH)
    )
    result = asyncio.run(adapter.run("任务"))

    assert not result.success
    assert result.error is not None
    assert "退出码 3" in result.error and "boom" in result.error


def test_claude_adapter_keeps_a_non_json_answer(tmp_path: Path) -> None:
    adapter = claude_adapter(
        tmp_path, script_command(tmp_path, "plain.py", _FAKE_PLAIN_TEXT)
    )
    result = asyncio.run(adapter.run("任务"))

    assert result.success
    assert result.output == "plain text answer"
    assert result.metadata["parse"] == "raw"


def test_timeout_kills_the_cli_and_reports_it(tmp_path: Path) -> None:
    adapter = claude_adapter(
        tmp_path, script_command(tmp_path, "sleep.py", _FAKE_SLEEPER)
    )
    result = asyncio.run(adapter.run("任务", timeout=0.5))

    assert not result.success
    assert result.error is not None and "超时" in result.error
    assert result.metadata["timeout"] is True
    assert result.duration_ms is not None and result.duration_ms < 10_000


def test_missing_cli_is_a_result_not_a_crash(tmp_path: Path) -> None:
    adapter = claude_adapter(tmp_path, ["definitely-not-installed-4f3a"])

    result = asyncio.run(adapter.run("任务"))
    assert not result.success
    assert result.error is not None and "不存在" in result.error
    assert not asyncio.run(adapter.health_check())


def test_disabled_adapter_refuses_and_fails_health(tmp_path: Path) -> None:
    adapter = claude_adapter(
        tmp_path,
        script_command(tmp_path, "ok.py", _FAKE_CLAUDE_OK),
        enabled=False,
    )

    result = asyncio.run(adapter.run("任务"))
    assert not result.success
    assert result.error is not None and "停用" in result.error
    assert not asyncio.run(adapter.health_check())


def test_health_check_runs_version(tmp_path: Path) -> None:
    healthy = claude_adapter(
        tmp_path, script_command(tmp_path, "ok.py", _FAKE_CLAUDE_OK)
    )
    broken = claude_adapter(
        tmp_path, script_command(tmp_path, "crash.py", _FAKE_CRASH)
    )

    assert asyncio.run(healthy.health_check())
    # crash 脚本对 --version 返回 0,所以这里造一个真的失败:不存在的命令。
    assert not asyncio.run(
        claude_adapter(tmp_path, ["definitely-not-installed-4f3a"]).health_check()
    )
    assert asyncio.run(broken.health_check())  # --version succeeds by design


def test_codex_adapter_reads_the_last_message_file(tmp_path: Path) -> None:
    adapter = codex_adapter(
        tmp_path, script_command(tmp_path, "codex_ok.py", _FAKE_CODEX_OK)
    )
    result = asyncio.run(adapter.run("审一下这段代码"))

    assert result.success
    assert result.output is not None and result.output.startswith("codex answer:")
    assert "审一下这段代码" in result.output
    assert result.raw_output is not None and "transcript noise" in result.raw_output


def test_codex_adapter_falls_back_to_stdout(tmp_path: Path) -> None:
    adapter = codex_adapter(
        tmp_path, script_command(tmp_path, "codex_nf.py", _FAKE_CODEX_NO_FILE)
    )
    result = asyncio.run(adapter.run("任务"))

    assert result.success
    assert result.output == "stdout answer"
    assert result.metadata["parse"] == "raw"


# --- manager tests: primary success / fallback / all fail ---


class StubAdapter(AgentAdapter):
    def __init__(self, name: str, *, ok: bool, healthy: bool = True) -> None:
        self.name = name
        self._ok = ok
        self._healthy = healthy
        self.calls: list[str] = []

    async def run(
        self,
        task: str,
        context: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> AgentResult:
        self.calls.append(task)
        if self._ok:
            return AgentResult(
                success=True, agent=self.name, output=f"{self.name}:{task}"
            )
        return AgentResult(success=False, agent=self.name, error="模拟失败")

    async def health_check(self) -> bool:
        return self._healthy


def test_manager_runs_the_named_agent() -> None:
    manager = AgentManager()
    manager.register("claude", StubAdapter("claude", ok=True))

    result = asyncio.run(manager.run(agent="claude", task="hello"))
    assert result.success and result.output == "claude:hello"


def test_manager_rejects_unknown_agent_names() -> None:
    manager = AgentManager()
    with pytest.raises(UnknownAgent):
        asyncio.run(manager.run(agent="gemini", task="hello"))


def test_fallback_moves_to_the_next_agent() -> None:
    manager = AgentManager()
    manager.register("claude", StubAdapter("claude", ok=False))
    manager.register("codex", StubAdapter("codex", ok=True))

    result = asyncio.run(
        manager.run_with_fallback("task", agents=["claude", "codex"])
    )

    assert result.success and result.agent == "codex"
    attempts = result.metadata["attempts"]
    assert [attempt["agent"] for attempt in attempts] == ["claude", "codex"]
    assert attempts[0]["success"] is False and attempts[1]["success"] is True


def test_fallback_uses_configured_defaults_and_skips_unregistered() -> None:
    manager = AgentManager(
        defaults=AgentDefaults(primary="claude", fallback=["grok", "codex"])
    )
    manager.register("claude", StubAdapter("claude", ok=False))
    manager.register("codex", StubAdapter("codex", ok=True))

    result = asyncio.run(manager.run_with_fallback("task"))

    assert result.success and result.agent == "codex"
    attempts = result.metadata["attempts"]
    assert attempts[1] == {"agent": "grok", "error": "未注册,跳过"}


def test_fallback_returns_the_last_failure_when_all_fail() -> None:
    manager = AgentManager()
    manager.register("claude", StubAdapter("claude", ok=False))
    manager.register("codex", StubAdapter("codex", ok=False))

    result = asyncio.run(
        manager.run_with_fallback("task", agents=["claude", "codex"])
    )

    assert not result.success and result.agent == "codex"
    assert len(result.metadata["attempts"]) == 2


def test_health_check_maps_every_registered_agent() -> None:
    manager = AgentManager()
    manager.register("claude", StubAdapter("claude", ok=True, healthy=True))
    manager.register("codex", StubAdapter("codex", ok=True, healthy=False))

    assert asyncio.run(manager.health_check()) == {
        "claude": True,
        "codex": False,
    }


def test_review_returns_every_answer() -> None:
    manager = AgentManager()
    manager.register("claude", StubAdapter("claude", ok=True))
    manager.register("codex", StubAdapter("codex", ok=True))

    results = asyncio.run(manager.review("task", agents=["claude", "codex"]))

    assert set(results) == {"claude", "codex"}
    assert results["claude"].output == "claude:task"
    assert results["codex"].output == "codex:task"


# --- configuration and wiring ---


def test_build_manager_registers_configured_agents(tmp_path: Path) -> None:
    config = make_config(tmp_path).model_copy(
        update={
            "agents": {
                "claude": AgentWorkerConfig(command=["claude"]),
                "codex": AgentWorkerConfig(enabled=False),
            }
        }
    )
    manager = build_manager(config)

    assert manager.registered_agents() == ["claude", "codex"]
    assert manager.available_agents() == ["claude"]


def test_build_manager_rejects_unknown_agent_ids(tmp_path: Path) -> None:
    config = make_config(tmp_path).model_copy(
        update={
            "agents": {
                "claude": AgentWorkerConfig(),
                "hal9000": AgentWorkerConfig(),
            }
        }
    )
    with pytest.raises(AgentConfigError, match="hal9000"):
        build_manager(config)


def test_config_requires_a_configured_primary(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="primary"):
        make_config(tmp_path).model_copy(deep=True).model_validate(
            {
                **make_config(tmp_path).model_dump(mode="json"),
                "agents": {"codex": {"enabled": True}},
            }
        )


def test_agents_endpoint_lists_workers_and_health(tmp_path: Path) -> None:
    config = make_config(tmp_path).model_copy(
        update={
            "agents": {
                "claude": AgentWorkerConfig(
                    command=script_command(tmp_path, "ok.py", _FAKE_CLAUDE_OK)
                ),
                "codex": AgentWorkerConfig(enabled=False),
            }
        }
    )
    with TestClient(create_app(config, renderer=FakeRenderer())) as http:
        response = http.get("/v1/agents")

    assert response.status_code == 200
    workers = {entry["agent"]: entry for entry in response.json()}
    assert workers["claude"]["available"] is True
    assert workers["claude"]["primary"] is True
    assert workers["codex"]["enabled"] is False
    assert workers["codex"]["available"] is False


# --- chat seam: a workspace turn can go to an external worker ---


def test_chat_turn_can_route_to_an_external_agent(tmp_path: Path) -> None:
    config = make_config(tmp_path).model_copy(
        update={
            "agents": {
                "claude": AgentWorkerConfig(
                    command=script_command(tmp_path, "ok.py", _FAKE_CLAUDE_OK)
                )
            }
        }
    )
    brain = ScriptedChat([])  # the local brain must stay untouched
    with TestClient(
        create_app(config, renderer=FakeRenderer(), chat_client=brain)
    ) as http:
        chat_id = http.post("/v1/chats", json={}).json()["chat_id"]
        record = http.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "给我一个 60 秒的分镜方案", "agent": "claude"},
        ).json()

    assert [message["role"] for message in record["messages"]] == [
        "user",
        "assistant",
    ]
    assistant = record["messages"][-1]
    assert assistant["agent"] == "claude"
    # The fake echoes its prompt: the user turn, the tool catalog and the
    # calling protocol all made it into what the CLI was asked.
    assert "给我一个 60 秒的分镜方案" in assistant["content"]
    assert "generate_video" in assistant["content"]
    assert "工具调用协议" in assistant["content"]
    assert brain.requests == []


_FAKE_CLAUDE_TOOL_DRIVER = """
    import json, sys
    if "--version" in sys.argv[1:]:
        print("fake 1.0"); sys.exit(0)
    prompt = sys.stdin.read()
    if "工具 generate_image 返回" in prompt:
        inner = "图片任务已排好,job id 见上。"
    else:
        call = {
            "tool": "generate_image",
            "arguments": {"capability": "flux-t2i", "prompt": "深蓝海报"},
        }
        inner = "```json\\n" + json.dumps(call, ensure_ascii=False) + "\\n```"
    print(json.dumps({"type": "result", "is_error": False, "result": inner}))
"""


def test_external_agent_drives_the_tool_loop(tmp_path: Path) -> None:
    """The cloud worker picks a tool over the fenced-JSON protocol, this
    process executes it locally, and the worker answers off the result."""
    # test_agent's config carries the flux-t2i capability the tool call needs.
    config = make_agent_config(tmp_path).model_copy(
        update={
            "agents": {
                "claude": AgentWorkerConfig(
                    command=script_command(
                        tmp_path, "driver.py", _FAKE_CLAUDE_TOOL_DRIVER
                    )
                )
            }
        }
    )
    with TestClient(
        create_app(config, renderer=FakeRenderer(), job_runner=JobFakeRunner())
    ) as http:
        chat_id = http.post("/v1/chats", json={}).json()["chat_id"]
        record = http.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "画一张深蓝海报", "agent": "claude"},
        ).json()
        jobs = http.get("/v1/jobs").json()

    assert [message["role"] for message in record["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    call_message = record["messages"][1]
    assert call_message["agent"] == "claude"
    assert call_message["tool_calls"][0]["function"]["name"] == "generate_image"
    assert record["messages"][2]["tool_name"] == "generate_image"
    assert "已排好" in record["messages"][-1]["content"]
    # The tool ran for real: one flux-t2i job sits in the local queue.
    assert [job["capability"] for job in jobs] == ["flux-t2i"]


_FAKE_CODEX_BRAIN = """
    import json, sys
    args = sys.argv[1:]
    if "--version" in args:
        print("fake-codex 1.0.0"); sys.exit(0)
    prompt = sys.stdin.read()
    if "工具 list_capabilities 返回" in prompt:
        answer = "本机支持的模式都列出来了。"
    else:
        answer = json.dumps({"tool": "list_capabilities", "arguments": {}})
    path = args[args.index("--output-last-message") + 1]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(answer)
    print("transcript noise")
"""


def test_codex_brain_may_answer_with_bare_json(tmp_path: Path) -> None:
    """No fence around the JSON — some models skip it; the parser must still
    read the whole reply as one tool call."""
    config = make_config(tmp_path).model_copy(
        update={
            "agents": {
                "codex": AgentWorkerConfig(
                    command=script_command(tmp_path, "cbrain.py", _FAKE_CODEX_BRAIN)
                )
            },
            "agent_defaults": AgentDefaults(primary="codex", fallback=[]),
        }
    )
    with TestClient(create_app(config, renderer=FakeRenderer())) as http:
        chat_id = http.post("/v1/chats", json={}).json()["chat_id"]
        record = http.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "这台机器能生成什么?", "agent": "codex"},
        ).json()

    roles = [message["role"] for message in record["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    first = record["messages"][1]
    assert first["tool_calls"][0]["function"]["name"] == "list_capabilities"
    assert first["content"] == ""
    assert record["messages"][3]["agent"] == "codex"


_FAKE_CLAUDE_DIES_AFTER_TOOL = """
    import json, sys
    if "--version" in sys.argv[1:]:
        sys.exit(0)
    prompt = sys.stdin.read()
    if "工具 list_capabilities 返回" in prompt:
        print("quota burned", file=sys.stderr)
        sys.exit(3)
    call = json.dumps({"tool": "list_capabilities", "arguments": {}})
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": "```json\\n" + call + "\\n```",
    }))
"""


def test_brain_death_after_tools_keeps_the_transcript(tmp_path: Path) -> None:
    """Dying before any tool ran persists nothing (see the crash test below);
    dying after a tool already queued work must keep the transcript — queued
    task ids must not vanish with the turn."""
    config = make_config(tmp_path).model_copy(
        update={
            "agents": {
                "claude": AgentWorkerConfig(
                    command=script_command(
                        tmp_path, "dies.py", _FAKE_CLAUDE_DIES_AFTER_TOOL
                    )
                )
            }
        }
    )
    with TestClient(create_app(config, renderer=FakeRenderer())) as http:
        chat_id = http.post("/v1/chats", json={}).json()["chat_id"]
        response = http.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "看看能力", "agent": "claude"},
        )
        assert response.status_code == 200
        fetched = http.get(f"/v1/chats/{chat_id}").json()

    roles = [message["role"] for message in fetched["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]
    assert "失联" in fetched["messages"][-1]["content"]


def test_chat_turn_with_unknown_agent_is_a_400(tmp_path: Path) -> None:
    with TestClient(
        create_app(make_config(tmp_path), renderer=FakeRenderer())
    ) as http:
        chat_id = http.post("/v1/chats", json={}).json()["chat_id"]
        response = http.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "hi", "agent": "gemini"}
        )

    assert response.status_code == 400
    assert "gemini" in response.json()["detail"]


def test_failed_external_turn_persists_nothing(tmp_path: Path) -> None:
    config = make_config(tmp_path).model_copy(
        update={
            "agents": {
                "claude": AgentWorkerConfig(
                    command=script_command(tmp_path, "crash.py", _FAKE_CRASH)
                )
            }
        }
    )
    with TestClient(create_app(config, renderer=FakeRenderer())) as http:
        chat_id = http.post("/v1/chats", json={}).json()["chat_id"]
        response = http.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "hi", "agent": "claude"},
        )
        record = http.get(f"/v1/chats/{chat_id}").json()

    assert response.status_code == 503
    assert record["messages"] == []


# --- real CLI integration: health checks cost nothing and run whenever the
# CLI is installed; actual tasks spend quota and need AGENT_CLI_TESTS=1. ---

_REAL_TASKS = pytest.mark.skipif(
    os.environ.get("AGENT_CLI_TESTS") != "1",
    reason="真实任务会消耗订阅额度;AGENT_CLI_TESTS=1 才跑",
)


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI 未安装")
def test_real_claude_cli_health(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(
        "claude", AgentWorkerConfig(), workspace=tmp_path / "ws"
    )
    assert asyncio.run(adapter.health_check())


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI 未安装")
def test_real_codex_cli_health(tmp_path: Path) -> None:
    adapter = CodexAdapter(
        "codex", AgentWorkerConfig(), workspace=tmp_path / "ws"
    )
    assert asyncio.run(adapter.health_check())


@_REAL_TASKS
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI 未安装")
def test_real_claude_tiny_task(tmp_path: Path) -> None:
    adapter = ClaudeCodeAdapter(
        "claude", AgentWorkerConfig(timeout_seconds=180), workspace=tmp_path / "ws"
    )
    result = asyncio.run(adapter.run("Reply with exactly the word OK and nothing else."))
    assert result.success, result.error
    assert result.output is not None and "OK" in result.output


@_REAL_TASKS
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI 未安装")
def test_real_codex_tiny_task(tmp_path: Path) -> None:
    adapter = CodexAdapter(
        "codex", AgentWorkerConfig(timeout_seconds=180), workspace=tmp_path / "ws"
    )
    result = asyncio.run(adapter.run("Reply with exactly the word OK and nothing else."))
    assert result.success, result.error
    assert result.output is not None and "OK" in result.output
