from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import threading
from typing import Any

import pytest

from videogen_service.config import DirectorConfig, RendererConfig, RenderMode
from videogen_service.director import (
    AnthropicDirector,
    DirectorError,
    OllamaDirector,
    PromptDirector,
    check_prompt,
)
from videogen_service.models import EnhanceRequest, H3Prompt

BODY = (
    "[Shot 1] Live-action, cinematic, a baker opens the shutters. "
    "The baker (S1) says: <d>[English] First batch of the morning.</d> "
    "[Shot 2] At 00:05.000, the camera cuts to a close-up of the sliced bread."
)
IDEA = '面包师傅开门,说 "First batch of the morning.",然后特写切面包'


def prompt(**overrides: str) -> H3Prompt:
    values: dict[str, str] = {
        "integrated_multimodal_description": BODY,
        "overall_soundscape": "Wooden shutters scrape open over a quiet street.",
        "non_diegetic_music": "A soft acoustic-guitar pattern at a moderate tempo.",
    }
    values.update(overrides)
    return H3Prompt.model_validate(values)


def check(written: H3Prompt, *, source: str = IDEA, seconds: float = 10.125) -> list[str]:
    return check_prompt(
        written, source=source, seconds=seconds, require_verbatim_dialogue=True
    )


class FakeProvider:
    """Hands back canned prompts, one per attempt, and records the feedback."""

    def __init__(self, *written: H3Prompt | DirectorError) -> None:
        self.written = list(written)
        self.calls: list[Sequence[str]] = []
        self.seconds: list[float] = []

    def write(
        self,
        *,
        idea: str,
        seconds: float,
        mode: RenderMode,
        feedback: Sequence[str],
    ) -> H3Prompt:
        self.calls.append(list(feedback))
        self.seconds.append(seconds)
        result = self.written[min(len(self.calls) - 1, len(self.written) - 1)]
        if isinstance(result, DirectorError):
            raise result
        return result


def director(provider: FakeProvider, tmp_path: Path, **overrides: object) -> PromptDirector:
    values: dict[str, object] = {
        "model": "test-model",
        "guide_file": tmp_path / "guide.md",
    }
    values.update(overrides)
    return PromptDirector(
        provider,
        settings=DirectorConfig.model_validate(values),
        renderer=RendererConfig(
            fps=24, min_seconds=5, max_seconds=15, max_total_seconds=120, max_shots=12
        ),
        cache_dir=tmp_path / "prompts",
    )


def test_a_well_formed_prompt_raises_nothing() -> None:
    assert check(prompt()) == []


def test_cut_times_must_climb_and_stay_inside_the_clip() -> None:
    late = BODY.replace("At 00:05.000", "At 00:12.000")
    backwards = BODY.replace("[Shot 1] Live-action", "[Shot 1] At 00:09.000 Live-action")

    assert "[Shot 2] 的 At 00:12.000 落在" in " ".join(
        check(prompt(integrated_multimodal_description=late))
    )
    assert "[Shot 1] 不带切换时间" in " ".join(
        check(prompt(integrated_multimodal_description=backwards))
    )


def test_a_later_shot_without_a_cut_time_is_caught() -> None:
    missing = BODY.replace("At 00:05.000, ", "")

    assert "[Shot 2] 缺少 At MM:SS.mmm 切换时间" in check(
        prompt(integrated_multimodal_description=missing)
    )


def test_shot_numbers_must_run_from_one() -> None:
    skipped = BODY.replace("[Shot 2]", "[Shot 3]")

    assert "镜头编号要从 1 起连续递增" in " ".join(
        check(prompt(integrated_multimodal_description=skipped))
    )


def test_a_body_without_any_shot_marker_is_caught() -> None:
    assert check(prompt(integrated_multimodal_description="a baker opens a door")) == [
        "画面描述要以 [Shot 1] 开头"
    ]


def test_translated_dialogue_is_caught() -> None:
    # The single failure H3 punishes hardest: it speaks <d> verbatim, so a
    # translated line is a wrong take no re-render can fix.
    translated = BODY.replace(
        "First batch of the morning.", "今天的第一炉面包出炉了。"
    )

    assert "台词和原文对不上" in " ".join(
        check(prompt(integrated_multimodal_description=translated))
    )


def test_dialogue_matching_ignores_only_whitespace() -> None:
    spaced = BODY.replace(
        "<d>[English] First batch of the morning.</d>",
        "<d>[English]   First batch of the morning.  </d>",
    )

    assert check(prompt(integrated_multimodal_description=spaced)) == []


def test_dialogue_checking_can_be_switched_off() -> None:
    invented = BODY.replace("First batch of the morning.", "Something I made up.")

    assert check_prompt(
        prompt(integrated_multimodal_description=invented),
        source=IDEA,
        seconds=10.125,
        require_verbatim_dialogue=False,
    ) == []


def test_dialogue_does_not_belong_in_the_soundscape() -> None:
    assert "overall_soundscape 只放环境音" in " ".join(
        check(prompt(overall_soundscape="A voice says <d>[English] Hello.</d>"))
    )


def test_mood_words_in_the_score_field_are_caught() -> None:
    warnings = check(
        prompt(non_diegetic_music="A sad, hopeful piano line at a slow tempo.")
    )

    assert "hopeful, sad" in " ".join(warnings)


def test_the_audio_fields_accept_n_a() -> None:
    assert check(prompt(overall_soundscape="N/A", non_diegetic_music="N/A")) == []


def test_a_failed_check_is_fed_back_and_the_retry_is_kept(tmp_path: Path) -> None:
    broken = prompt(non_diegetic_music="An epic swell of strings.")
    provider = FakeProvider(broken, prompt())

    response = director(provider, tmp_path).enhance(
        EnhanceRequest(mode="t2v", prompt=IDEA, seconds=10.0)
    )

    assert response.enhanced is True
    assert response.warnings == []
    assert provider.calls[0] == []
    assert "non_diegetic_music" in provider.calls[1][0]


def test_the_least_broken_attempt_survives_when_neither_is_clean(tmp_path: Path) -> None:
    worse = prompt(
        integrated_multimodal_description=BODY.replace("At 00:05.000", "At 00:12.000"),
        non_diegetic_music="An epic swell of strings.",
    )
    better = prompt(non_diegetic_music="An epic swell of strings.")
    provider = FakeProvider(worse, better)

    response = director(provider, tmp_path).enhance(
        EnhanceRequest(mode="t2v", prompt=IDEA, seconds=10.0)
    )

    assert len(response.warnings) == 1
    assert response.fields == better


def test_a_provider_outage_returns_the_original_prompt(tmp_path: Path) -> None:
    provider = FakeProvider(DirectorError("连接失败"))

    response = director(provider, tmp_path).enhance(
        EnhanceRequest(mode="t2v", prompt=IDEA, seconds=10.0)
    )

    assert response.enhanced is False
    assert response.prompt == IDEA
    assert response.fields is None
    assert "已退回原文" in response.warnings[0]


def test_the_reported_duration_is_the_aligned_one(tmp_path: Path) -> None:
    provider = FakeProvider(prompt())

    response = director(provider, tmp_path).enhance(
        EnhanceRequest(mode="t2v", prompt=IDEA, seconds=10.0)
    )

    # 10s asks for 240 frames, the grid delivers 243. The writer is told that
    # number and the cut times are checked against it, not against the 10 the
    # caller asked for — a timestamp is only correct against the real duration.
    assert response.seconds == pytest.approx(243 / 24)
    assert provider.seconds == [pytest.approx(243 / 24)]


def test_an_identical_request_is_served_from_cache(tmp_path: Path) -> None:
    provider = FakeProvider(prompt())
    writer = director(provider, tmp_path)
    request = EnhanceRequest(mode="t2v", prompt=IDEA, seconds=10.0)

    first = writer.enhance(request)
    second = writer.enhance(request)

    assert provider.calls == [[]]
    assert second == first


def test_a_degraded_response_is_not_cached(tmp_path: Path) -> None:
    provider = FakeProvider(DirectorError("连接失败"), prompt())
    writer = director(provider, tmp_path)
    request = EnhanceRequest(mode="t2v", prompt=IDEA, seconds=10.0)

    assert writer.enhance(request).enhanced is False
    assert writer.enhance(request).enhanced is True


# The two providers below are the only code here that touches a socket, so they
# are exercised against a real one rather than a stub.

FIELDS = {
    "integrated_multimodal_description": "[Shot 1] Cinematic, a valley in fog.",
    "overall_soundscape": "Wind through wet grass.",
    "non_diegetic_music": "N/A",
}


@contextmanager
def serving(reply: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    seen: dict[str, Any] = {}
    body = json.dumps(reply).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def test_the_ollama_provider_asks_for_the_schema_and_parses_the_reply(
    tmp_path: Path,
) -> None:
    reply = {"message": {"content": json.dumps(FIELDS)}}
    with serving(reply) as (base_url, seen):
        settings = DirectorConfig(
            provider="ollama",
            model="qwen3.6:27b",
            guide_file=tmp_path / "guide.md",
            base_url=base_url,
        )
        written = OllamaDirector(settings, guide="GUIDE").write(
            idea=IDEA, seconds=10.125, mode="t2v", feedback=["修掉情绪词"]
        )

    assert seen["path"] == "/api/chat"
    assert seen["body"]["stream"] is False
    assert sorted(seen["body"]["format"]["required"]) == sorted(FIELDS)
    assert seen["body"]["messages"][0] == {"role": "system", "content": "GUIDE"}
    assert "修掉情绪词" in seen["body"]["messages"][1]["content"]
    assert written.non_diegetic_music == "N/A"


def test_the_ollama_provider_rejects_a_reply_that_is_not_the_three_fields(
    tmp_path: Path,
) -> None:
    with serving({"message": {"content": '{"vibes": "good"}'}}) as (base_url, _):
        settings = DirectorConfig(
            provider="ollama",
            model="qwen3.6:27b",
            guide_file=tmp_path / "guide.md",
            base_url=base_url,
        )
        with pytest.raises(DirectorError, match="没有按三字段结构返回"):
            OllamaDirector(settings, guide="GUIDE").write(
                idea=IDEA, seconds=10.125, mode="t2v", feedback=[]
            )


def test_the_claude_provider_caches_the_guide_and_pins_the_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("anthropic")
    reply = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": json.dumps(FIELDS)}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    with serving(reply) as (base_url, seen):
        monkeypatch.setenv("ANTHROPIC_BASE_URL", base_url)
        monkeypatch.setenv("VIDEOGEN_TEST_KEY", "sk-ant-test")
        settings = DirectorConfig(
            model="claude-opus-5",
            guide_file=tmp_path / "guide.md",
            api_key_env="VIDEOGEN_TEST_KEY",
        )
        written = AnthropicDirector(settings, guide="GUIDE").write(
            idea=IDEA, seconds=10.125, mode="t2v", feedback=[]
        )

    assert seen["path"] == "/v1/messages"
    # The guide is the same prefix on every rewrite; without this the repeat
    # cost of the director is ~10x what it needs to be.
    assert seen["body"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    schema = seen["body"]["output_config"]["format"]
    assert schema["type"] == "json_schema"
    assert schema["schema"]["additionalProperties"] is False
    assert written.overall_soundscape == "Wind through wet grass."
