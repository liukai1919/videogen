import json
from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient
import pytest

from videogen_service.api import create_app
from videogen_service.config import ServiceConfig
from videogen_service.memory import MemoryNotFound, MemoryStore
from videogen_service.models import RenderProgress, RenderRequest, ScriptRequest
from videogen_service.scripting import ScriptStudio
from videogen_service.transcript import Transcript, canonical_youtube_url


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
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def summarize(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "title": "蓝天为什么是蓝的",
                "summary": "短波长的蓝光被散射得最多。",
                "shots": [
                    {"seconds": 6, "prompt": "航拍晴朗天空", "narration": "抬头就是蓝色"}
                ],
            },
            ensure_ascii=False,
        )


class FakeRenderer:
    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
    ) -> bytes:
        return b"FAKE-MP4"


# ---- MemoryStore -------------------------------------------------------------


def test_entries_are_added_listed_and_removed(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "work")
    entry = store.add("旁白都用轻松的口语")
    assert entry.entry_id.startswith("mem_")
    assert [item.text for item in store.list()] == ["旁白都用轻松的口语"]

    # Survives a fresh store over the same file.
    again = MemoryStore(tmp_path / "work")
    assert [item.text for item in again.list()] == ["旁白都用轻松的口语"]

    again.remove(entry.entry_id)
    assert again.list() == []
    with pytest.raises(MemoryNotFound):
        again.remove(entry.entry_id)


def test_injection_takes_only_the_newest_entries(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path / "work")
    for index in range(25):
        store.add(f"偏好 {index}")
    texts = store.texts()
    assert len(texts) == 20
    assert texts[0] == "偏好 5"  # oldest injected
    assert texts[-1] == "偏好 24"  # newest last


# ---- 注入总结 Prompt ----------------------------------------------------------


def test_saved_preferences_reach_the_prompt(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    store = MemoryStore(config.work_dir)
    store.add("旁白都用轻松的口语,别用书面腔")
    summarizer = FakeSummarizer()
    studio = ScriptStudio(
        config, fetcher=FakeFetcher(), summarizer=summarizer, memory=store
    )

    studio.create(ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ"))

    prompt = summarizer.prompts[0]
    assert "长期偏好" in prompt
    assert "- 旁白都用轻松的口语,别用书面腔" in prompt


def test_no_preferences_means_no_memory_section(tmp_path: Path) -> None:
    summarizer = FakeSummarizer()
    studio = ScriptStudio(
        make_config(tmp_path), fetcher=FakeFetcher(), summarizer=summarizer
    )
    studio.create(ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ"))
    assert "长期偏好" not in summarizer.prompts[0]


# ---- HTTP seam ---------------------------------------------------------------


def test_memory_crud_over_http(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    with TestClient(app) as client:
        created = client.post(
            "/v1/memory", json={"text": "画面色调偏冷,少用暖色"}
        )
        assert created.status_code == 201
        entry_id = created.json()["entry_id"]

        listed = client.get("/v1/memory")
        assert [item["text"] for item in listed.json()] == ["画面色调偏冷,少用暖色"]

        assert client.delete(f"/v1/memory/{entry_id}").status_code == 204
        assert client.delete(f"/v1/memory/{entry_id}").status_code == 404
        assert client.get("/v1/memory").json() == []
