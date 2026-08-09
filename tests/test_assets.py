from pathlib import Path
import time
from typing import Callable

from fastapi.testclient import TestClient
import pytest

from videogen_service.api import create_app
from videogen_service.assets import AssetError, AssetNotFound, AssetStore
from videogen_service.config import ServiceConfig
from videogen_service.models import RenderProgress, RenderRequest

_PNG = b"\x89PNG\r\n\x1a\nfake-image-bytes"


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
                "i2v": {
                    "workflow_file": str(tmp_path / "i2v.json"),
                    "inputs": {"prompt": ["1.text"], "first_frame": ["2.image"]},
                },
            },
        }
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
        return b"FAKE-MP4"


def wait_for_done(client: TestClient, render_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    state = client.get(f"/v1/renders/{render_id}").json()
    while state["status"] not in {"DONE", "FAILED"} and time.monotonic() < deadline:
        time.sleep(0.01)
        state = client.get(f"/v1/renders/{render_id}").json()
    return dict(state)


# ---- AssetStore --------------------------------------------------------------


def test_assets_are_created_listed_and_deleted(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "work")
    record = store.create(
        name="主角小熊", category="角色", filename="bear.png", data=_PNG
    )
    assert record.asset_id.startswith("asset_")
    assert record.filename == "media.png"
    assert store.media_path(record.asset_id).read_bytes() == _PNG

    assert [asset.name for asset in store.list()] == ["主角小熊"]

    store.delete(record.asset_id)
    assert store.list() == []
    with pytest.raises(AssetNotFound):
        store.get(record.asset_id)


def test_non_image_files_are_refused(tmp_path: Path) -> None:
    store = AssetStore(tmp_path / "work")
    with pytest.raises(AssetError, match="格式不支持"):
        store.create(name="坏文件", category="", filename="movie.mp4", data=b"x")


# ---- HTTP seam ---------------------------------------------------------------


def test_an_asset_is_uploaded_served_and_deleted(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    with TestClient(app) as client:
        created = client.post(
            "/v1/assets",
            data={"name": "主角小熊", "category": "角色"},
            files={"file": ("bear.png", _PNG, "image/png")},
        )
        assert created.status_code == 201
        asset = created.json()
        assert asset["name"] == "主角小熊"
        media_url = asset["media_url"]

        listed = client.get("/v1/assets")
        assert [item["asset_id"] for item in listed.json()] == [asset["asset_id"]]

        served = client.get(media_url)
        assert served.status_code == 200
        assert served.content == _PNG
        assert served.headers["content-type"] == "image/png"

        assert client.delete(f"/v1/assets/{asset['asset_id']}").status_code == 204
        assert client.get(media_url).status_code == 404


def test_a_render_can_take_its_frame_from_an_asset(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    app = create_app(make_config(tmp_path), renderer=renderer)
    with TestClient(app) as client:
        asset = client.post(
            "/v1/assets",
            data={"name": "主角小熊", "category": "角色"},
            files={"file": ("bear.png", _PNG, "image/png")},
        ).json()

        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_asset_1",
                "mode": "i2v",
                "prompt": "小熊在草地上散步",
                "width": "864",
                "height": "480",
                "seconds": "6",
                "first_frame_asset": asset["asset_id"],
            },
        )
        assert submitted.status_code == 202
        assert wait_for_done(client, "vg_asset_1")["status"] == "DONE"

        frame = renderer.requests[0].first_frame
        assert frame is not None and frame.read_bytes() == _PNG

        # The render keeps its own copy: deleting the asset breaks nothing.
        assert client.delete(f"/v1/assets/{asset['asset_id']}").status_code == 204
        summary = client.get("/v1/renders").json()[0]
        assert summary["has_first_frame"] is True


def test_an_uploaded_file_beats_the_asset_field(tmp_path: Path) -> None:
    renderer = FakeRenderer()
    app = create_app(make_config(tmp_path), renderer=renderer)
    with TestClient(app) as client:
        asset = client.post(
            "/v1/assets",
            data={"name": "备用图"},
            files={"file": ("spare.png", _PNG, "image/png")},
        ).json()
        uploaded = b"\x89PNG\r\n\x1a\ndirect-upload"

        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_asset_2",
                "mode": "i2v",
                "prompt": "小熊在草地上散步",
                "width": "864",
                "height": "480",
                "seconds": "6",
                "first_frame_asset": asset["asset_id"],
            },
            files={"first_frame": ("mine.png", uploaded, "image/png")},
        )
        assert submitted.status_code == 202
        wait_for_done(client, "vg_asset_2")
        frame = renderer.requests[0].first_frame
        assert frame is not None and frame.read_bytes() == uploaded


def test_a_missing_asset_fails_the_submit_cleanly(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    with TestClient(app) as client:
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_asset_3",
                "mode": "i2v",
                "prompt": "小熊在草地上散步",
                "width": "864",
                "height": "480",
                "seconds": "6",
                "first_frame_asset": "asset_nowhere",
            },
        )
        assert submitted.status_code == 404
        # Nothing was queued for it.
        assert client.get("/v1/renders/vg_asset_3").status_code == 404


def test_a_reference_frame_is_archived_from_a_render(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    with TestClient(app) as client:
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_asset_4",
                "mode": "i2v",
                "prompt": "小熊在草地上散步",
                "width": "864",
                "height": "480",
                "seconds": "6",
            },
            files={"first_frame": ("bear.png", _PNG, "image/png")},
        )
        assert submitted.status_code == 202

        archived = client.post(
            "/v1/assets/from-render",
            json={"render_id": "vg_asset_4", "slot": "first", "name": "小熊定妆照"},
        )
        assert archived.status_code == 201
        asset = archived.json()
        assert asset["category"] == "参考帧"
        assert client.get(asset["media_url"]).content == _PNG

        missing_slot = client.post(
            "/v1/assets/from-render",
            json={"render_id": "vg_asset_4", "slot": "last", "name": "不存在的尾帧"},
        )
        assert missing_slot.status_code == 404
