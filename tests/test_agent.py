import json
from pathlib import Path
import time
from typing import Any, Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.artifacts import write_bytes_atomic
from videogen_service.config import Capability, ServiceConfig
from videogen_service.jobs import JobSpec
from videogen_service.memory import MemoryStore
from videogen_service.models import RenderProgress, RenderRequest, ScriptRequest, ScriptResult, ScriptShot, ScriptSource


def make_config(tmp_path: Path) -> ServiceConfig:
    return ServiceConfig.model_validate(
        {
            "host": "127.0.0.1",
            "port": 8020,
            "work_dir": str(tmp_path / "work"),
            "skills_dir": str(tmp_path / "skills"),
            "comfyui": {
                "base_url": "http://127.0.0.1:8188",
                "timeout_seconds": 1800,
                "poll_interval_seconds": 0.01,
            },
            "renderer": {
                "fps": 24,
                "min_seconds": 5,
                "max_seconds": 15,
                "max_total_seconds": 120,
                "max_shots": 12,
            },
            "ollama": {
                "unload_before_render": False,
                "base_url": "http://127.0.0.1:11434",
                "model": "qwen3.6:27b",
            },
            "modes": {
                "t2v": {
                    "workflow_file": str(tmp_path / "t2v.json"),
                    "inputs": {"prompt": ["1.text"]},
                },
                "story": {
                    "workflow_file": str(tmp_path / "story.json"),
                    "inputs": {"prompt": ["1.text"], "timeline": ["1.timeline"]},
                },
            },
            "capabilities": {
                "flux-t2i": {
                    "kind": "t2i",
                    "workflow_file": str(tmp_path / "flux.json"),
                    "inputs": {"prompt": ["6.text"]},
                },
            },
        }
    )


class FakeRenderer:
    def __init__(self) -> None:
        self.requests: list[RenderRequest] = []

    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
    ) -> bytes:
        self.requests.append(request)
        return b"FAKE-MP4"


class FakeRunner:
    def run(self, spec: JobSpec, capability: Capability, *, directory: Path) -> str:
        write_bytes_atomic(directory / "output.png", b"PNG")
        return "output.png"


class StubStudio:
    def create(self, request: ScriptRequest) -> ScriptResult:
        raise AssertionError("agent tests never fetch captions")

    def create_from_document(self, options: Any, *, filename: str, text: str) -> ScriptResult:
        return ScriptResult(
            source={"kind": "document", "filename": filename, "chars": len(text), "truncated": False},
            title="想法成片",
            summary="从想法生成的脚本。",
            mode="story",
            prompt="科普短片。\n\n[0s-6s] 想法画面",
            seconds=6.0,
            shots=[ScriptShot(seconds=6, prompt="想法画面", narration="解说")],
        )


class ScriptedChat:
    """Plays back a fixed sequence of assistant messages, recording every
    request the agent sends to the model."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.requests.append(messages)
        self.tools_seen.append(tools)
        if not self.replies:
            return {"content": "(没有更多台词了)"}
        return self.replies.pop(0)


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": arguments}}


def make_client(tmp_path: Path, chat: ScriptedChat) -> TestClient:
    return TestClient(
        create_app(
            make_config(tmp_path),
            renderer=FakeRenderer(),
            studio=StubStudio(),
            job_runner=FakeRunner(),
            chat_client=chat,
        )
    )


def test_a_chat_is_created_listed_and_deleted(tmp_path: Path) -> None:
    with make_client(tmp_path, ScriptedChat([])) as client:
        created = client.post("/v1/chats", json={"title": "宠物短片"})
        assert created.status_code == 201
        chat_id = created.json()["chat_id"]

        listed = client.get("/v1/chats").json()
        assert [chat["chat_id"] for chat in listed] == [chat_id]
        assert listed[0]["title"] == "宠物短片"

        assert client.delete(f"/v1/chats/{chat_id}").status_code == 204
        assert client.get(f"/v1/chats/{chat_id}").status_code == 404


def test_a_plain_answer_needs_no_tools(tmp_path: Path) -> None:
    chat = ScriptedChat([{"content": "你好,想做什么样的视频?"}])
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "你好"}
        ).json()

    roles = [message["role"] for message in record["messages"]]
    assert roles == ["user", "assistant"]
    assert record["messages"][-1]["content"] == "你好,想做什么样的视频?"
    # The system prompt rides in the request but is not stored in the record.
    assert chat.requests[0][0]["role"] == "system"
    assert "本地" in chat.requests[0][0]["content"]
    assert record["title"] == "你好"


def test_a_tool_call_runs_and_its_result_feeds_back(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_image",
                        {"capability": "flux-t2i", "prompt": "深蓝网络"},
                    )
                ],
            },
            {"content": "图片任务已排队,可以随时问我进度。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "画一张深蓝网络"}
        ).json()

        roles = [message["role"] for message in record["messages"]]
        assert roles == ["user", "assistant", "tool", "assistant"]
        result = json.loads(record["messages"][2]["content"])
        assert result["status"] in {"QUEUED", "RUNNING", "DONE"}
        job_id = result["job_id"]

        # The submitted job is real and finishes on the job queue.
        deadline = time.monotonic() + 3
        state = client.get(f"/v1/jobs/{job_id}").json()
        while state["status"] != "DONE" and time.monotonic() < deadline:
            time.sleep(0.01)
            state = client.get(f"/v1/jobs/{job_id}").json()
        assert state["status"] == "DONE"

    # The second model round saw the tool result in its context.
    tool_rounds = chat.requests[1]
    assert tool_rounds[-1]["role"] == "tool"
    assert job_id in tool_rounds[-1]["content"]


def test_storyboard_and_video_tools_compose(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [tool_call("write_storyboard", {"idea": "宠物屋测评"})],
            },
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {"mode": "story", "prompt": "科普短片。\n\n[0s-6s] 想法画面"},
                    )
                ],
            },
            {"content": "分镜写好了,渲染也排上了。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "帮我做一条宠物屋测评短片"},
        ).json()

        board = json.loads(record["messages"][2]["content"])
        assert board["mode"] == "story"
        assert "[0s-6s]" in board["prompt"]

        video = json.loads(record["messages"][4]["content"])
        render = client.get(f"/v1/renders/{video['render_id']}")
        assert render.status_code == 200


def test_the_render_envelope_guard_blocks_oom_specs(tmp_path: Path) -> None:
    # 2026-08-09 的两种真实死法:1080p 在模型加载阶段 OOM,30 秒双段 story
    # 渲染中途 OOM。守卫要在排队之前把它们拦下来,并让模型读得懂原因。
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {
                            "mode": "t2v",
                            "prompt": "夜景",
                            "width": 1920,
                            "height": 1080,
                        },
                    )
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {"mode": "story", "prompt": "[0s-15s] 开场\n[15s-30s] 结尾"},
                    )
                ],
            },
            {"content": "两条都超本机红线,没有排渲染。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "来一条 1080p 的 30 秒大片"},
        ).json()
        resolution = json.loads(record["messages"][2]["content"])
        duration = json.loads(record["messages"][4]["content"])
        assert "864x480" in resolution["error"]
        assert "15" in duration["error"]
        # Nothing slipped past the guard into the queue.
        assert client.get("/v1/renders").json() == []


def test_a_broken_tool_reports_instead_of_crashing(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call("generate_image", {"capability": "nope", "prompt": "x"})
                ],
            },
            {"content": "这个能力不存在,我换个方式。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "画一张图"}
        ).json()
    result = json.loads(record["messages"][2]["content"])
    assert "未配置的能力" in result["error"]


def test_a_looping_model_is_cut_off(tmp_path: Path) -> None:
    reply = {
        "content": "",
        "tool_calls": [tool_call("list_capabilities", {})],
    }
    chat = ScriptedChat([dict(reply) for _ in range(20)])
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "随便"}
        ).json()
    assert "轮数超限" in record["messages"][-1]["content"]


def test_memory_preferences_ride_in_the_system_prompt(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    MemoryStore(config.work_dir).add("旁白都用口语")
    chat = ScriptedChat([{"content": "好的。"}])
    app = create_app(
        config,
        renderer=FakeRenderer(),
        studio=StubStudio(),
        job_runner=FakeRunner(),
        chat_client=chat,
    )
    with TestClient(app) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        client.post(f"/v1/chats/{chat_id}/messages", json={"text": "在吗"})
    assert "旁白都用口语" in chat.requests[0][0]["content"]
