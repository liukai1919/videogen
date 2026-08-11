import json
from pathlib import Path

import pytest

from videogen_service.config import ServiceConfig, load_config
from videogen_service.models import ScriptRequest
from videogen_service.renderer import parse_storyboard, validate_storyboard
from videogen_service.scripting import ScriptError, ScriptStudio, ScriptUnavailable
from videogen_service.transcript import (
    Transcript,
    TranscriptError,
    TranscriptUnavailable,
    canonical_youtube_url,
    parse_json3,
    parse_vtt,
    select_caption_track,
)


def project_config() -> ServiceConfig:
    return load_config(Path(__file__).parents[1] / "config.example.yaml")


def transcript(**overrides: object) -> Transcript:
    values: dict[str, object] = {
        "video_id": "dQw4w9WgXcQ",
        "title": "Why the sky is blue",
        "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "language": "en",
        "automatic": True,
        "duration_seconds": 640.0,
        "text": "Sunlight scatters off air molecules. " * 50,
    }
    values.update(overrides)
    return Transcript(**values)  # type: ignore[arg-type]


class FakeFetcher:
    def __init__(self, result: Transcript | Exception) -> None:
        self.result = result
        self.urls: list[str] = []

    def fetch(self, url: str) -> Transcript:
        self.urls.append(url)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeSummarizer:
    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    def summarize(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


def draft(*shots: tuple[float, str]) -> str:
    return json.dumps(
        {
            "title": "蓝天为什么是蓝的",
            "summary": "阳光被空气分子散射,短波长的蓝光散得最多。",
            "shots": [
                {"seconds": seconds, "prompt": prompt, "narration": "解说"}
                for seconds, prompt in shots
            ],
        },
        ensure_ascii=False,
    )


def studio(response: str, *, source: Transcript | Exception | None = None) -> ScriptStudio:
    return ScriptStudio(
        project_config(),
        fetcher=FakeFetcher(source if source is not None else transcript()),
        summarizer=FakeSummarizer(response),
    )


def test_youtube_urls_are_reduced_to_a_watch_url() -> None:
    for url in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=30s",
        "https://youtu.be/dQw4w9WgXcQ?si=abc",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
        "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
    ):
        assert canonical_youtube_url(url) == (
            "dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://vimeo.com/12345",
        "https://www.youtube.com/",
        "file:///etc/passwd",
        "https://www.youtube.com/watch?v=short",
    ],
)
def test_non_youtube_links_are_refused_before_any_network_call(url: str) -> None:
    with pytest.raises(TranscriptError):
        canonical_youtube_url(url)


def test_generated_script_is_a_storyboard_the_renderer_accepts() -> None:
    config = project_config()
    result = studio(draft((6, "航拍晴朗天空"), (6, "阳光穿过大气层的示意"))).create(
        ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ")
    )

    # 12 秒在单条包络内 → 走 t2v 单发(散文时间轴,一次采样保镜头连贯,
    # A/B 2026-08-11),秒数按原始值请求而不是补齐帧网格。
    assert result.mode == "t2v"
    assert result.source.video_id == "dQw4w9WgXcQ"
    assert result.title == "蓝天为什么是蓝的"
    assert [shot.prompt for shot in result.shots] == [
        "航拍晴朗天空",
        "阳光穿过大气层的示意",
    ]
    assert "[0s-6s] 航拍晴朗天空" in result.prompt
    assert "[6s-12s] 阳光穿过大气层的示意" in result.prompt
    board = parse_storyboard(result.prompt, default_seconds=6)
    validate_storyboard(board, config=config)
    assert len(board.shots) == 2
    assert result.seconds == pytest.approx(12.0)


def test_long_scripts_still_route_to_story_chunking() -> None:
    # 4×6s=24s 超单条包络 → story 模式,秒数用补齐后的分镜总长。
    result = studio(
        draft(
            (6, "航拍晴朗天空"),
            (6, "阳光穿过大气层的示意"),
            (6, "蓝光被散射的粒子特写"),
            (6, "黄昏天空转红的对比"),
        )
    ).create(ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ"))

    assert result.mode == "story"
    assert result.seconds == pytest.approx(4 * 158 / 24)


def test_the_transcript_reaches_the_summariser_with_the_video_title() -> None:
    summarizer = FakeSummarizer(draft((6, "航拍晴朗天空")))
    ScriptStudio(
        project_config(), fetcher=FakeFetcher(transcript()), summarizer=summarizer
    ).create(ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ", guidance="面向初中生"))

    prompt = summarizer.prompts[0]
    assert "Why the sky is blue" in prompt
    assert "Sunlight scatters off air molecules." in prompt
    assert "面向初中生" in prompt


def test_shot_durations_are_clamped_onto_the_renderer_limits() -> None:
    config = project_config()
    result = studio(draft((0.5, "太短"), (99, "太长"))).create(
        ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ")
    )

    assert [shot.seconds for shot in result.shots] == [
        config.renderer.min_seconds,
        config.renderer.max_seconds,
    ]


def test_the_shot_list_is_cut_to_the_requested_budget() -> None:
    result = studio(draft(*[(6, f"第{index}镜") for index in range(12)])).create(
        ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ", target_seconds=20, max_shots=6)
    )

    assert len(result.shots) == 3
    assert result.seconds <= 20


def test_the_final_shot_survives_quantization_overshoot() -> None:
    # 目标 30、六秒镜头:H3 量化把每镜垫到 6.583s,5 镜共 32.9s。旧裁剪
    # 一律砍到 4 镜(26.3s)——R2 记档的"片长偏短八成"病根;就近适配应
    # 保留第 5 镜,超 2.9s 比缺 3.7s 更接近目标。
    result = studio(draft(*[(6, f"第{index}镜") for index in range(5)])).create(
        ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ", target_seconds=30)
    )

    assert len(result.shots) == 5
    assert result.seconds > 30


def test_closest_fit_never_exceeds_the_hard_cap() -> None:
    # 目标顶到 120s 硬上限:第 8 个 15s 镜头哪怕更接近目标(超 0.7s vs
    # 缺 14.4s)也不许越界——上限是 OOM 红线,不是软预算。
    config = project_config()
    result = studio(draft(*[(15, f"第{index}镜") for index in range(9)])).create(
        ScriptRequest(
            url="https://youtu.be/dQw4w9WgXcQ", target_seconds=120, max_shots=12
        )
    )

    assert result.seconds <= config.renderer.max_total_seconds


def test_a_long_transcript_is_truncated_instead_of_flooding_the_model() -> None:
    result = studio(
        draft((6, "航拍晴朗天空")), source=transcript(text="字" * 40_000)
    ).create(ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ"))

    assert result.source.transcript_truncated
    assert result.source.transcript_chars <= 24_000 + 40


def test_a_model_that_returns_prose_is_a_client_error_not_a_crash() -> None:
    with pytest.raises(ScriptError):
        studio("抱歉,我无法完成").create(
            ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ")
        )


def test_a_model_answer_wrapped_in_a_code_fence_still_parses() -> None:
    fenced = f"```json\n{draft((6, '航拍晴朗天空'))}\n```"
    result = studio(fenced).create(ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ"))

    assert len(result.shots) == 1


def test_a_video_without_captions_reports_the_upstream_as_unavailable() -> None:
    with pytest.raises(ScriptUnavailable):
        studio(
            draft((6, "航拍晴朗天空")),
            source=TranscriptUnavailable("这个视频没有可用字幕"),
        ).create(ScriptRequest(url="https://youtu.be/dQw4w9WgXcQ"))


def test_human_subtitles_win_over_auto_captions() -> None:
    info = {
        "subtitles": {"en": [{"ext": "vtt", "url": "https://example/en.vtt"}]},
        "automatic_captions": {
            "zh-Hans": [{"ext": "json3", "url": "https://example/zh.json3"}]
        },
    }

    track = select_caption_track(info, ["zh-Hans", "en"])

    assert track == ("en", False, "vtt", "https://example/en.vtt")


def test_auto_captions_are_used_when_nobody_wrote_subtitles() -> None:
    info = {
        "subtitles": {},
        "automatic_captions": {
            "en-US": [
                {"ext": "srt", "url": "https://example/en.srt"},
                {"ext": "json3", "url": "https://example/en.json3"},
            ]
        },
    }

    track = select_caption_track(info, ["en"])

    assert track == ("en-US", True, "json3", "https://example/en.json3")


def test_a_video_with_no_caption_track_at_all_is_unavailable() -> None:
    with pytest.raises(TranscriptUnavailable):
        select_caption_track({"subtitles": {}, "automatic_captions": {}}, ["zh"])


def test_rolling_auto_captions_are_de_duplicated() -> None:
    vtt = """WEBVTT
Kind: captions
Language: en

00:00:01.000 --> 00:00:03.000
sunlight <c>scatters</c>

00:00:03.000 --> 00:00:05.000
sunlight scatters
off air molecules

00:00:05.000 --> 00:00:07.000
off air molecules
and the sky looks blue
"""

    assert parse_vtt(vtt) == "sunlight scatters off air molecules and the sky looks blue"


def test_json3_appended_events_are_dropped() -> None:
    payload = json.dumps(
        {
            "events": [
                {"segs": [{"utf8": "阳光"}, {"utf8": "散射"}]},
                {"aAppend": 1, "segs": [{"utf8": "阳光散射"}]},
                {"segs": [{"utf8": "\n"}]},
                {"segs": [{"utf8": "天空变蓝"}]},
            ]
        }
    )

    assert parse_json3(payload) == "阳光散射 天空变蓝"


def _fake_ollama_response(payload: dict) -> object:
    class _Response:
        def read(self) -> bytes:
            return json.dumps(payload, ensure_ascii=False).encode("utf-8")

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    return _Response()


def _summarizer():
    # OllamaSummarizer 只读这几个属性,鸭子类型即可,不用搭全量配置。
    from types import SimpleNamespace

    from videogen_service.scripting import OllamaSummarizer

    config = SimpleNamespace(
        ollama=SimpleNamespace(base_url="http://127.0.0.1:11434", model="qwen"),
        script=SimpleNamespace(model=None, temperature=0.4, timeout_seconds=5),
    )
    return OllamaSummarizer(config)  # type: ignore[arg-type]


def test_empty_response_falls_back_to_thinking(monkeypatch) -> None:
    # 思考型模型 + format=json:整段 JSON 落在 thinking,response 为空
    # (R1 评测 11/11 实锤);summarize 必须捞回 thinking 里的正文。
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _fake_ollama_response(
            {"response": "", "thinking": '{"ok": true}'}
        ),
    )
    assert _summarizer().summarize("提示词") == '{"ok": true}'


def test_truly_empty_answer_still_raises(monkeypatch) -> None:
    from videogen_service.scripting import ScriptUnavailable

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda request, timeout: _fake_ollama_response(
            {"response": "", "thinking": ""}
        ),
    )
    try:
        _summarizer().summarize("提示词")
    except ScriptUnavailable as error:
        assert "没有返回内容" in str(error)
    else:
        raise AssertionError("空响应应该抛 ScriptUnavailable")
