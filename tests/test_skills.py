import json
from pathlib import Path
from typing import Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.config import ServiceConfig
from videogen_service.models import RenderProgress, RenderRequest, ScriptRequest
from videogen_service.scripting import ScriptError, ScriptStudio
from videogen_service.skills import SkillLibrary
from videogen_service.transcript import Transcript, canonical_youtube_url
import pytest


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


DEFAULT_META = "\n".join(
    [
        "name: 科普纪录片",
        "description: 测试用规范",
        "category: 纪录片",
        "defaults:",
        "  style: 手绘水彩插画风",
        "  shot_seconds: 7",
    ]
)


def write_skill(
    skills_dir: Path,
    skill_id: str = "science-doc",
    *,
    meta: str = DEFAULT_META,
    instructions: str = "- 每个分镜只讲一个概念\n- 画面不出现文字",
) -> None:
    path = skills_dir / skill_id
    path.mkdir(parents=True)
    (path / "meta.yaml").write_text(meta, encoding="utf-8")
    (path / "SKILL.md").write_text(instructions, encoding="utf-8")


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


def make_studio(tmp_path: Path, summarizer: FakeSummarizer) -> ScriptStudio:
    config = make_config(tmp_path)
    return ScriptStudio(
        config,
        fetcher=FakeFetcher(),
        summarizer=summarizer,
        skills=SkillLibrary(config.skills_dir),
    )


# ---- SkillLibrary -----------------------------------------------------------


def test_library_lists_and_loads_a_skill(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills")
    library = SkillLibrary(tmp_path / "skills")

    skills = library.list()
    assert [skill.skill_id for skill in skills] == ["science-doc"]

    skill = library.get("science-doc")
    assert skill is not None
    assert skill.meta.name == "科普纪录片"
    assert skill.meta.defaults.style == "手绘水彩插画风"
    assert skill.meta.defaults.shot_seconds == 7
    assert "画面不出现文字" in skill.instructions


def test_library_survives_a_missing_directory(tmp_path: Path) -> None:
    library = SkillLibrary(tmp_path / "nowhere")
    assert library.list() == []
    assert library.get("anything") is None


def test_a_broken_skill_disables_only_itself(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "good")
    write_skill(skills_dir, "bad-meta", meta="name: [未闭合")
    write_skill(skills_dir, "extra-keys", meta="name: x\nunknown_field: 1")
    incomplete = skills_dir / "no-skill-md"
    incomplete.mkdir()
    (incomplete / "meta.yaml").write_text("name: 缺说明书", encoding="utf-8")

    library = SkillLibrary(skills_dir)
    assert [skill.skill_id for skill in library.list()] == ["good"]
    assert library.get("bad-meta") is None
    assert library.get("no-skill-md") is None


def test_get_refuses_path_shaped_ids(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills")
    library = SkillLibrary(tmp_path / "skills")
    assert library.get("../skills/science-doc") is None


# ---- ScriptStudio 套用 Skill -------------------------------------------------


def test_skill_defaults_and_instructions_reach_the_prompt(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills")
    summarizer = FakeSummarizer()
    studio = make_studio(tmp_path, summarizer)

    result = studio.create(
        ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ", skill="science-doc")
    )

    prompt = summarizer.prompts[0]
    assert "创作规范(来自 Skill「科普纪录片」" in prompt
    assert "每个分镜只讲一个概念" in prompt
    assert "建议 7" in prompt  # shot_seconds 来自 Skill 默认值
    assert "手绘水彩插画风" in result.prompt  # style 进了分镜的统一风格开场


def test_explicit_request_fields_beat_skill_defaults(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills")
    summarizer = FakeSummarizer()
    studio = make_studio(tmp_path, summarizer)

    result = studio.create(
        ScriptRequest(
            url="https://youtu.be/dQw4w9WgXcQ",
            skill="science-doc",
            style="赛博朋克霓虹",
            shot_seconds=9,
        )
    )

    assert "赛博朋克霓虹" in result.prompt
    assert "手绘水彩插画风" not in result.prompt
    assert "建议 9" in summarizer.prompts[0]


def test_an_unknown_skill_fails_before_fetching(tmp_path: Path) -> None:
    studio = make_studio(tmp_path, FakeSummarizer())
    with pytest.raises(ScriptError, match="不存在"):
        studio.create(
            ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ", skill="missing")
        )


# ---- HTTP seam ---------------------------------------------------------------


def test_the_skill_list_is_served(tmp_path: Path) -> None:
    write_skill(tmp_path / "skills")
    app = create_app(make_config(tmp_path), renderer=FakeRenderer())
    with TestClient(app) as client:
        listed = client.get("/v1/skills")

    assert listed.status_code == 200
    assert listed.json() == [
        {
            "skill_id": "science-doc",
            "name": "科普纪录片",
            "description": "测试用规范",
            "category": "纪录片",
            "defaults": {"style": "手绘水彩插画风", "shot_seconds": 7.0},
        }
    ]
