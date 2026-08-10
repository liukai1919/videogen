import json
from pathlib import Path
import threading
import time
from typing import Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.config import ServiceConfig
from videogen_service.models import RenderProgress, RenderRequest
from videogen_service.scripting import ScriptStudio, ScriptUnavailable, Summarizer
from videogen_service.transcript import Transcript, canonical_youtube_url


def make_config(tmp_path: Path) -> ServiceConfig:
    return ServiceConfig.model_validate(
        {
            "host": "127.0.0.1",
            "port": 8020,
            "work_dir": str(tmp_path / "work"),
            # Pinned into the sandbox so the repo's real skills/ never leaks in.
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


class FakeFetcher:
    def fetch(self, url: str) -> Transcript:
        video_id, watch_url = canonical_youtube_url(url)
        return Transcript(
            video_id=video_id,
            title="Why the sky is blue",
            url=watch_url,
            language="en",
            automatic=True,
            duration_seconds=640.0,
            text="Sunlight scatters off air molecules.",
        )


class FakeSummarizer:
    def summarize(self, prompt: str) -> str:
        return json.dumps(
            {
                "title": "蓝天为什么是蓝的",
                "summary": "短波长的蓝光被散射得最多。",
                "shots": [
                    {"seconds": 6, "prompt": "航拍晴朗天空", "narration": "抬头就是蓝色"},
                    {"seconds": 6, "prompt": "阳光散射示意", "narration": "蓝光散得最开"},
                ],
            },
            ensure_ascii=False,
        )


class FailingSummarizer:
    def summarize(self, prompt: str) -> str:
        raise ScriptUnavailable("连不上 Ollama(http://127.0.0.1:11434)")


def fake_studio(tmp_path: Path, *, summarizer: Summarizer | None = None) -> ScriptStudio:
    return ScriptStudio(
        make_config(tmp_path),
        fetcher=FakeFetcher(),
        summarizer=summarizer or FakeSummarizer(),
    )


class FakeRenderer:
    def __init__(self) -> None:
        self.requests: list[RenderRequest] = []

    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
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


class FailingOnceRenderer(FakeRenderer):
    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        self.requests.append(request)
        if len(self.requests) == 1:
            raise RuntimeError("ComfyUI 掉线了")
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
        should_cancel: Callable[[], bool] | None = None,
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
            r"(\d+(?:\.\d+)?)\s*s?\s*)?"
            r"\s*(续|接)?\s*\]\s*(.*)$"
        ),
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


def test_a_youtube_url_becomes_a_storyboard_that_renders(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    app = create_app(
        make_config(tmp_path),
        renderer=renderer,
        studio=fake_studio(tmp_path),
    )
    with TestClient(app) as client:
        script = client.post(
            "/v1/scripts", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
        )
        assert script.status_code == 200
        result = script.json()
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_from_youtube",
                "mode": result["mode"],
                "prompt": result["prompt"],
                "width": "864",
                "height": "480",
                "seconds": str(result["seconds"]),
            },
        )
        assert submitted.status_code == 202
        state = wait_for_done(client, "vg_from_youtube")

    assert result["source"]["video_id"] == "dQw4w9WgXcQ"
    assert [shot["prompt"] for shot in result["shots"]] == ["航拍晴朗天空", "阳光散射示意"]
    assert state["status"] == "DONE"
    assert renderer.requests[0].prompt == result["prompt"]


def test_an_unreachable_ollama_is_reported_as_a_dependency_outage(
    tmp_path: Path,
) -> None:
    app = create_app(
        make_config(tmp_path),
        renderer=FakeRenderer(),
        studio=fake_studio(tmp_path, summarizer=FailingSummarizer()),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/scripts", json={"url": "https://youtu.be/dQw4w9WgXcQ"}
        )

    assert response.status_code == 503
    assert "Ollama" in response.json()["detail"]


def test_a_link_that_is_not_youtube_is_rejected(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), studio=fake_studio(tmp_path)
    )
    with TestClient(app) as client:
        response = client.post("/v1/scripts", json={"url": "https://vimeo.com/12345"})

    assert response.status_code == 400


def test_health_stays_byte_compatible_with_the_control_plane(tmp_path: Path) -> None:
    # The sibling project parses /health with extra="forbid", so console-only
    # settings live on their own endpoint instead of being folded in here.
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        health = client.get("/health").json()
        script = client.get("/v1/scripts/config").json()

    assert "script" not in health
    assert script["enabled"] is True
    assert script["model"] == "qwen3.6:27b"
    assert script["max_shots"] == 8


def test_the_console_lists_renders_with_the_spec_that_made_them(
    tmp_path: Path,
) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        client.post(
            "/v1/renders",
            data={
                "render_id": "vg_listed",
                "mode": "t2v",
                "prompt": "晨雾中的山谷",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        assert wait_for_done(client, "vg_listed")["status"] == "DONE"
        listed = client.get("/v1/renders")

    assert listed.status_code == 200
    jobs = listed.json()
    assert len(jobs) == 1
    assert jobs[0]["render_id"] == "vg_listed"
    assert jobs[0]["prompt"] == "晨雾中的山谷"
    assert jobs[0]["mode"] == "t2v"
    assert jobs[0]["width"] == 864
    assert jobs[0]["media_url"] == "/v1/renders/vg_listed/media"
    assert jobs[0]["created_at"]
    assert jobs[0]["elapsed_seconds"] is None


def test_a_failed_render_is_retried_from_the_stored_request(tmp_path: Path) -> None:
    renderer = FailingOnceRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        client.post(
            "/v1/renders",
            data={
                "render_id": "vg_retry",
                "mode": "i2v",
                "prompt": "让湖面泛起涟漪",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
            files={"first_frame": ("first.png", b"PNG", "image/png")},
        )
        assert wait_for_done(client, "vg_retry")["status"] == "FAILED"
        retried = client.post("/v1/renders/vg_retry/retry")
        state = wait_for_done(client, "vg_retry")

    assert retried.status_code == 202
    assert state["status"] == "DONE"
    # The reference frame is re-read from disk, not re-uploaded by the browser.
    second = renderer.requests[1]
    assert second.first_frame is not None
    assert second.first_frame.read_bytes() == b"PNG"


def test_an_in_flight_render_cannot_be_retried(tmp_path: Path) -> None:
    renderer = BlockingRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        client.post(
            "/v1/renders",
            data={
                "render_id": "vg_busy",
                "mode": "t2v",
                "prompt": "湖面",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        assert renderer.started.wait(1)
        conflict = client.post("/v1/renders/vg_busy/retry")
        renderer.release.set()
        wait_for_done(client, "vg_busy")

    assert conflict.status_code == 409


def test_the_console_page_and_its_assets_are_served(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        page = client.get("/")
        script = client.get("/static/videogen.js")
        style = client.get("/static/app.css")
        traversal = client.get("/static/../config.yaml")
        unknown = client.get("/static/secrets.txt")

    assert page.status_code == 200
    assert "视频生成" in page.text
    assert script.status_code == 200
    assert "/v1/scripts" in script.text
    assert style.status_code == 200
    assert traversal.status_code == 404
    assert unknown.status_code == 404


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


def test_text_mode_refuses_a_reference_frame_it_cannot_patch(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    with TestClient(create_app(make_config(tmp_path), renderer=renderer)) as client:
        validated = client.post(
            "/v1/validate",
            json={
                "mode": "t2v",
                "prompt": "湖面",
                "width": 864,
                "height": 480,
                "seconds": 5,
                "has_first_frame": True,
                "has_last_frame": False,
            },
        )
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_stray_frame",
                "mode": "t2v",
                "prompt": "湖面",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
            files={"first_frame": ("first.png", b"PNG", "image/png")},
        )
        missing = client.get("/v1/renders/vg_stray_frame")

    assert validated.status_code == 400
    assert "不接首帧" in validated.json()["detail"]
    # The frame must be refused at the seam, not accepted and then failed after
    # the renderer has already uploaded it to ComfyUI.
    assert submitted.status_code == 400
    assert missing.status_code == 404
    assert renderer.requests == []


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
