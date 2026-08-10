# 渲染取消与 BrainGate:排队中取消、运行中取消、端点语义、agent 工具,
# 以及"渲染启动的卸载等大脑说完话"的门闩语义。
import threading
import time
import json
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.braingate import BrainGate
from videogen_service.config import ServiceConfig
from videogen_service.models import RenderProgress, RenderRequest
from videogen_service.renderer import RenderCancelled


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
            },
        }
    )


class BlockingRenderer:
    """卡在渲染里,直到被取消或被放行——用来钉住 RUNNING 状态。"""

    def __init__(self) -> None:
        self.requests: list[RenderRequest] = []
        self.started = threading.Event()
        self.release = threading.Event()

    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        self.requests.append(request)
        self.started.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                raise RenderCancelled("渲染已取消")
            if self.release.is_set():
                return b"FAKE-MP4"
            time.sleep(0.01)
        raise AssertionError("渲染既没被取消也没被放行")


def submit(client: TestClient, render_id: str) -> None:
    response = client.post(
        "/v1/renders",
        data={
            "render_id": render_id,
            "mode": "t2v",
            "prompt": "日出雪山航拍",
            "width": 864,
            "height": 480,
            "seconds": 6,
        },
    )
    assert response.status_code == 202, response.text


def wait_status(client: TestClient, render_id: str, wanted: set[str]) -> dict:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        view = client.get(f"/v1/renders/{render_id}").json()
        if view["status"] in wanted:
            return view
        time.sleep(0.01)
    raise AssertionError(f"{render_id} 没到 {wanted}: {view}")


def test_cancel_a_running_render(tmp_path: Path) -> None:
    renderer = BlockingRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        submit(client, "r-run")
        assert renderer.started.wait(3)
        cancelled = client.post("/v1/renders/r-run/cancel")
        assert cancelled.status_code == 200
        view = wait_status(client, "r-run", {"FAILED"})
        assert "已取消" in view["error"]


def test_cancel_a_queued_render_before_it_starts(tmp_path: Path) -> None:
    renderer = BlockingRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        submit(client, "r-front")
        assert renderer.started.wait(3)
        submit(client, "r-waiting")
        cancelled = client.post("/v1/renders/r-waiting/cancel").json()
        assert cancelled["status"] == "FAILED"
        assert "未开始渲染" in cancelled["error"]
        renderer.release.set()
        wait_status(client, "r-front", {"DONE"})
    # 排队即被取消的那条从未进过渲染器。
    assert [request.render_id for request in renderer.requests] == ["r-front"]


def test_cancel_refuses_finished_and_unknown(tmp_path: Path) -> None:
    renderer = BlockingRenderer()
    renderer.release.set()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        submit(client, "r-done")
        wait_status(client, "r-done", {"DONE"})
        assert client.post("/v1/renders/r-done/cancel").status_code == 409
        assert client.post("/v1/renders/nope/cancel").status_code == 404


def test_agent_cancel_render_tool(tmp_path: Path) -> None:
    class ScriptedChat:
        def __init__(self, replies: list[dict[str, Any]]) -> None:
            self.replies = list(replies)

        def chat(
            self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]]
        ) -> dict[str, Any]:
            if not self.replies:
                return {"content": "(完)"}
            return self.replies.pop(0)

    renderer = BlockingRenderer()
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "cancel_render",
                            "arguments": {"render_id": "r-tool"},
                        }
                    }
                ],
            },
            {"content": "已取消。"},
        ]
    )
    with TestClient(
        create_app(make_config(tmp_path), renderer=renderer, chat_client=chat)
    ) as client:
        submit(client, "r-tool")
        assert renderer.started.wait(3)
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "取消刚才那条渲染"}
        ).json()
        result = json.loads(record["messages"][2]["content"])
        assert result["cancelled"] is True
        wait_status(client, "r-tool", {"FAILED"})


def test_brain_gate_waits_for_active_generation() -> None:
    gate = BrainGate()
    assert gate.wait_idle(0.01) is True  # 空闲时立即放行

    started = threading.Event()

    def talk() -> None:
        with gate.active():
            started.set()
            time.sleep(0.2)

    thread = threading.Thread(target=talk)
    thread.start()
    assert started.wait(1)
    begin = time.monotonic()
    assert gate.wait_idle(2.0) is True  # 等到生成结束才放行
    assert time.monotonic() - begin >= 0.1
    thread.join()

    def long_talk() -> None:
        with gate.active():
            started.set()
            time.sleep(0.5)

    started.clear()
    thread = threading.Thread(target=long_talk)
    thread.start()
    assert started.wait(1)
    assert gate.wait_idle(0.05) is False  # 超时仍忙,交调用方决断
    thread.join()
