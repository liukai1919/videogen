from pathlib import Path
import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.downloads import DownloadError, DownloadService
from test_api import FakeRenderer, make_config


class FakeEngine:
    def info(self, url: str) -> dict:
        return {"title": "Example", "formats": [{"id": "v1", "height": 720}]}

    def download(self, url, kind, format_id, directory, progress, cancelled):
        progress(50)
        if cancelled.is_set():
            raise InterruptedError()
        path = directory / ("file.mp3" if kind == "audio" else "file.mp4")
        path.write_bytes(b"media")
        progress(100)
        return path


def wait_done(service: DownloadService, task_id: str) -> dict:
    for _ in range(100):
        record = service.get(task_id)
        if record["status"] in {"DONE", "FAILED", "CANCELLED"}:
            return record
        time.sleep(0.01)
    raise AssertionError("task did not complete")


def test_submit_persists_and_serves_file(tmp_path: Path) -> None:
    service = DownloadService(tmp_path, engine=FakeEngine())
    try:
        assert service.info("https://example.com/video")["title"] == "Example"
        created = service.submit("https://example.com/video", kind="video", format_id="v1")
        done = wait_done(service, created["task_id"])
        assert done["status"] == "DONE"
        assert done["progress"] == 100
        assert service.media_path(created["task_id"]).read_bytes() == b"media"
    finally:
        service.close()
    restarted = DownloadService(tmp_path, engine=FakeEngine())
    try:
        assert restarted.get(created["task_id"])["status"] == "DONE"
    finally:
        restarted.close()


def test_rejects_private_and_duplicate_batch_urls(tmp_path: Path) -> None:
    service = DownloadService(tmp_path, engine=FakeEngine())
    try:
        with pytest.raises(DownloadError):
            service.info("http://127.0.0.1/internal")
        with pytest.raises(DownloadError):
            service.submit("file:///etc/passwd", kind="video")
        items = service.submit_many([
            "https://example.com/a", "https://example.com/a", "https://example.com/b"
        ], kind="audio")
        assert len(items) == 2
        assert all(wait_done(service, item["task_id"])["status"] == "DONE" for item in items)
    finally:
        service.close()


def test_cancel_and_retry(tmp_path: Path) -> None:
    started = threading.Event()

    class BlockingEngine(FakeEngine):
        def download(self, url, kind, format_id, directory, progress, cancelled):
            started.set()
            while not cancelled.is_set():
                time.sleep(0.01)
            raise InterruptedError()

    service = DownloadService(tmp_path, engine=BlockingEngine())
    try:
        created = service.submit("https://example.com/a", kind="video")
        assert started.wait(1)
        service.cancel(created["task_id"])
        assert wait_done(service, created["task_id"])["status"] == "CANCELLED"
        with pytest.raises(DownloadError):
            service.media_path(created["task_id"])
    finally:
        service.close()
    resumed = DownloadService(tmp_path, engine=FakeEngine())
    try:
        retry = resumed.retry(created["task_id"])
        assert wait_done(resumed, retry["task_id"])["status"] == "DONE"
    finally:
        resumed.close()


def test_download_http_flow(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    app.state.download_service.engine = FakeEngine()
    with TestClient(app) as client:
        info = client.post("/v1/downloads/info", json={"url": "https://example.com/a"})
        assert info.status_code == 200
        assert info.json()["formats"][0]["height"] == 720
        response = client.post("/v1/downloads", json={
            "urls": ["https://example.com/a", "https://example.com/a"],
            "kind": "video", "format_id": "v1",
        })
        assert response.status_code == 201
        assert len(response.json()) == 1
        task_id = response.json()[0]["task_id"]
        assert wait_done(app.state.download_service, task_id)["status"] == "DONE"
        assert client.get(f"/v1/downloads/{task_id}/media").content == b"media"
        assert client.head(f"/v1/downloads/{task_id}/media").status_code == 200
        assert len(client.get("/v1/downloads").json()) == 1
        assert client.post("/v1/downloads", json={
            "urls": ["http://localhost/private"], "kind": "video"
        }).status_code == 400


def test_restart_marks_interrupted_task_retryable(tmp_path: Path) -> None:
    folder = tmp_path / ".downloads" / "download_previous"
    folder.mkdir(parents=True)
    (folder / "task.json").write_text(json.dumps({
        "task_id": "download_previous",
        "url": "https://example.com/a",
        "kind": "video",
        "format_id": None,
        "status": "RUNNING",
        "progress": 47,
        "error": None,
        "filename": None,
        "created_at": "2026-09-29T00:00:00+00:00",
    }), encoding="utf-8")
    service = DownloadService(tmp_path, engine=FakeEngine())
    try:
        assert service.get("download_previous")["status"] == "FAILED"
        retry = service.retry("download_previous")
        assert wait_done(service, retry["task_id"])["status"] == "DONE"
    finally:
        service.close()
