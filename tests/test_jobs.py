from datetime import UTC, datetime
from pathlib import Path
import threading
import time
from typing import Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.artifacts import write_bytes_atomic
from videogen_service.config import Capability, ServiceConfig
from videogen_service.jobs import JobRecord, JobService, JobSpec
from videogen_service.models import RenderProgress, RenderRequest

_PNG = b"\x89PNG\r\n\x1a\nfake-image"
_WAV = b"RIFFfake-audio"


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
            "capabilities": {
                "flux-t2i": {
                    "kind": "t2i",
                    "workflow_file": str(tmp_path / "flux.json"),
                    "inputs": {
                        "prompt": ["6.text"],
                        "width": ["5.width"],
                        "height": ["5.height"],
                        "seed": ["25.noise_seed"],
                    },
                },
                "local-tts": {
                    "kind": "tts",
                    "command": ["fake-tts", "{text_file}", "{output_file}"],
                    "output_suffix": ".wav",
                },
            },
        }
    )


class FakeRunner:
    """Writes a canned output per capability kind, recording every call."""

    def __init__(self) -> None:
        self.calls: list[JobSpec] = []

    def run(
        self, spec: JobSpec, capability: Capability, *, directory: Path
    ) -> str:
        self.calls.append(spec)
        if capability.kind == "t2i":
            write_bytes_atomic(directory / "output.png", _PNG)
            return "output.png"
        write_bytes_atomic(directory / "output.wav", _WAV)
        return "output.wav"


class FailingOnceRunner(FakeRunner):
    def run(
        self, spec: JobSpec, capability: Capability, *, directory: Path
    ) -> str:
        if not self.calls:
            self.calls.append(spec)
            raise RuntimeError("ComfyUI 掉线了")
        return super().run(spec, capability, directory=directory)


class GatedRunner(FakeRunner):
    """Blocks until released, to observe the shared GPU gate from outside."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def run(
        self, spec: JobSpec, capability: Capability, *, directory: Path
    ) -> str:
        self.started.set()
        assert self.release.wait(3)
        return super().run(spec, capability, directory=directory)


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


def wait_job(client: TestClient, job_id: str, timeout: float = 3.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    state = client.get(f"/v1/jobs/{job_id}").json()
    while state["status"] not in {"DONE", "FAILED"} and time.monotonic() < deadline:
        time.sleep(0.01)
        state = client.get(f"/v1/jobs/{job_id}").json()
    return dict(state)


def test_the_capability_catalog_covers_both_lanes(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), job_runner=FakeRunner()
    )
    with TestClient(app) as client:
        catalog = client.get("/v1/capabilities").json()
    by_id = {entry["capability_id"]: entry for entry in catalog}
    assert by_id["t2v"] == {
        "capability_id": "t2v",
        "kind": "t2v",
        "output": "video",
        "submit_via": "renders",
        "needs_gpu": True,
    }
    assert by_id["flux-t2i"]["output"] == "image"
    assert by_id["flux-t2i"]["submit_via"] == "jobs"
    assert by_id["local-tts"]["output"] == "audio"
    assert by_id["local-tts"]["needs_gpu"] is False


def test_an_image_job_runs_and_serves_its_output(tmp_path: Path) -> None:
    runner = FakeRunner()
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), job_runner=runner
    )
    with TestClient(app) as client:
        submitted = client.post(
            "/v1/jobs",
            json={
                "job_id": "job_img_1",
                "capability": "flux-t2i",
                "prompt": "深蓝空间中的神经网络",
                "width": 1024,
                "height": 576,
            },
        )
        assert submitted.status_code == 202

        state = wait_job(client, "job_img_1")
        assert state["status"] == "DONE"
        assert state["media_url"] == "/v1/jobs/job_img_1/media"

        media = client.get("/v1/jobs/job_img_1/media")
        assert media.status_code == 200
        assert media.content == _PNG
        assert media.headers["content-type"] == "image/png"

        listed = client.get("/v1/jobs").json()
        assert [job["job_id"] for job in listed] == ["job_img_1"]


def test_a_tts_job_requires_text_and_serves_audio(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), job_runner=FakeRunner()
    )
    with TestClient(app) as client:
        missing = client.post(
            "/v1/jobs", json={"job_id": "job_tts_0", "capability": "local-tts"}
        )
        assert missing.status_code == 400

        submitted = client.post(
            "/v1/jobs",
            json={
                "job_id": "job_tts_1",
                "capability": "local-tts",
                "text": "双曲空间装得下指数级的分支。",
            },
        )
        assert submitted.status_code == 202
        assert wait_job(client, "job_tts_1")["status"] == "DONE"
        media = client.get("/v1/jobs/job_tts_1/media")
        assert media.headers["content-type"] == "audio/wav"
        assert media.content == _WAV


def test_unknown_capability_and_duplicate_ids_are_refused(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), job_runner=FakeRunner()
    )
    with TestClient(app) as client:
        unknown = client.post(
            "/v1/jobs",
            json={"job_id": "job_x", "capability": "nope", "prompt": "画"},
        )
        assert unknown.status_code == 400

        first = client.post(
            "/v1/jobs",
            json={"job_id": "job_dup", "capability": "flux-t2i", "prompt": "画"},
        )
        assert first.status_code == 202
        wait_job(client, "job_dup")
        second = client.post(
            "/v1/jobs",
            json={"job_id": "job_dup", "capability": "flux-t2i", "prompt": "画"},
        )
        assert second.status_code == 409


def test_a_failed_job_is_retryable_and_deletable(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path),
        renderer=FakeRenderer(),
        job_runner=FailingOnceRunner(),
    )
    with TestClient(app) as client:
        client.post(
            "/v1/jobs",
            json={"job_id": "job_r", "capability": "flux-t2i", "prompt": "画"},
        )
        state = wait_job(client, "job_r")
        assert state["status"] == "FAILED"
        assert "ComfyUI 掉线了" in str(state["error"])

        assert client.post("/v1/jobs/job_r/retry").status_code == 202
        assert wait_job(client, "job_r")["status"] == "DONE"

        assert client.delete("/v1/jobs/job_r").status_code == 204
        assert client.get("/v1/jobs/job_r").status_code == 404


def test_an_image_output_can_be_archived_as_an_asset(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), job_runner=FakeRunner()
    )
    with TestClient(app) as client:
        client.post(
            "/v1/jobs",
            json={"job_id": "job_a", "capability": "flux-t2i", "prompt": "主角"},
        )
        wait_job(client, "job_a")
        archived = client.post(
            "/v1/jobs/job_a/save-asset", json={"name": "主角定妆", "category": "角色"}
        )
        assert archived.status_code == 201
        asset = archived.json()
        assert client.get(asset["media_url"]).content == _PNG


def test_gpu_jobs_and_renders_share_one_gate(tmp_path: Path) -> None:
    runner = GatedRunner()
    renderer = FakeRenderer()
    app = create_app(make_config(tmp_path), renderer=renderer, job_runner=runner)
    with TestClient(app) as client:
        client.post(
            "/v1/jobs",
            json={"job_id": "job_gate", "capability": "flux-t2i", "prompt": "画"},
        )
        assert runner.started.wait(3)

        # A render submitted while the image job holds the gate must wait.
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_gate",
                "mode": "t2v",
                "prompt": "山谷推近",
                "width": "864",
                "height": "480",
                "seconds": "6",
            },
        )
        assert submitted.status_code == 202
        time.sleep(0.1)
        assert renderer.requests == []  # still parked at the gate

        runner.release.set()
        deadline = time.monotonic() + 3
        while (
            client.get("/v1/renders/vg_gate").json()["status"] != "DONE"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert client.get("/v1/renders/vg_gate").json()["status"] == "DONE"
        assert len(renderer.requests) == 1


def test_restart_recovery_fails_interrupted_jobs(tmp_path: Path) -> None:
    # A RUNNING record written straight to disk stands in for the crashed
    # service, so no live worker races the recovery we are asserting on.
    config = make_config(tmp_path)
    now = datetime.now(UTC).isoformat()
    record = JobRecord(
        spec=JobSpec(job_id="job_int", capability="flux-t2i", prompt="画"),
        kind="t2i",
        status="RUNNING",
        created_at=now,
        updated_at=now,
    )
    path = config.work_dir / ".jobs" / "job_int" / "job.json"
    path.parent.mkdir(parents=True)
    path.write_text(record.model_dump_json(), encoding="utf-8")

    service = JobService(config, runner=FakeRunner())
    try:
        view = service.get("job_int")
        assert view.status == "FAILED"
        assert "重启" in str(view.error)
    finally:
        service.close()
