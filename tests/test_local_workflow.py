from io import BytesIO
import json
from pathlib import Path
import time
from typing import Callable
import zipfile

from docx import Document
from fastapi.testclient import TestClient
import pytest

from videogen_service.api import create_app
from videogen_service.config import ServiceConfig
from videogen_service.documents import DocumentError, extract_text
from videogen_service.export import narration_srt
from videogen_service.models import (
    RenderProgress,
    RenderRequest,
    ScriptShot,
)
from videogen_service.scripting import ScriptStudio, storyboard_text


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


class FakeSummarizer:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def summarize(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return json.dumps(
            {
                "title": "双曲空间的秘密",
                "summary": "层级网络在双曲空间里更紧凑。",
                "music": "低音铺底缓慢推进,弦乐拨奏渐强",
                "shots": [
                    {
                        "seconds": 6,
                        "prompt": "网格向外扩张成放射状网络",
                        "sound": "风掠过网格的低鸣",
                        "narration": "双曲空间装得下指数级的分支",
                    }
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


def studio_app(tmp_path: Path) -> tuple[TestClient, FakeSummarizer]:
    config = make_config(tmp_path)
    summarizer = FakeSummarizer()
    studio = ScriptStudio(config, summarizer=summarizer)
    client = TestClient(create_app(config, renderer=FakeRenderer(), studio=studio))
    return client, summarizer


def docx_bytes(*paragraphs: str) -> bytes:
    buffer = BytesIO()
    document = Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    document.save(buffer)
    return buffer.getvalue()


# ---- 文本抽取 ----------------------------------------------------------------


def test_plain_text_and_markdown_are_decoded() -> None:
    assert extract_text("notes.txt", "双曲空间。".encode("utf-8")) == "双曲空间。"
    assert extract_text("notes.md", b"# Title\nbody") == "# Title\nbody"


def test_docx_paragraphs_are_extracted() -> None:
    data = docx_bytes("第一段:双曲几何。", "第二段:层级网络。")
    text = extract_text("research.docx", data)
    assert "第一段:双曲几何。" in text
    assert "第二段:层级网络。" in text


def test_unsupported_suffixes_are_refused() -> None:
    with pytest.raises(DocumentError, match="不支持的文档格式"):
        extract_text("movie.mp4", b"x")


def test_a_broken_pdf_reports_a_clean_error() -> None:
    with pytest.raises(DocumentError, match="PDF"):
        extract_text("broken.pdf", b"not a pdf at all")


# ---- 文档起稿 HTTP -----------------------------------------------------------


def test_a_document_becomes_a_storyboard(tmp_path: Path) -> None:
    client, summarizer = studio_app(tmp_path)
    with client:
        response = client.post(
            "/v1/scripts/document",
            data={"target_seconds": "30", "guidance": "面向初中生"},
            files={
                "file": (
                    "双曲空间研究.txt",
                    "双曲几何常被用于表示层级网络。".encode("utf-8"),
                    "text/plain",
                )
            },
        )
    assert response.status_code == 200
    script = response.json()
    assert script["source"] == {
        "kind": "document",
        "filename": "双曲空间研究.txt",
        "chars": len("双曲几何常被用于表示层级网络。"),
        "truncated": False,
    }
    assert script["mode"] == "story"
    assert script["prompt"].startswith("短片《双曲空间的秘密》")
    # H3 原生出声:配乐进全局 preamble,音效折进分镜段落。
    assert "配乐:低音铺底缓慢推进,弦乐拨奏渐强。" in script["prompt"]
    assert "音效:风掠过网格的低鸣" in script["prompt"]
    prompt = summarizer.prompts[0]
    assert "本地资料文档《双曲空间研究》" in prompt
    assert "面向初中生" in prompt
    assert "文档原文:" in prompt


def test_a_document_script_is_archived_into_its_project(tmp_path: Path) -> None:
    client, _ = studio_app(tmp_path)
    with client:
        client.post("/v1/projects", json={"project_id": "hyp", "name": "双曲空间"})
        response = client.post(
            "/v1/scripts/document",
            data={"project_id": "hyp"},
            files={"file": ("研究.txt", "层级网络。".encode("utf-8"), "text/plain")},
        )
        assert response.status_code == 200

        project = client.get("/v1/projects/hyp").json()
        assert len(project["drafts"]) == 1
        draft = project["drafts"][0]
        assert draft["result"]["source"]["kind"] == "document"
        # A document draft archives bare options — there is no url to record.
        assert "url" not in draft["request"]


def test_an_empty_document_is_a_client_error(tmp_path: Path) -> None:
    client, _ = studio_app(tmp_path)
    with client:
        response = client.post(
            "/v1/scripts/document",
            files={"file": ("blank.txt", b"   \n  ", "text/plain")},
        )
    assert response.status_code == 400


# ---- 导出打包 ----------------------------------------------------------------


def test_storyboard_text_carries_continuation_markers() -> None:
    shots = [
        ScriptShot(seconds=6, prompt="女子起跳", narration=""),
        ScriptShot(seconds=6, prompt="她落地", narration="", continues=True),
    ]
    text = storyboard_text(shots, preamble="风格")
    assert "[0s-6s] 女子起跳" in text
    assert "[6s-12s续] 她落地" in text


def test_narration_srt_uses_the_padded_timeline() -> None:
    shots = [
        ScriptShot(seconds=6, prompt="镜头一", narration="第一句"),
        ScriptShot(seconds=6, prompt="镜头二", narration=""),
        ScriptShot(seconds=6, prompt="镜头三", narration="第三句"),
    ]
    srt = narration_srt(shots, fps=24)
    # 6s aligns up to 158 frames = 6.583s on the 17k+5 grid.
    assert "1\n00:00:00,000 --> 00:00:06,583\n第一句" in srt
    # The silent shot keeps its slot on the clock but emits no cue.
    assert "2\n00:00:13,167 --> 00:00:19,750\n第三句" in srt


def test_a_project_exports_mp4_srt_and_manifest(tmp_path: Path) -> None:
    client, _ = studio_app(tmp_path)
    with client:
        client.post("/v1/projects", json={"project_id": "hyp", "name": "双曲空间"})
        client.post(
            "/v1/scripts/document",
            data={"project_id": "hyp"},
            files={"file": ("研究.txt", "层级网络。".encode("utf-8"), "text/plain")},
        )
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_export_1",
                "mode": "story",
                "prompt": "[0s-6s] 网格向外扩张",
                "width": "864",
                "height": "480",
                "seconds": "6",
            },
        )
        assert submitted.status_code == 202
        deadline = time.monotonic() + 3
        while (
            client.get("/v1/renders/vg_export_1").json()["status"] != "DONE"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        client.post("/v1/projects/hyp/renders", json={"render_id": "vg_export_1"})

        exported = client.get("/v1/projects/hyp/export")
        assert exported.status_code == 200
        assert exported.headers["content-type"] == "application/zip"

        archive = zipfile.ZipFile(BytesIO(exported.content))
        names = set(archive.namelist())
        assert "project.json" in names
        assert "drafts/draft-01/storyboard.txt" in names
        assert "drafts/draft-01/narration.srt" in names
        assert "drafts/draft-01/narration.txt" in names
        assert "renders/vg_export_1.mp4" in names
        assert archive.read("renders/vg_export_1.mp4") == b"FAKE-MP4"

        manifest = json.loads(archive.read("project.json"))
        assert manifest["name"] == "双曲空间"
        assert manifest["renders"] == ["vg_export_1"]
        assert manifest["skipped_renders"] == []


def test_unfinished_renders_are_skipped_not_fatal(tmp_path: Path) -> None:
    client, _ = studio_app(tmp_path)
    with client:
        client.post("/v1/projects", json={"project_id": "hyp", "name": "双曲空间"})
        # Linked id whose render was deleted afterwards.
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "vg_gone",
                "mode": "story",
                "prompt": "[0s-6s] 网格",
                "width": "864",
                "height": "480",
                "seconds": "6",
            },
        )
        assert submitted.status_code == 202
        deadline = time.monotonic() + 3
        while (
            client.get("/v1/renders/vg_gone").json()["status"] != "DONE"
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        client.post("/v1/projects/hyp/renders", json={"render_id": "vg_gone"})
        assert client.delete("/v1/renders/vg_gone").status_code == 204

        exported = client.get("/v1/projects/hyp/export")
        assert exported.status_code == 200
        manifest = json.loads(
            zipfile.ZipFile(BytesIO(exported.content)).read("project.json")
        )
        assert manifest["skipped_renders"] == ["vg_gone"]
