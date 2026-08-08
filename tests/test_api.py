from pathlib import Path
import threading
import time
from typing import Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.config import DirectorConfig, RendererConfig, RenderMode, ServiceConfig
from videogen_service.director import PromptDirector
from videogen_service.models import H3Prompt, RenderProgress, RenderRequest


def make_config(tmp_path: Path) -> ServiceConfig:
    return ServiceConfig.model_validate(
        {
            "host": "127.0.0.1",
            "port": 8020,
            "work_dir": str(tmp_path / "work"),
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
                "i2v": {
                    "workflow_file": str(tmp_path / "i2v.json"),
                    "inputs": {"prompt": ["1.text"], "first_frame": ["2.image"]},
                },
                "story": {
                    "workflow_file": str(tmp_path / "story.json"),
                    "inputs": {"prompt": ["1.text"], "timeline": ["1.timeline"]},
                },
            },
        }
    )


def make_director(tmp_path: Path, written: H3Prompt) -> PromptDirector:
    class Provider:
        def write(
            self,
            *,
            idea: str,
            seconds: float,
            mode: RenderMode,
            feedback: "list[str] | tuple[str, ...]",
        ) -> H3Prompt:
            return written

    return PromptDirector(
        {"claude": Provider(), "chatgpt": Provider()},
        settings=DirectorConfig.model_validate(
            {
                "guide_file": tmp_path / "guide.md",
                "default": "claude",
                "providers": {
                    "claude": {"provider": "anthropic", "model": "claude-opus-5"},
                    "chatgpt": {"provider": "openai", "model": "gpt-test"},
                },
            }
        ),
        renderer=RendererConfig(
            fps=24, min_seconds=5, max_seconds=15, max_total_seconds=120, max_shots=12
        ),
        cache_dir=tmp_path / "prompts",
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
        if on_progress is not None:
            on_progress(
                RenderProgress(
                    percent=0.5,
                    step=10,
                    total_steps=20,
                    pass_index=1,
                    pass_total=1,
                )
            )
        return b"FAKE-MP4"


class BlockingRenderer(FakeRenderer):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
    ) -> bytes:
        self.requests.append(request)
        self.started.set()
        assert self.release.wait(3)
        return b"FAKE-MP4"


def wait_for_done(client: TestClient, render_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    state = client.get(f"/v1/renders/{render_id}").json()
    while state["status"] not in {"DONE", "FAILED"} and time.monotonic() < deadline:
        time.sleep(0.01)
        state = client.get(f"/v1/renders/{render_id}").json()
    return state


def test_health_describes_the_renderer_contract(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        health = client.get("/health")

    assert health.status_code == 200
    assert health.json() == {
        "status": "ok",
        "modes": ["i2v", "story", "t2v"],
        "timeline_modes": ["story"],
        "max_total_seconds": 120.0,
        "max_shots": 12,
        "fps": 24,
        "min_seconds": 5.0,
        "max_seconds": 15.0,
        "length_step": 17,
        "length_offset": 5,
        "shot_header_pattern": (
            r"^\[\s*(\d+(?:\.\d+)?)\s*s?\s*(?:[-–—~～]\s*"
            r"(\d+(?:\.\d+)?)\s*s?\s*)?\]\s*(.*)$"
        ),
        "director": None,
    }


def test_a_render_is_submitted_observed_and_downloaded(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_abc123",
                "mode": "t2v",
                "prompt": "晨雾中的山谷",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        state = wait_for_done(client, "vg_abc123")
        media = client.get("/v1/renders/vg_abc123/media")

    assert submitted.status_code == 202
    assert state["status"] == "DONE"
    assert state["media_url"] == "/v1/renders/vg_abc123/media"
    assert media.content == b"FAKE-MP4"
    assert renderer.requests[0].prompt == "晨雾中的山谷"


def test_reference_frames_are_owned_by_the_render_service(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_image",
                "mode": "i2v",
                "prompt": "让湖面泛起涟漪",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
            files={"first_frame": ("first.png", b"PNG", "image/png")},
        )
        state = wait_for_done(client, "vg_image")

    assert submitted.status_code == 202
    assert state["status"] == "DONE"
    frame = renderer.requests[0].first_frame
    assert frame is not None
    assert frame.read_bytes() == b"PNG"
    assert tmp_path / "work" in frame.parents


def test_validation_returns_the_aligned_storyboard_duration(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        response = client.post(
            "/v1/validate",
            json={
                "mode": "story",
                "prompt": "[0s-5s] 山谷\n[5s-10s] 树冠",
                "width": 864,
                "height": 480,
                "seconds": 5,
                "has_first_frame": False,
                "has_last_frame": False,
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "mode": "story",
        "timeline_mode": True,
        "seconds": 248 / 24,
    }


def test_validation_reports_the_snapped_duration_not_the_requested_one(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        response = client.post(
            "/v1/validate",
            json={
                "mode": "t2v",
                "prompt": "晨雾里的山谷",
                "width": 864,
                "height": 480,
                "seconds": 6,
                "has_first_frame": False,
                "has_last_frame": False,
            },
        )

    assert response.status_code == 200
    # 6s asks for 144 frames, the grid delivers 158 — a 0.58s gap the control
    # plane must see, since it is the one cutting and captioning the result.
    assert response.json() == {
        "mode": "t2v",
        "timeline_mode": False,
        "seconds": 158 / 24,
    }


def test_enhance_is_unavailable_when_no_director_is_configured(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        response = client.post(
            "/v1/enhance", json={"mode": "t2v", "prompt": "晨雾里的山谷", "seconds": 6}
        )

    assert response.status_code == 503
    assert "没有配置提示词导演" in response.json()["detail"]


def test_enhance_returns_the_rewritten_prompt_and_its_warnings(tmp_path: Path) -> None:
    written = H3Prompt(
        integrated_multimodal_description="[Shot 1] Cinematic, fog drifts through a valley.",
        overall_soundscape="Wind moves through wet grass.",
        # A mood word the checker must catch and report rather than swallow.
        non_diegetic_music="A nostalgic piano line at a slow tempo.",
    )
    app = create_app(
        make_config(tmp_path),
        renderer=FakeRenderer(),
        director=make_director(tmp_path, written),
    )
    with TestClient(app) as client:
        # Everything the control plane needs to draw the picker.
        assert client.get("/health").json()["director"] == {
            "default": "claude",
            "providers": [
                {"id": "chatgpt", "provider": "openai", "model": "gpt-test"},
                {"id": "claude", "provider": "anthropic", "model": "claude-opus-5"},
            ],
        }
        response = client.post(
            "/v1/enhance", json={"mode": "t2v", "prompt": "晨雾里的山谷", "seconds": 6}
        )
        chosen = client.post(
            "/v1/enhance",
            json={
                "mode": "t2v",
                "prompt": "晨雾里的山谷",
                "seconds": 6,
                "provider": "chatgpt",
            },
        )
        unknown = client.post(
            "/v1/enhance",
            json={
                "mode": "t2v",
                "prompt": "晨雾里的山谷",
                "seconds": 6,
                "provider": "gemini",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["enhanced"] is True
    assert body["provider"] == "claude"
    assert body["prompt"].startswith("integrated_multimodal_description: [Shot 1]")
    assert body["seconds"] == 158 / 24
    assert "nostalgic" in body["warnings"][0]
    assert chosen.json()["provider"] == "chatgpt"
    # Naming a writer that does not exist is the caller's mistake, not a
    # degrade — it must not silently come back as the default's answer.
    assert unknown.status_code == 400


def test_image_mode_validation_requires_a_first_frame(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        response = client.post(
            "/v1/validate",
            json={
                "mode": "i2v",
                "prompt": "湖面",
                "width": 864,
                "height": 480,
                "seconds": 5,
                "has_first_frame": False,
                "has_last_frame": False,
            },
        )

    assert response.status_code == 400
    assert "首帧" in response.json()["detail"]


def test_unknown_mode_is_a_client_error_not_a_worker_crash(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        response = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_unknown",
                "mode": "not-a-mode",
                "prompt": "湖面",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )

    assert response.status_code == 422


def test_foreign_web_pages_cannot_spend_the_local_gpu(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        response = client.post(
            "/v1/renders",
            headers={"Origin": "https://attacker.example"},
            data={
                "render_id": "vg_cross_site",
                "mode": "t2v",
                "prompt": "湖面",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )

    assert response.status_code == 403


def test_resubmitting_a_finished_render_is_idempotent(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    payload = {
        "render_id": "vg_same",
        "mode": "t2v",
        "prompt": "湖面",
        "width": "864",
        "height": "480",
        "seconds": "5",
    }
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        assert client.post("/v1/renders", data=payload).status_code == 202
        assert wait_for_done(client, "vg_same")["status"] == "DONE"
        repeated = client.post("/v1/renders", data=payload)

    assert repeated.status_code == 202
    assert repeated.json()["status"] == "DONE"
    assert len(renderer.requests) == 1


def test_one_worker_keeps_later_gpu_jobs_queued(tmp_path: Path) -> None:
    renderer = BlockingRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        first = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_first",
                "mode": "t2v",
                "prompt": "第一条",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        assert first.status_code == 202
        assert renderer.started.wait(1)
        second = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_second",
                "mode": "t2v",
                "prompt": "第二条",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        assert second.json()["status"] == "QUEUED"
        assert [request.render_id for request in renderer.requests] == ["vg_first"]
        renderer.release.set()
        assert wait_for_done(client, "vg_first")["status"] == "DONE"
        assert wait_for_done(client, "vg_second")["status"] == "DONE"

    assert [request.render_id for request in renderer.requests] == [
        "vg_first",
        "vg_second",
    ]


def test_service_shutdown_waits_for_active_work_and_fails_queued_work(
    tmp_path: Path,
) -> None:
    renderer = BlockingRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        response = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_shutdown",
                "mode": "t2v",
                "prompt": "正在生成",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        assert response.status_code == 202
        assert renderer.started.wait(1)
        queued = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_shutdown_queued",
                "mode": "t2v",
                "prompt": "还在排队",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        assert queued.json()["status"] == "QUEUED"
        threading.Timer(0.05, renderer.release.set).start()

    assert [request.render_id for request in renderer.requests] == ["vg_shutdown"]
    active_state = (tmp_path / "work" / "vg_shutdown" / "state.json").read_text(
        encoding="utf-8"
    )
    assert '"status": "DONE"' in active_state
    queued_state = (
        tmp_path / "work" / "vg_shutdown_queued" / "state.json"
    ).read_text(encoding="utf-8")
    assert '"status": "FAILED"' in queued_state


def test_terminal_render_can_be_deleted(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        client.post(
            "/v1/renders",
            data={
                "render_id": "vg_delete",
                "mode": "t2v",
                "prompt": "湖面",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        assert wait_for_done(client, "vg_delete")["status"] == "DONE"
        deleted = client.delete("/v1/renders/vg_delete")
        missing = client.get("/v1/renders/vg_delete")

    assert deleted.status_code == 204
    assert missing.status_code == 404
