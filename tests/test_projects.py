from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient
import pytest

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
from videogen_service.projects import (
    ProjectConflict,
    ProjectNotFound,
    ProjectStore,
)


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
        }
    )


def script_request(**overrides: object) -> ScriptRequest:
    values: dict[str, object] = {"url": "https://youtu.be/dQw4w9WgXcQ"}
    values.update(overrides)
    return ScriptRequest.model_validate(values)


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
    """Answers every script request with the same canned result."""

    def create(self, request: ScriptRequest) -> ScriptResult:
        return script_result()


class FakeRenderer:
    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
    ) -> bytes:
        return b"FAKE-MP4"


# ---- ProjectStore ------------------------------------------------------------


def test_projects_are_created_listed_and_deleted(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "work")

    created = store.create(project_id=None, name="蓝天科普")
    assert created.project_id.startswith("proj_")
    assert store.get(created.project_id).name == "蓝天科普"

    summaries = store.list()
    assert [summary.name for summary in summaries] == ["蓝天科普"]
    assert summaries[0].draft_count == 0
    assert summaries[0].render_count == 0

    store.delete(created.project_id)
    assert store.list() == []
    with pytest.raises(ProjectNotFound):
        store.get(created.project_id)


def test_an_explicit_id_cannot_be_taken_twice(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "work")
    store.create(project_id="sky", name="第一个")
    with pytest.raises(ProjectConflict):
        store.create(project_id="sky", name="第二个")


def test_drafts_are_numbered_in_order(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "work")
    store.create(project_id="sky", name="蓝天科普")

    first = store.add_draft("sky", request=script_request(), result=script_result())
    second = store.add_draft(
        "sky", request=script_request(guidance="面向初中生"), result=script_result()
    )

    assert (first.draft_id, second.draft_id) == (1, 2)
    record = store.get("sky")
    assert len(record.drafts) == 2
    assert record.drafts[1].request.guidance == "面向初中生"
    assert record.updated_at >= record.created_at


def test_linking_a_render_is_idempotent(tmp_path: Path) -> None:
    store = ProjectStore(tmp_path / "work")
    store.create(project_id="sky", name="蓝天科普")

    store.link_render("sky", "vg_001")
    record = store.link_render("sky", "vg_001")
    assert record.render_ids == ["vg_001"]


def test_the_store_never_collides_with_render_directories(tmp_path: Path) -> None:
    # ".projects" contains a dot, which the render id pattern rejects, so a
    # render can never claim the directory the store lives in.
    store = ProjectStore(tmp_path / "work")
    record = store.create(project_id="sky", name="蓝天科普")
    assert (tmp_path / "work" / ".projects" / "sky" / "project.json").is_file()
    assert record.project_id == "sky"


# ---- HTTP seam ---------------------------------------------------------------


def test_projects_crud_over_http(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    with TestClient(app) as client:
        created = client.post("/v1/projects", json={"name": "蓝天科普"})
        assert created.status_code == 201
        project_id = created.json()["project_id"]

        listed = client.get("/v1/projects")
        assert listed.status_code == 200
        assert [item["project_id"] for item in listed.json()] == [project_id]

        detail = client.get(f"/v1/projects/{project_id}")
        assert detail.status_code == 200
        assert detail.json()["name"] == "蓝天科普"
        assert detail.json()["drafts"] == []

        assert client.delete(f"/v1/projects/{project_id}").status_code == 204
        assert client.delete(f"/v1/projects/{project_id}").status_code == 404


def test_a_duplicate_project_id_is_a_conflict(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    with TestClient(app) as client:
        first = client.post(
            "/v1/projects", json={"project_id": "sky", "name": "第一个"}
        )
        assert first.status_code == 201
        second = client.post(
            "/v1/projects", json={"project_id": "sky", "name": "第二个"}
        )
        assert second.status_code == 409


def test_a_script_with_a_project_is_archived_as_a_draft(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), studio=StubStudio()
    )
    with TestClient(app) as client:
        client.post("/v1/projects", json={"project_id": "sky", "name": "蓝天科普"})

        scripted = client.post(
            "/v1/scripts",
            json={"url": "https://youtu.be/dQw4w9WgXcQ", "project_id": "sky"},
        )
        assert scripted.status_code == 200

        detail = client.get("/v1/projects/sky").json()
        assert len(detail["drafts"]) == 1
        assert detail["drafts"][0]["draft_id"] == 1
        assert detail["drafts"][0]["result"]["title"] == "蓝天为什么是蓝的"
        assert detail["drafts"][0]["request"]["project_id"] == "sky"


def test_a_script_for_a_missing_project_is_refused_up_front(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), studio=StubStudio()
    )
    with TestClient(app) as client:
        scripted = client.post(
            "/v1/scripts",
            json={"url": "https://youtu.be/dQw4w9WgXcQ", "project_id": "missing"},
        )
        assert scripted.status_code == 404


def test_a_render_is_linked_only_if_it_exists(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    with TestClient(app) as client:
        client.post("/v1/projects", json={"project_id": "sky", "name": "蓝天科普"})

        missing = client.post(
            "/v1/projects/sky/renders", json={"render_id": "vg_missing"}
        )
        assert missing.status_code == 404

        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_001",
                "mode": "t2v",
                "prompt": "航拍晴朗天空",
                "width": "864",
                "height": "480",
                "seconds": "6",
            },
        )
        assert submitted.status_code == 202

        linked = client.post(
            "/v1/projects/sky/renders", json={"render_id": "vg_001"}
        )
        assert linked.status_code == 200
        assert linked.json()["render_ids"] == ["vg_001"]

        into_missing = client.post(
            "/v1/projects/nowhere/renders", json={"render_id": "vg_001"}
        )
        assert into_missing.status_code == 404
