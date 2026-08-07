# Exercises the ComfyUI client at its HTTP and workflow-patching boundaries.
# Inputs are fake urlopen responses and a miniature API-format workflow.
# Assertions cover patching, submit rejection, execution errors, and timeouts.

import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

import pytest

import videogen_service.comfyui as comfyui_module
from videogen_service.comfyui import (
    ComfyUiClient,
    ComfyUiError,
    load_workflow,
    patch_workflow,
)

POINTERS = {
    "prompt": ["4.text"],
    "width": ["6.width", "7.width"],
    "height": ["6.height", "7.height"],
    "seed": ["10.noise_seed"],
}


def sample_workflow() -> dict[str, Any]:
    return {
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "", "clip": ["2", 0]}},
        "6": {
            "class_type": "EmptyFlux2LatentImage",
            "inputs": {"width": 8, "height": 8, "batch_size": 1},
        },
        "7": {
            "class_type": "Flux2Scheduler",
            "inputs": {"steps": 4, "width": 8, "height": 8},
        },
        "10": {"class_type": "RandomNoise", "inputs": {"noise_seed": 0}},
    }


def sample_values() -> dict[str, Any]:
    return {"prompt": "夜色书桌", "width": 1536, "height": 864, "seed": 42}


class FakeResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def read(self) -> bytes:
        return self.body

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *arguments: object) -> bool:
        return False


def install_routes(
    monkeypatch: Any, routes: dict[str, object], calls: list[str] | None = None
) -> None:
    def urlopen(request: Any, timeout: float | None = None) -> FakeResponse:
        url = request.full_url
        if calls is not None:
            calls.append(url)
        for prefix, payload in routes.items():
            if url.startswith(prefix):
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, bytes):
                    return FakeResponse(payload)
                if callable(payload):
                    return FakeResponse(payload())
                return FakeResponse(json.dumps(payload).encode("utf-8"))
        raise AssertionError(f"unexpected request: {url}")

    monkeypatch.setattr(comfyui_module.urllib.request, "urlopen", urlopen)


def make_client(**overrides: Any) -> ComfyUiClient:
    settings: dict[str, Any] = {
        "base_url": "http://127.0.0.1:8188",
        "timeout_seconds": 5.0,
        "poll_interval_seconds": 0.0,
    }
    settings.update(overrides)
    return ComfyUiClient(**settings)


def test_patching_reaches_every_node_a_pointer_names() -> None:
    patched = patch_workflow(
        sample_workflow(), pointers=POINTERS, values=sample_values()
    )

    assert patched["4"]["inputs"]["text"] == "夜色书桌"
    assert patched["6"]["inputs"]["width"] == 1536
    assert patched["7"]["inputs"]["width"] == 1536
    assert patched["6"]["inputs"]["height"] == 864
    assert patched["7"]["inputs"]["height"] == 864
    assert patched["10"]["inputs"]["noise_seed"] == 42


def test_patching_leaves_the_loaded_workflow_untouched() -> None:
    workflow = sample_workflow()

    patch_workflow(workflow, pointers=POINTERS, values=sample_values())

    assert workflow["4"]["inputs"]["text"] == ""
    assert workflow["6"]["inputs"]["width"] == 8


def test_a_pointer_to_a_missing_node_names_the_pointer() -> None:
    pointers = dict(POINTERS, prompt=["99.text"])

    with pytest.raises(ComfyUiError, match="99"):
        patch_workflow(sample_workflow(), pointers=pointers, values=sample_values())


def test_a_pointer_to_a_missing_field_names_the_class() -> None:
    pointers = dict(POINTERS, prompt=["4.prompt"])

    with pytest.raises(ComfyUiError, match="CLIPTextEncode"):
        patch_workflow(sample_workflow(), pointers=pointers, values=sample_values())


def test_a_value_without_a_pointer_is_rejected() -> None:
    pointers = {name: targets for name, targets in POINTERS.items() if name != "seed"}

    with pytest.raises(ComfyUiError, match="seed"):
        patch_workflow(sample_workflow(), pointers=pointers, values=sample_values())


def test_the_editor_save_format_is_refused_with_the_export_hint(
    tmp_path: Path,
) -> None:
    workflow_file = tmp_path / "ui.json"
    workflow_file.write_text(
        json.dumps({"nodes": [], "links": [], "version": 1}), encoding="utf-8"
    )

    with pytest.raises(ComfyUiError, match="Export"):
        load_workflow(workflow_file)


def test_a_missing_workflow_file_names_the_path(tmp_path: Path) -> None:
    with pytest.raises(ComfyUiError, match="workflow"):
        load_workflow(tmp_path / "absent.json")


def test_rendering_submits_polls_and_downloads_the_image(monkeypatch: Any) -> None:
    calls: list[str] = []
    install_routes(
        monkeypatch,
        {
            "http://127.0.0.1:8188/prompt": {"prompt_id": "p1", "node_errors": {}},
            "http://127.0.0.1:8188/history/p1": {
                "p1": {
                    "status": {"status_str": "success"},
                    "outputs": {
                        "13": {
                            "images": [
                                {
                                    "filename": "cover_0001.png",
                                    "subfolder": "",
                                    "type": "output",
                                }
                            ]
                        }
                    },
                }
            },
            "http://127.0.0.1:8188/view": b"PNGDATA",
        },
        calls,
    )

    image = make_client().render(sample_workflow())

    assert image.data == b"PNGDATA"
    assert image.filename == "cover_0001.png"
    assert calls[0].endswith("/prompt")
    assert "filename=cover_0001.png" in calls[-1]


def test_rendering_returns_the_mp4_a_save_video_node_reported(monkeypatch: Any) -> None:
    # SaveVideo files itself under "images" with an "animated" marker, so nothing
    # but the filename distinguishes a clip from a cover on the way back.
    install_routes(
        monkeypatch,
        {
            "http://127.0.0.1:8188/prompt": {"prompt_id": "p1"},
            "http://127.0.0.1:8188/history/p1": {
                "p1": {
                    "status": {"status_str": "success"},
                    "outputs": {
                        "92": {
                            "images": [
                                {
                                    "filename": "MiniMax_H3_00001_.mp4",
                                    "subfolder": "video",
                                    "type": "output",
                                }
                            ],
                            "animated": [True],
                        }
                    },
                }
            },
            "http://127.0.0.1:8188/view": b"MP4DATA",
        },
    )

    output = make_client().render(sample_workflow())

    assert output.data == b"MP4DATA"
    assert output.filename.endswith(".mp4")


def test_a_rejected_workflow_surfaces_the_node_errors(monkeypatch: Any) -> None:
    install_routes(
        monkeypatch,
        {
            "http://127.0.0.1:8188/prompt": {
                "prompt_id": "p1",
                "node_errors": {"1": {"errors": [{"message": "value not in list"}]}},
            }
        },
    )

    with pytest.raises(ComfyUiError, match="value not in list"):
        make_client().render(sample_workflow())


def test_an_http_error_body_reaches_the_message(monkeypatch: Any) -> None:
    import io

    error = HTTPError(
        url="http://127.0.0.1:8188/prompt",
        code=400,
        msg="Bad Request",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"error": "prompt outputs failed validation"}'),
    )
    install_routes(monkeypatch, {"http://127.0.0.1:8188/prompt": error})

    with pytest.raises(ComfyUiError, match="failed validation"):
        make_client().render(sample_workflow())


def test_an_unreachable_server_says_so(monkeypatch: Any) -> None:
    install_routes(
        monkeypatch,
        {"http://127.0.0.1:8188/prompt": URLError("Connection refused")},
    )

    with pytest.raises(ComfyUiError, match="ComfyUI 已启动"):
        make_client().render(sample_workflow())


def test_an_execution_error_surfaces_the_message(monkeypatch: Any) -> None:
    install_routes(
        monkeypatch,
        {
            "http://127.0.0.1:8188/prompt": {"prompt_id": "p1"},
            "http://127.0.0.1:8188/history/p1": {
                "p1": {
                    "status": {
                        "status_str": "error",
                        "messages": [["execution_error", {"exception_message": "OOM"}]],
                    },
                    "outputs": {},
                }
            },
        },
    )

    with pytest.raises(ComfyUiError, match="OOM"):
        make_client().render(sample_workflow())


def test_a_finished_run_without_images_is_an_error(monkeypatch: Any) -> None:
    install_routes(
        monkeypatch,
        {
            "http://127.0.0.1:8188/prompt": {"prompt_id": "p1"},
            "http://127.0.0.1:8188/history/p1": {
                "p1": {"status": {"status_str": "success"}, "outputs": {"13": {}}}
            },
        },
    )

    with pytest.raises(ComfyUiError, match="SaveImage"):
        make_client(timeout_seconds=0.0).render(sample_workflow())


def test_a_run_that_never_finishes_times_out(monkeypatch: Any) -> None:
    install_routes(
        monkeypatch,
        {
            "http://127.0.0.1:8188/prompt": {"prompt_id": "p1"},
            "http://127.0.0.1:8188/history/p1": {},
        },
    )

    with pytest.raises(ComfyUiError, match="超时"):
        make_client(timeout_seconds=0.0).render(sample_workflow())
