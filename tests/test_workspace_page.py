from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.config import ServiceConfig
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


def test_the_workspace_page_and_its_script_are_served(tmp_path: Path) -> None:
    with TestClient(create_app(make_config(tmp_path), renderer=FakeRenderer())) as client:
        page = client.get("/workspace")
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
        assert "ws-shell" in page.text
        assert "/static/workspace.js" in page.text

        script = client.get("/static/workspace.js")
        assert script.status_code == 200
        assert "text/javascript" in script.headers["content-type"]
        assert "/v1/chats" in script.text
