"""Rewrites a plain-language idea into MiniMax H3's three-field prompt format.

H3's hosted API does this with a closed-source module (Context-IR); locally we
ask an LLM and check the result ourselves. The checks matter as much as the
model, because the failure modes are specific, cheap to detect, and expensive to
miss: rewritten dialogue breaks lip sync, a cut timed past the end of the clip
silently loses a shot, and mood words in the score field make H3 score the
adjective instead of the scene.

Enhancement is deliberately a separate step from rendering. The control plane
submits whichever prompt it settles on, so a render stays reproducible from its
recorded spec alone and a director outage can never block the GPU.
"""

from collections.abc import Sequence
import hashlib
import json
import logging
from pathlib import Path
import re
from typing import Any, Protocol
from urllib.error import URLError
import urllib.request

from videogen_service.artifacts import write_json_atomic
from videogen_service.config import DirectorConfig, RendererConfig, RenderMode
from videogen_service.models import EnhanceRequest, EnhanceResponse, H3Prompt
from videogen_service.renderer import aligned_seconds

SHOT_MARKER = re.compile(r"\[Shot\s+(\d+)\]")
SHOT_TIME = re.compile(r"At\s+(\d{1,2}):(\d{2}(?:\.\d{1,3})?)")
DIALOGUE = re.compile(r"<d>(.*?)</d>", re.DOTALL)
LANGUAGE_TAG = re.compile(r"^\s*\[[^\]]+\]\s*")
WORD = re.compile(r"[a-z]+")
NOT_APPLICABLE = frozenset({"", "N/A", "NA", "NONE"})

# non_diegetic_music takes instruments, tempo, rhythm and dynamics. Naming the
# feeling makes H3 score the adjective rather than the scene, so these are the
# words the official guide tells you to keep out of that field.
MOOD_WORDS = frozenset(
    {
        "cheerful", "dramatic", "dreamy", "eerie", "emotional", "epic", "gloomy",
        "happy", "haunting", "heroic", "hopeful", "joyful", "melancholic",
        "melancholy", "menacing", "mysterious", "nostalgic", "ominous",
        "playful", "poignant", "romantic", "sad", "somber", "suspenseful",
        "tense", "triumphant", "uplifting", "whimsical", "wistful",
    }
)


class DirectorError(RuntimeError):
    pass


class Director(Protocol):
    def write(
        self,
        *,
        idea: str,
        seconds: float,
        mode: RenderMode,
        feedback: Sequence[str],
    ) -> H3Prompt: ...


def check_prompt(
    prompt: H3Prompt,
    *,
    source: str,
    seconds: float,
    require_verbatim_dialogue: bool,
) -> list[str]:
    """Return every rule the rewritten prompt breaks, in reading order."""
    warnings = _check_shots(prompt.integrated_multimodal_description, seconds=seconds)
    if require_verbatim_dialogue:
        warnings += _check_dialogue(
            prompt.integrated_multimodal_description, source=source
        )
    warnings += _check_soundscape(prompt.overall_soundscape)
    warnings += _check_score(prompt.non_diegetic_music)
    return warnings


def _check_shots(body: str, *, seconds: float) -> list[str]:
    markers = list(SHOT_MARKER.finditer(body))
    if not markers:
        return ["画面描述要以 [Shot 1] 开头"]
    warnings: list[str] = []
    previous = 0.0
    for position, marker in enumerate(markers, start=1):
        if int(marker.group(1)) != position:
            warnings.append(
                f"镜头编号要从 1 起连续递增,第 {position} 个写的是 {marker.group(0)}"
            )
        end = markers[position].start() if position < len(markers) else len(body)
        stamp = SHOT_TIME.search(body[marker.end() : end])
        if position == 1:
            if stamp is not None:
                warnings.append("[Shot 1] 不带切换时间,时间戳从 [Shot 2] 开始")
            continue
        if stamp is None:
            warnings.append(f"[Shot {position}] 缺少 At MM:SS.mmm 切换时间")
            continue
        at = int(stamp.group(1)) * 60 + float(stamp.group(2))
        if at <= previous:
            warnings.append(f"[Shot {position}] 的 {stamp.group(0)} 没有比上一镜更晚")
        if at >= seconds:
            warnings.append(
                f"[Shot {position}] 的 {stamp.group(0)} 落在 {seconds:.2f} 秒的成片之外"
            )
        previous = at
    return warnings


def _check_dialogue(body: str, *, source: str) -> list[str]:
    # H3 speaks whatever sits inside <d> verbatim, so a paraphrase or a
    # translation there is a wrong take that no amount of re-rendering fixes.
    # Whitespace is squeezed on both sides because CJK source text carries none.
    haystack = _squeeze(source)
    warnings: list[str] = []
    for match in DIALOGUE.finditer(body):
        line = LANGUAGE_TAG.sub("", match.group(1)).strip()
        if not line:
            warnings.append("有一处 <d> 标签是空的")
        elif _squeeze(line) not in haystack:
            warnings.append(f"台词和原文对不上,不能改写或翻译: {line}")
    return warnings


def _check_soundscape(text: str) -> list[str]:
    if _is_not_applicable(text):
        return []
    if "<d>" in text:
        return ["overall_soundscape 只放环境音和动作音效,对白属于画面描述字段"]
    return []


def _check_score(text: str) -> list[str]:
    if _is_not_applicable(text):
        return []
    found = sorted({word for word in WORD.findall(text.lower()) if word in MOOD_WORDS})
    if found:
        return [
            "non_diegetic_music 只写乐器、速度、节奏和强弱,去掉情绪词: "
            + ", ".join(found)
        ]
    return []


def _squeeze(text: str) -> str:
    return "".join(text.split())


def _is_not_applicable(text: str) -> bool:
    return text.strip().upper() in NOT_APPLICABLE


class PromptDirector:
    """Wraps a provider with the retry, verification and caching around it."""

    def __init__(
        self,
        provider: Director,
        *,
        settings: DirectorConfig,
        renderer: RendererConfig,
        cache_dir: Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self._provider = provider
        self._settings = settings
        self._renderer = renderer
        self._cache_dir = cache_dir
        self._logger = logger or logging.getLogger("videogen_service")

    def enhance(self, request: EnhanceRequest) -> EnhanceResponse:
        seconds = aligned_seconds(request.seconds, fps=self._renderer.fps)
        cached = self._read_cache(request, seconds=seconds)
        if cached is not None:
            return cached
        try:
            response = self._write(request, seconds=seconds)
        except DirectorError as error:
            # Never block on the director: hand back the original prompt and let
            # the operator decide whether to submit it as-is.
            self._logger.warning("提示词改写失败,退回原文: %s", error)
            return EnhanceResponse(
                prompt=request.prompt,
                enhanced=False,
                fields=None,
                warnings=[f"提示词改写失败,已退回原文: {error}"],
                seconds=seconds,
            )
        self._write_cache(request, seconds=seconds, response=response)
        return response

    def _write(self, request: EnhanceRequest, *, seconds: float) -> EnhanceResponse:
        best: tuple[H3Prompt, list[str]] | None = None
        feedback: list[str] = []
        for _ in range(max(1, self._settings.max_attempts)):
            prompt = self._provider.write(
                idea=request.prompt,
                seconds=seconds,
                mode=request.mode,
                feedback=feedback,
            )
            warnings = check_prompt(
                prompt,
                source=request.prompt,
                seconds=seconds,
                require_verbatim_dialogue=self._settings.require_verbatim_dialogue,
            )
            if best is None or len(warnings) < len(best[1]):
                best = (prompt, warnings)
            if not warnings:
                break
            feedback = warnings
        assert best is not None  # the loop runs at least once
        prompt, warnings = best
        if warnings:
            self._logger.info(
                "改写后的提示词仍有 %d 处不合规,已连同结果返回", len(warnings)
            )
        return EnhanceResponse(
            prompt=prompt.render(),
            enhanced=True,
            fields=prompt,
            warnings=warnings,
            seconds=seconds,
        )

    def _cache_path(self, request: EnhanceRequest, *, seconds: float) -> Path:
        digest = hashlib.sha256(
            json.dumps(
                [
                    self._settings.provider,
                    self._settings.model,
                    self._settings.require_verbatim_dialogue,
                    request.mode,
                    request.prompt,
                    round(seconds, 4),
                ],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return self._cache_dir / f"{digest}.json"

    def _read_cache(
        self, request: EnhanceRequest, *, seconds: float
    ) -> EnhanceResponse | None:
        path = self._cache_path(request, seconds=seconds)
        if not path.is_file():
            return None
        try:
            return EnhanceResponse.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # A stale or truncated entry is not worth failing a rewrite over.
            return None

    def _write_cache(
        self, request: EnhanceRequest, *, seconds: float, response: EnhanceResponse
    ) -> None:
        try:
            write_json_atomic(
                self._cache_path(request, seconds=seconds), response.model_dump()
            )
        except OSError as error:
            self._logger.warning("提示词缓存写入失败: %s", error)


def build_guide(settings: DirectorConfig) -> str:
    if settings.guide_file is None:
        raise DirectorError("director.guide_file 未配置")
    try:
        return settings.guide_file.read_text(encoding="utf-8")
    except OSError as error:
        raise DirectorError(f"读不到提示词指南: {settings.guide_file}") from error


def build_director(settings: DirectorConfig) -> Director:
    guide = build_guide(settings)
    if settings.provider == "anthropic":
        return AnthropicDirector(settings, guide=guide)
    return OllamaDirector(settings, guide=guide)


def _task(idea: str, *, seconds: float, mode: RenderMode, feedback: Sequence[str]) -> str:
    lines = [
        f"Target duration: {seconds:.2f} seconds. Every [Shot N] cut time must be "
        f"below {seconds:.2f}.",
        f"Generation mode: {mode}.",
        "",
        "Idea to rewrite:",
        idea,
    ]
    if feedback:
        lines += [
            "",
            "Your previous attempt broke these rules. Fix every one of them and "
            "keep the rest of the prompt intact:",
            *(f"- {item}" for item in feedback),
        ]
    return "\n".join(lines)


class AnthropicDirector:
    """Claude with structured outputs, so only the content rules need checking."""

    def __init__(self, settings: DirectorConfig, *, guide: str) -> None:
        import anthropic

        self._anthropic = anthropic
        self._settings = settings
        self._guide = guide
        key = settings.api_key()
        self._client = (
            anthropic.Anthropic(api_key=key, timeout=settings.timeout_seconds)
            if key is not None
            # No key in the environment is not an error: the SDK also resolves
            # an `ant auth login` profile.
            else anthropic.Anthropic(timeout=settings.timeout_seconds)
        )

    def write(
        self,
        *,
        idea: str,
        seconds: float,
        mode: RenderMode,
        feedback: Sequence[str],
    ) -> H3Prompt:
        try:
            response = self._client.messages.parse(
                model=self._settings.model,
                max_tokens=self._settings.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": self._guide,
                        # The guide is a fixed prefix on every rewrite; caching it
                        # takes the repeat cost of these calls down to a rounding
                        # error against one render.
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                output_format=H3Prompt,
                messages=[
                    {
                        "role": "user",
                        "content": _task(
                            idea, seconds=seconds, mode=mode, feedback=feedback
                        ),
                    }
                ],
            )
        except self._anthropic.APIStatusError as error:
            raise DirectorError(f"Claude 返回 HTTP {error.status_code}: {error}") from error
        except self._anthropic.APIConnectionError as error:
            raise DirectorError(f"连接 Claude 失败: {error}") from error
        if response.stop_reason == "refusal":
            raise DirectorError("Claude 拒绝了这次改写")
        parsed = response.parsed_output
        if not isinstance(parsed, H3Prompt):
            raise DirectorError("Claude 没有按三字段结构返回")
        return parsed


class OllamaDirector:
    """The local path — no API key, no new dependency, same seam."""

    def __init__(self, settings: DirectorConfig, *, guide: str) -> None:
        self._settings = settings
        self._guide = guide

    def write(
        self,
        *,
        idea: str,
        seconds: float,
        mode: RenderMode,
        feedback: Sequence[str],
    ) -> H3Prompt:
        payload = json.dumps(
            {
                "model": self._settings.model,
                "stream": False,
                "format": H3Prompt.model_json_schema(),
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": self._guide},
                    {
                        "role": "user",
                        "content": _task(
                            idea, seconds=seconds, mode=mode, feedback=feedback
                        ),
                    },
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._settings.base_url.rstrip('/')}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._settings.timeout_seconds
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except (TimeoutError, URLError, OSError) as error:
            raise DirectorError(f"连接 Ollama 失败: {error}") from error
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise DirectorError("Ollama 返回了非 JSON 响应") from error
        content = _dig(body, "message", "content")
        if not isinstance(content, str):
            raise DirectorError("Ollama 响应里没有 message.content")
        try:
            return H3Prompt.model_validate_json(content)
        except ValueError as error:
            raise DirectorError(f"Ollama 没有按三字段结构返回: {error}") from error


def _dig(value: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value
