from pathlib import Path
import time
from typing import Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.artifacts import write_bytes_atomic
from videogen_service.config import ServiceConfig
from videogen_service.jobs import AnyCapability, JobSpec, ffmpeg_command
from videogen_service.models import RenderProgress, RenderRequest


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


class FakeRenderer:
    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
    ) -> bytes:
        return b"FAKE-MP4"


class RecordingRunner:
    def __init__(self) -> None:
        self.specs: list[JobSpec] = []

    def run(
        self, spec: JobSpec, capability: AnyCapability, *, directory: Path
    ) -> str:
        self.specs.append(spec)
        write_bytes_atomic(directory / "output.mp4", b"FAKE-FINAL")
        return "output.mp4"


def wait_job(client: TestClient, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    state = client.get(f"/v1/jobs/{job_id}").json()
    while state["status"] not in {"DONE", "FAILED"} and time.monotonic() < deadline:
        time.sleep(0.01)
        state = client.get(f"/v1/jobs/{job_id}").json()
    return dict(state)


# ---- ffmpeg 命令构造 ----------------------------------------------------------


def test_audio_only_copies_the_video_stream() -> None:
    command = ffmpeg_command(
        video=Path("/w/vg_1/output.mp4"),
        audio=Path("/w/.jobs/job_tts/output.wav"),
        subtitles=None,
        output="output.mp4",
    )
    assert command[:6] == [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        "/w/vg_1/output.mp4",
        "-i",
    ]
    assert "-c:v" in command and command[command.index("-c:v") + 1] == "copy"
    assert ["-map", "0:v:0"] == command[command.index("-map") : command.index("-map") + 2]
    assert "1:a:0" in command and "aac" in command
    assert "-shortest" not in command  # 旁白短于画面时不能截断视频


def test_narration_mixes_over_native_audio() -> None:
    command = ffmpeg_command(
        video=Path("/w/vg_1/output.mp4"),
        audio=Path("/w/.jobs/job_tts/output.wav"),
        subtitles=None,
        output="output.mp4",
        mix_native=True,
    )
    graph = command[command.index("-filter_complex") + 1]
    assert "volume=0.3" in graph and "amix=inputs=2" in graph
    assert "duration=first" in graph  # 画面原声定长:解说短了原声继续
    assert "[aout]" in command  # 出的是混音结果,不是解说直通
    assert "1:a:0" not in command


def test_subtitles_force_a_reencode() -> None:
    command = ffmpeg_command(
        video=Path("/w/vg_1/output.mp4"),
        audio=None,
        subtitles="subs.srt",
        output="output.mp4",
    )
    assert "subtitles=subs.srt" in command
    assert "libx264" in command
    assert "0:a?" in command  # 源视频没有音轨也不报错


def test_compose_validation_over_http(tmp_path: Path) -> None:
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), job_runner=RecordingRunner()
    )
    with TestClient(app) as client:
        missing_render = client.post(
            "/v1/jobs",
            json={"job_id": "job_c0", "capability": "compose", "subtitles": "1\n..."},
        )
        assert missing_render.status_code == 400

        nothing_to_add = client.post(
            "/v1/jobs",
            json={"job_id": "job_c1", "capability": "compose", "render_id": "vg_1"},
        )
        assert nothing_to_add.status_code == 400
        assert "至少需要" in nothing_to_add.json()["detail"]


def test_compose_runs_through_the_job_queue(tmp_path: Path) -> None:
    runner = RecordingRunner()
    app = create_app(
        make_config(tmp_path), renderer=FakeRenderer(), job_runner=runner
    )
    with TestClient(app) as client:
        submitted = client.post(
            "/v1/jobs",
            json={
                "job_id": "job_final",
                "capability": "compose",
                "render_id": "vg_src",
                "subtitles": "1\n00:00:00,000 --> 00:00:06,583\n第一句\n",
            },
        )
        assert submitted.status_code == 202

        state = wait_job(client, "job_final")
        assert state["status"] == "DONE"
        assert state["kind"] == "compose"
        assert state["render_id"] == "vg_src"

        media = client.get("/v1/jobs/job_final/media")
        assert media.headers["content-type"] == "video/mp4"
        assert media.content == b"FAKE-FINAL"

        assert runner.specs[0].subtitles is not None

        catalog = client.get("/v1/capabilities").json()
        compose = [c for c in catalog if c["capability_id"] == "compose"]
        assert compose == [
            {
                "capability_id": "compose",
                "kind": "compose",
                "output": "video",
                "submit_via": "jobs",
                "needs_gpu": False,
            }
        ]
