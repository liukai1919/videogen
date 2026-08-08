"""Guards the seam the videotube control plane parses.

Every model below is copied verbatim from that project's agent/videogen.py and
agent/models.py, including extra="forbid": adding a field to one of these
responses does not merely go unused over there, it raises and takes the
automation pipeline down. The console's own richer payloads therefore live on
separate endpoints, and these tests are what keeps them separate.
"""

from pathlib import Path
from typing import Literal

from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict

from tests.test_api import FakeRenderer, make_config, wait_for_done
from videogen_service.api import create_app
from videogen_service.models import RenderProgress, RenderView


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class VideogenProgress(StrictModel):
    percent: float
    step: int
    total_steps: int
    pass_index: int
    pass_total: int


class RenderValidation(StrictModel):
    mode: str
    timeline_mode: bool
    seconds: float


class RenderServiceHealth(StrictModel):
    status: Literal["ok"]
    modes: list[str]
    timeline_modes: list[str]
    max_total_seconds: float
    max_shots: int
    fps: int
    min_seconds: float
    max_seconds: float
    length_step: int
    length_offset: int
    shot_header_pattern: str


class RemoteRenderState(StrictModel):
    render_id: str
    status: Literal["QUEUED", "RUNNING", "DONE", "FAILED"]
    progress: VideogenProgress | None = None
    error: str | None = None
    media_url: str | None = None


def client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer()))


def test_health_still_parses_on_the_control_plane(tmp_path: Path) -> None:
    with client(tmp_path) as http:
        response = http.get("/health")

    assert response.status_code == 200
    RenderServiceHealth.model_validate(response.json())


def test_validate_still_parses_on_the_control_plane(tmp_path: Path) -> None:
    with client(tmp_path) as http:
        response = http.post(
            "/v1/validate",
            json={
                "mode": "t2v",
                "prompt": "湖面",
                "width": 864,
                "height": 480,
                "seconds": 5,
                "seed": None,
                "has_first_frame": False,
                "has_last_frame": False,
            },
        )

    assert response.status_code == 200
    RenderValidation.model_validate(response.json())


def test_the_pipeline_render_round_trip_is_unchanged(tmp_path: Path) -> None:
    # Mirrors RemoteVideogenClient.render: multipart submit, poll the same id,
    # follow media_url, then delete.
    with client(tmp_path) as http:
        submitted = http.post(
            "/v1/renders",
            data={
                "render_id": "vg_pipeline",
                "mode": "i2v",
                "prompt": "让湖面泛起涟漪",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
            files={"first_frame": ("first.png", b"PNG", "image/png")},
        )
        assert submitted.status_code == 202
        RemoteRenderState.model_validate(submitted.json())

        wait_for_done(http, "vg_pipeline")
        polled = http.get("/v1/renders/vg_pipeline")
        state = RemoteRenderState.model_validate(polled.json())
        assert state.status == "DONE"
        assert state.media_url is not None

        media = http.get(state.media_url)
        assert media.status_code == 200
        assert media.content == b"FAKE-MP4"

        assert http.delete("/v1/renders/vg_pipeline").status_code == 204
        assert http.delete("/v1/renders/vg_pipeline").status_code == 404


def test_a_progress_report_still_parses_on_the_control_plane(tmp_path: Path) -> None:
    with client(tmp_path) as http:
        http.post(
            "/v1/renders",
            data={
                "render_id": "vg_progress",
                "mode": "t2v",
                "prompt": "湖面",
                "width": "864",
                "height": "480",
                "seconds": "5",
            },
        )
        wait_for_done(http, "vg_progress")
        # FakeRenderer reports one progress tick before finishing; replay it
        # through the same view the control plane polls.
        state = RemoteRenderState.model_validate(
            http.get("/v1/renders/vg_progress").json()
        )

    assert state.status == "DONE"


def test_the_console_only_endpoints_stay_off_the_frozen_ones(tmp_path: Path) -> None:
    with client(tmp_path) as http:
        health = http.get("/health").json()
        summaries = http.get("/v1/renders").json()
        script = http.get("/v1/scripts/config").json()

    # The console's extra fields must never leak into the frozen payload.
    assert set(health) == set(RenderServiceHealth.model_fields)
    assert summaries == []
    assert "enabled" in script


def test_the_polled_view_matches_the_control_plane_field_for_field() -> None:
    # A timing-free check that no field was added to the states the pipeline
    # polls, including the progress it reports mid-render.
    assert set(RenderView.model_fields) == set(RemoteRenderState.model_fields)
    assert set(RenderProgress.model_fields) == set(VideogenProgress.model_fields)
