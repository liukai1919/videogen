# Turns a YouTube URL into a storyboard this service can already render.
# The pipeline is: yt-dlp captions -> local LLM summary -> shot list -> the same
# "[0s-6s] ..." prompt a person would have typed into the storyboard box. The
# studio deliberately stops there: choosing when to spend the GPU stays with the
# control plane, which posts the returned prompt to /v1/renders.

import json
import logging
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.error import HTTPError, URLError
import urllib.request

from pydantic import BaseModel, Field, ValidationError

from videogen_service.config import RenderMode, ServiceConfig
from videogen_service.models import (
    DocumentSource,
    ScriptOptions,
    ScriptRequest,
    ScriptResult,
    ScriptShot,
    ScriptSource,
)
from videogen_service.renderer import (
    align_length_frames,
    parse_storyboard,
    storyboard_seconds,
    validate_storyboard,
)
from videogen_service.memory import MemoryStore
from videogen_service.skills import Skill, SkillLibrary
from videogen_service.transcript import (
    Transcript,
    TranscriptError,
    TranscriptFetcher,
    TranscriptUnavailable,
    YtDlpTranscriptFetcher,
)

_LOGGER = logging.getLogger("videogen_service.scripting")
_TRUNCATION_NOTE = "\n[字幕过长,后面的部分已截断]"
_ScriptOptionsT = TypeVar("_ScriptOptionsT", bound=ScriptOptions)


class ScriptError(RuntimeError):
    pass


class ScriptUnavailable(ScriptError):
    """A dependency of the pipeline (yt-dlp, YouTube, Ollama) is not answering."""


class Summarizer(Protocol):
    def summarize(self, prompt: str) -> str: ...


class OllamaSummarizer:
    def __init__(self, config: ServiceConfig) -> None:
        self._base_url = config.ollama.base_url.rstrip("/")
        self._model = config.script.model or config.ollama.model
        self._script = config.script

    @property
    def model(self) -> str:
        return self._model

    def summarize(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                # Ollama constrains decoding to valid JSON, which is what lets
                # the shot list be parsed instead of scraped out of prose.
                "format": "json",
                "options": {"temperature": self._script.temperature},
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/generate",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._script.timeout_seconds
            ) as response:
                body = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise ScriptUnavailable(f"Ollama 返回 {error.code}: {detail}") from error
        except (TimeoutError, URLError, OSError) as error:
            raise ScriptUnavailable(
                f"连不上 Ollama({self._base_url}),请先启动它: {error}"
            ) from error
        try:
            document = json.loads(body)
        except json.JSONDecodeError as error:
            raise ScriptUnavailable("Ollama 的响应不是 JSON") from error
        text = document.get("response") if isinstance(document, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ScriptUnavailable("Ollama 没有返回内容")
        return text


class _DraftShot(BaseModel):
    seconds: float = 0.0
    prompt: str = ""
    narration: str = ""


class _Draft(BaseModel):
    title: str = ""
    summary: str = ""
    shots: list[_DraftShot] = Field(default_factory=list)


class ScriptStudio:
    def __init__(
        self,
        config: ServiceConfig,
        *,
        fetcher: TranscriptFetcher | None = None,
        summarizer: Summarizer | None = None,
        skills: SkillLibrary | None = None,
        memory: MemoryStore | None = None,
    ) -> None:
        self._config = config
        self._fetcher = fetcher or YtDlpTranscriptFetcher(config.script)
        self._summarizer = summarizer or OllamaSummarizer(config)
        self._skills = skills or SkillLibrary(config.skills_dir)
        self._memory = memory or MemoryStore(config.work_dir)

    def create(self, request: ScriptRequest) -> ScriptResult:
        settings = self._config.script
        if not settings.enabled:
            raise ScriptError("本服务没有开启字幕总结功能(script.enabled)")
        request, skill = self._resolve_skill(request)
        try:
            transcript = self._fetcher.fetch(request.url)
        except TranscriptUnavailable as error:
            raise ScriptUnavailable(str(error)) from error
        except TranscriptError as error:
            raise ScriptError(str(error)) from error

        text, truncated = _clip(transcript.text, settings.max_transcript_chars)
        draft = _parse_draft(
            self._summarizer.summarize(
                build_summary_prompt(
                    transcript,
                    text=text,
                    truncated=truncated,
                    request=request,
                    config=self._config,
                    skill=skill,
                    memory=self._memory.texts(),
                )
            )
        )
        shots = _fit_shots(draft.shots, request=request, config=self._config)
        preamble = _preamble(
            title=draft.title or transcript.title,
            style=request.style or settings.style,
        )
        prompt = storyboard_text(shots, preamble=preamble)
        board = parse_storyboard(prompt, default_seconds=settings.shot_seconds)
        validate_storyboard(board, config=self._config)
        _LOGGER.info(
            "已从 %s 生成 %d 段分镜(字幕 %s%s)",
            transcript.url,
            len(shots),
            transcript.language,
            ",自动字幕" if transcript.automatic else "",
        )
        return ScriptResult(
            source=ScriptSource(
                video_id=transcript.video_id,
                title=transcript.title,
                url=transcript.url,
                subtitle_language=transcript.language,
                automatic_captions=transcript.automatic,
                duration_seconds=transcript.duration_seconds,
                transcript_chars=len(text),
                transcript_truncated=truncated,
            ),
            title=draft.title or transcript.title,
            summary=draft.summary,
            mode=_script_mode(self._config),
            prompt=prompt,
            seconds=storyboard_seconds(board, fps=self._config.renderer.fps),
            shots=shots,
        )

    def create_from_document(
        self, options: ScriptOptions, *, filename: str, text: str
    ) -> ScriptResult:
        """Same storyboard pipeline, seeded by a local file instead of
        captions: the text was already extracted on this machine."""
        settings = self._config.script
        if not settings.enabled:
            raise ScriptError("本服务没有开启字幕总结功能(script.enabled)")
        options, skill = self._resolve_skill(options)
        if not text.strip():
            raise ScriptError("文档里没有可用的文字")
        clipped, truncated = _clip(text, settings.max_transcript_chars)
        title = Path(filename).stem or filename
        draft = _parse_draft(
            self._summarizer.summarize(
                build_document_prompt(
                    title,
                    text=clipped,
                    truncated=truncated,
                    request=options,
                    config=self._config,
                    skill=skill,
                    memory=self._memory.texts(),
                )
            )
        )
        shots = _fit_shots(draft.shots, request=options, config=self._config)
        preamble = _preamble(
            title=draft.title or title, style=options.style or settings.style
        )
        prompt = storyboard_text(shots, preamble=preamble)
        board = parse_storyboard(prompt, default_seconds=settings.shot_seconds)
        validate_storyboard(board, config=self._config)
        _LOGGER.info("已从文档 %s 生成 %d 段分镜", filename, len(shots))
        return ScriptResult(
            source=DocumentSource(
                filename=filename, chars=len(clipped), truncated=truncated
            ),
            title=draft.title or title,
            summary=draft.summary,
            mode=_script_mode(self._config),
            prompt=prompt,
            seconds=storyboard_seconds(board, fps=self._config.renderer.fps),
            shots=shots,
        )

    def _resolve_skill(
        self, options: _ScriptOptionsT
    ) -> tuple[_ScriptOptionsT, Skill | None]:
        if options.skill is None:
            return options, None
        skill = self._skills.get(options.skill)
        if skill is None:
            raise ScriptError(f"Skill「{options.skill}」不存在或加载失败")
        return _apply_skill(options, skill), skill


def _script_mode(config: ServiceConfig) -> RenderMode:
    if "story" in config.modes:
        return "story"
    raise ScriptError("生成分镜需要 story 模式,请在 config.yaml 里配置它")


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + _TRUNCATION_NOTE, True


def _preamble(*, title: str, style: str) -> str:
    return f"科普短片《{title}》。统一风格:{style}。"


def _apply_skill(request: _ScriptOptionsT, skill: Skill) -> _ScriptOptionsT:
    """Fill request fields the caller left unset from the skill's defaults.

    Explicit request values always win; the skill only supplies what is
    missing, and config defaults remain the final fallback downstream.
    """
    defaults = skill.meta.defaults
    updates: dict[str, object] = {}
    if request.style is None and defaults.style is not None:
        updates["style"] = defaults.style
    if request.target_seconds is None and defaults.target_seconds is not None:
        updates["target_seconds"] = defaults.target_seconds
    if request.shot_seconds is None and defaults.shot_seconds is not None:
        updates["shot_seconds"] = defaults.shot_seconds
    if request.max_shots is None and defaults.max_shots is not None:
        updates["max_shots"] = defaults.max_shots
    # output_language has a non-null default, so "the caller left it unset" is
    # visible only through model_fields_set, not through the value itself.
    if (
        "output_language" not in request.model_fields_set
        and defaults.output_language is not None
    ):
        updates["output_language"] = defaults.output_language
    return request.model_copy(update=updates) if updates else request


def build_summary_prompt(
    transcript: Transcript,
    *,
    text: str,
    truncated: bool,
    request: ScriptOptions,
    config: ServiceConfig,
    skill: Skill | None = None,
    memory: list[str] | None = None,
) -> str:
    intro = [
        f"你是科普短视频的编剧。下面是 YouTube 视频《{transcript.title}》的字幕",
        "(可能是机器自动听写的,会有错别字和口语,请按语义理解)。",
    ]
    return _compose_summary_prompt(
        intro,
        source_label="字幕",
        text=text,
        truncated=truncated,
        request=request,
        config=config,
        skill=skill,
        memory=memory,
    )


def build_document_prompt(
    title: str,
    *,
    text: str,
    truncated: bool,
    request: ScriptOptions,
    config: ServiceConfig,
    skill: Skill | None = None,
    memory: list[str] | None = None,
) -> str:
    intro = [f"你是科普短视频的编剧。下面是本地资料文档《{title}》的内容。"]
    return _compose_summary_prompt(
        intro,
        source_label="文档",
        text=text,
        truncated=truncated,
        request=request,
        config=config,
        skill=skill,
        memory=memory,
    )


def _compose_summary_prompt(
    intro: list[str],
    *,
    source_label: str,
    text: str,
    truncated: bool,
    request: ScriptOptions,
    config: ServiceConfig,
    skill: Skill | None,
    memory: list[str] | None,
) -> str:
    settings = config.script
    renderer = config.renderer
    shot_seconds = request.shot_seconds or settings.shot_seconds
    max_shots = min(request.max_shots or settings.max_shots, settings.max_shots)
    target_seconds = request.target_seconds or settings.target_seconds
    wanted = max(1, min(max_shots, round(target_seconds / shot_seconds)))
    lines = [
        *intro,
        f"请把它改写成一条 {target_seconds:g} 秒左右的原创科普短片脚本。",
        "",
        "要求:",
        f"1. 全部用{request.output_language}输出。",
        "2. title:不超过 20 个字的短片标题。",
        "3. summary:2-3 句话,说明这条短片讲什么、结论是什么。",
        f"4. shots:{wanted} 个分镜(最多 {max_shots} 个),按讲解顺序排列。",
        f"5. 每个分镜的 seconds 在 {renderer.min_seconds:g} 到 "
        f"{renderer.max_seconds:g} 之间,建议 {shot_seconds:g}。",
        "6. 每个分镜的 prompt 是给文生视频模型的画面描述:主体、动作、镜头运动、",
        "   光线、氛围,一句到两句,不要写字幕、文字、水印、台标或真实人物姓名。",
        "7. 每个分镜的 narration 是这一镜的解说词,一到两句;所有 narration 连起来",
        "   要能独立讲清楚原理,不能只是复述画面。",
        f"8. 内容要重新组织和表达,不要整句照抄原{source_label}。",
        "9. 只输出 JSON,不要解释,不要代码块。",
        "",
        'JSON 结构:{"title": "...", "summary": "...", "shots": '
        '[{"seconds": 6, "prompt": "...", "narration": "..."}]}',
    ]
    if memory:
        # Standing preferences the user explicitly saved; skill and per-run
        # direction come later, so they can still override for one run.
        lines += ["", "长期偏好(用户此前明确要求,持续生效):"]
        lines += [f"- {entry}" for entry in memory]
    if skill is not None and skill.instructions:
        # The user's 额外要求 comes after these, so ad-hoc direction can still
        # override a preset for one run.
        lines += [
            "",
            f"创作规范(来自 Skill「{skill.meta.name}」,必须遵守):",
            skill.instructions,
        ]
    if request.guidance:
        lines += ["", f"额外要求:{request.guidance}"]
    if truncated:
        lines += ["", f"注意:{source_label}已被截断,请根据现有部分完成脚本。"]
    lines += ["", f"{source_label}原文:", "<<<", text, ">>>"]
    return "\n".join(lines)


def _parse_draft(response: str) -> _Draft:
    payload = _json_object(response)
    try:
        draft = _Draft.model_validate(payload)
    except ValidationError as error:
        raise ScriptError(f"模型返回的脚本结构不对: {error}") from error
    if not draft.shots:
        raise ScriptError("模型没有给出分镜,请重试或换一个 Ollama 模型")
    return draft


def _json_object(response: str) -> dict[str, Any]:
    text = response.strip()
    if text.startswith("```"):
        text = text.strip("`")
        _, _, text = text.partition("\n")
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        raise ScriptError(f"模型没有返回 JSON: {response[:200]}")
    try:
        document = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise ScriptError(f"模型返回的 JSON 解析失败: {error}") from error
    if not isinstance(document, dict):
        raise ScriptError("模型返回的 JSON 不是对象")
    return document


def _fit_shots(
    drafts: list[_DraftShot], *, request: ScriptOptions, config: ServiceConfig
) -> list[ScriptShot]:
    """Clamp a model's shot list onto the renderer's real limits."""
    settings = config.script
    renderer = config.renderer
    fallback = request.shot_seconds or settings.shot_seconds
    max_shots = min(request.max_shots or settings.max_shots, settings.max_shots)
    budget = min(
        request.target_seconds or settings.target_seconds, renderer.max_total_seconds
    )
    shots: list[ScriptShot] = []
    total_seconds = 0.0
    for draft in drafts:
        if len(shots) >= max_shots:
            break
        prompt = " ".join(draft.prompt.split())
        if not prompt:
            continue
        seconds = draft.seconds if draft.seconds > 0 else fallback
        seconds = min(max(seconds, renderer.min_seconds), renderer.max_seconds)
        # The renderer pads every shot up to the next H3 length step, so the
        # budget has to be checked against the padded duration, not the ask.
        padded = align_length_frames(seconds, fps=renderer.fps) / renderer.fps
        if shots and total_seconds + padded > budget:
            break
        total_seconds += padded
        shots.append(
            ScriptShot(
                seconds=seconds,
                prompt=prompt,
                narration=" ".join(draft.narration.split()),
            )
        )
    if not shots:
        raise ScriptError("模型给出的分镜都没有画面描述,请重试")
    return shots


def storyboard_text(shots: list[ScriptShot], *, preamble: str) -> str:
    lines = [preamble, ""] if preamble else []
    start = 0.0
    for shot in shots:
        end = start + shot.seconds
        lines.append(f"[{start:g}s-{end:g}s] {shot.prompt}")
        start = end
    return "\n".join(lines)
