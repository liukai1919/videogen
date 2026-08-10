from datetime import UTC, datetime
from pathlib import Path
import threading
import time
from typing import Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.config import ServiceConfig
from videogen_service.models import (
    RenderProgress,
    RenderRequest,
    ScriptRequest,
    ScriptResult,
    ScriptShot,
    ScriptSource,
)
from videogen_service.pipeline import PipelineRecord, PipelineRequest
from videogen_service.projects import ProjectStore
from videogen_service.scripting import ScriptUnavailable


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
                "story": {
                    "workflow_file": str(tmp_path / "story.json"),
                    "inputs": {"prompt": ["1.text"], "timeline": ["1.timeline"]},
                },
            },
        }
    )


def script_result() -> ScriptResult:
    return ScriptResult(
        source=ScriptSource(
            video_id="dQw4w9WgXcQ",
            title="Why the sky is blue",
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            subtitle_language="en",
            automatic_captions=True,
            duration_seconds=640.0,
            transcript_chars=100,
            transcript_truncated=False,
        ),
        title="蓝天为什么是蓝的",
        summary="短波长的蓝光被散射得最多。",
        mode="story",
        prompt="科普短片。\n\n[0s-6s] 航拍晴朗天空",
        seconds=6.0,
        shots=[ScriptShot(seconds=6, prompt="航拍晴朗天空", narration="抬头就是蓝色")],
    )


class StubStudio:
    def __init__(self) -> None:
        self.requests: list[ScriptRequest] = []

    def create(self, request: ScriptRequest) -> ScriptResult:
        self.requests.append(request)
        return script_result()


class FailingOnceStudio(StubStudio):
    def create(self, request: ScriptRequest) -> ScriptResult:
        self.requests.append(request)
        if len(self.requests) == 1:
            raise ScriptUnavailable("连不上 Ollama(http://127.0.0.1:11434)")
        return script_result()


class BlockingStudio(StubStudio):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def create(self, request: ScriptRequest) -> ScriptResult:
        self.requests.append(request)
        self.started.set()
        assert self.release.wait(3)
        return script_result()


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


def wait_pipeline(
    client: TestClient, project_id: str, *statuses: str, timeout: float = 3.0
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(f"/v1/projects/{project_id}/pipeline")
        if response.status_code == 200 and response.json()["status"] in statuses:
            return dict(response.json())
        if time.monotonic() > deadline:
            raise AssertionError(
                f"流水线没有到达 {statuses}: {response.status_code} {response.text}"
            )
        time.sleep(0.01)


def start_run(client: TestClient, project_id: str = "sky") -> None:
    created = client.post(
        "/v1/projects", json={"project_id": project_id, "name": "蓝天科普"}
    )
    assert created.status_code == 201
    started = client.post(
        f"/v1/projects/{project_id}/pipeline",
        json={"url": "https://youtu.be/dQw4w9WgXcQ", "width": 864, "height": 480},
    )
    assert started.status_code == 202


def test_the_full_flow_scripts_reviews_and_renders(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    app = create_app(make_config(tmp_path), renderer=renderer, studio=StubStudio())
    with TestClient(app) as client:
        start_run(client)
        run = wait_pipeline(client, "sky", "AWAITING_REVIEW")
        assert run["draft_id"] == 1
        draft = run["draft"]
        assert isinstance(draft, dict)
        assert draft["result"]["title"] == "蓝天为什么是蓝的"

        approved = client.post("/v1/projects/sky/pipeline/approve", json={})
        assert approved.status_code == 202
        assert approved.json()["status"] in {"RENDERING", "DONE"}
        assert approved.json()["review"]["prompt_overridden"] is False

        run = wait_pipeline(client, "sky", "DONE")
        render_id = run["render_id"]
        assert isinstance(render_id, str) and render_id.startswith("pl_")
        assert run["render"]["media_url"] == f"/v1/renders/{render_id}/media"

        project = client.get("/v1/projects/sky").json()
        assert project["render_ids"] == [render_id]
        assert len(project["drafts"]) == 1


def test_an_edited_storyboard_is_what_renders(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    app = create_app(make_config(tmp_path), renderer=renderer, studio=StubStudio())
    with TestClient(app) as client:
        start_run(client)
        wait_pipeline(client, "sky", "AWAITING_REVIEW")

        edited = "科普短片。\n\n[0s-8s] 改过的画面描述"
        approved = client.post(
            "/v1/projects/sky/pipeline/approve", json={"prompt": edited}
        )
        assert approved.status_code == 202
        assert approved.json()["review"]["prompt_overridden"] is True

        wait_pipeline(client, "sky", "DONE")
        assert renderer.requests[0].prompt == edited
        # The archived draft keeps the original text; only the render changed.
        draft = client.get("/v1/projects/sky").json()["drafts"][0]
        assert "改过的画面描述" not in draft["result"]["prompt"]


def test_a_broken_edit_keeps_the_run_reviewable(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), studio=StubStudio()
    )
    with TestClient(app) as client:
        start_run(client)
        wait_pipeline(client, "sky", "AWAITING_REVIEW")

        bad = client.post(
            "/v1/projects/sky/pipeline/approve",
            json={"prompt": "[0s-100s] 一段一百秒,超出上限"},
        )
        assert bad.status_code == 400

        still = client.get("/v1/projects/sky/pipeline").json()
        assert still["status"] == "AWAITING_REVIEW"
        assert (
            client.post("/v1/projects/sky/pipeline/approve", json={}).status_code
            == 202
        )


def test_rejection_ends_the_run_and_frees_the_project(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), studio=StubStudio()
    )
    with TestClient(app) as client:
        start_run(client)
        wait_pipeline(client, "sky", "AWAITING_REVIEW")

        rejected = client.post(
            "/v1/projects/sky/pipeline/reject", json={"reason": "分镜太散"}
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "REJECTED"
        assert rejected.json()["review"]["reason"] == "分镜太散"

        again = client.post(
            "/v1/projects/sky/pipeline",
            json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        assert again.status_code == 202


def test_only_one_active_run_per_project(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), studio=StubStudio()
    )
    with TestClient(app) as client:
        start_run(client)
        wait_pipeline(client, "sky", "AWAITING_REVIEW")
        second = client.post(
            "/v1/projects/sky/pipeline",
            json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        assert second.status_code == 409


def test_a_missing_project_cannot_start_a_pipeline(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), studio=StubStudio()
    )
    with TestClient(app) as client:
        started = client.post(
            "/v1/projects/nowhere/pipeline",
            json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        assert started.status_code == 404


def test_a_script_failure_is_retryable(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), studio=FailingOnceStudio()
    )
    with TestClient(app) as client:
        start_run(client)
        run = wait_pipeline(client, "sky", "FAILED")
        assert "连不上 Ollama" in str(run["error"])

        retried = client.post("/v1/projects/sky/pipeline/retry")
        assert retried.status_code == 202
        wait_pipeline(client, "sky", "AWAITING_REVIEW")


def test_a_render_failure_is_retried_without_a_new_script(tmp_path: Path) -> None:
    renderer = FailingOnceRenderer()
    app = create_app(make_config(tmp_path), renderer=renderer, studio=StubStudio())
    with TestClient(app) as client:
        start_run(client)
        wait_pipeline(client, "sky", "AWAITING_REVIEW")
        assert (
            client.post("/v1/projects/sky/pipeline/approve", json={}).status_code
            == 202
        )
        run = wait_pipeline(client, "sky", "FAILED")
        assert "ComfyUI 掉线了" in str(run["error"])

        retried = client.post("/v1/projects/sky/pipeline/retry")
        assert retried.status_code == 202
        run = wait_pipeline(client, "sky", "DONE")
        # Still the first script: retry re-queued the render, not the studio.
        assert len(renderer.requests) == 2
        project = client.get("/v1/projects/sky").json()
        assert len(project["drafts"]) == 1


def test_restart_turns_an_interrupted_script_into_a_retryable_failure(
    tmp_path: Path,
) -> None:
    config = make_config(tmp_path)
    ProjectStore(config.work_dir).create(project_id="sky", name="蓝天科普")
    now = datetime.now(UTC).isoformat()
    record = PipelineRecord(
        project_id="sky",
        request=PipelineRequest(url="https://youtu.be/dQw4w9WgXcQ"),
        status="SCRIPTING",
        created_at=now,
        updated_at=now,
    )
    path = config.work_dir / ".projects" / "sky" / "pipeline.json"
    path.write_text(record.model_dump_json(), encoding="utf-8")

    app = create_app(config, renderer=FakeRenderer(), studio=StubStudio())
    with TestClient(app) as client:
        run = client.get("/v1/projects/sky/pipeline").json()
        assert run["status"] == "FAILED"
        assert "重启" in run["error"]


def test_cancelling_a_scripting_run_discards_its_outcome(tmp_path: Path) -> None:
    studio = BlockingStudio()
    app = create_app(make_config(tmp_path), renderer=FakeRenderer(), studio=studio)
    with TestClient(app) as client:
        start_run(client)
        assert studio.started.wait(3)

        assert client.delete("/v1/projects/sky/pipeline").status_code == 204
        assert client.get("/v1/projects/sky/pipeline").status_code == 404

        studio.release.set()
        time.sleep(0.05)  # let the worker finish and hit the missing record
        assert client.get("/v1/projects/sky/pipeline").status_code == 404

        # The project is free for a fresh run, which now completes normally.
        again = client.post(
            "/v1/projects/sky/pipeline",
            json={"url": "https://youtu.be/dQw4w9WgXcQ"},
        )
        assert again.status_code == 202
        wait_pipeline(client, "sky", "AWAITING_REVIEW")
