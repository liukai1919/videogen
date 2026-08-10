# The platform's brain: a tool-calling agent whose every tool is one of this
# machine's own capabilities — submit an image or TTS job, queue an H3 render,
# draft a storyboard, browse assets, remember a preference. The default brain
# is the local Ollama model (/api/chat, native tool calls); a configured cloud
# CLI worker (claude/codex) can drive the very same loop instead, its tool
# calls carried over a fenced-JSON text protocol — reasoning may go to the
# cloud, execution always stays on this machine.
#
# Long generations never block a chat turn: generation tools SUBMIT work and
# return a job/render id immediately; the model reports the id and can check
# on it in a later turn with the status tools. A turn is therefore bounded by
# Ollama's own thinking time plus a handful of fast local calls.
#
# Sessions are durable: work/.chats/<chat_id>/chat.json, same atomic style as
# every other store.

import asyncio
from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
import secrets
import shutil
from threading import Lock
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
import urllib.request

from pydantic import BaseModel, ConfigDict, Field

from videogen_service.agents import AgentManager
from videogen_service.artifacts import write_json_atomic
from videogen_service.assets import AssetStore
from videogen_service.config import ServiceConfig
from videogen_service.export import narration_srt
from videogen_service.jobs import (
    COMPOSE_CAPABILITY_ID,
    JobError,
    JobNotFound,
    JobService,
    JobSpec,
)
from videogen_service.memory import MemoryStore
from videogen_service.models import RenderSpec, ScriptOptions
from videogen_service.renderer import RenderError, is_timeline_mode
from videogen_service.scripting import ScriptError, ScriptStudio
from videogen_service.service import (
    RenderConflict,
    RenderNotFound,
    RenderService,
)

_LOGGER = logging.getLogger("videogen_service.agent")
_CHAT_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
CHATS_DIRNAME = ".chats"
# A model that keeps calling tools without answering is looping, not working.
_MAX_TOOL_ROUNDS = 8
_MAX_TURN_MESSAGES = 60  # context sent to the brain per turn, newest kept
# 本机实测内存红线(2026-08-09 两次 OOM 击杀 ComfyUI 得出):1080p 在模型
# 加载阶段就顶穿 32GB 内存,864x480 的 30 秒双段渲染同样阵亡。安全包络是
# ≤864x480 的像素量、单条渲染总时长 ≤renderer.max_seconds(时长上限走配置,
# 换机器/扩内存后改 config 即可,这里只钉死像素量)。
_MAX_RENDER_PIXELS = 864 * 480


class AgentError(RuntimeError):
    pass


class AgentUnavailable(AgentError):
    """Ollama is not answering; the platform's brain is offline."""


class ChatNotFound(AgentError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatMessage(StrictModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    # Ollama's tool_calls shape is stored verbatim so a transcript replays.
    tool_calls: list[dict[str, Any]] | None = None
    tool_name: str | None = None
    # 哪个外部 Agent worker 答的这条(claude/codex…);None = 本地大脑。
    agent: str | None = None
    created_at: str


class ChatRecord(StrictModel):
    schema_version: Literal[1] = 1
    chat_id: str
    title: str = Field(default="新对话", max_length=120)
    messages: list[ChatMessage] = Field(default_factory=list)
    created_at: str
    updated_at: str


class ChatSummary(StrictModel):
    chat_id: str
    title: str
    message_count: int
    created_at: str
    updated_at: str


class ChatClient(Protocol):
    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """One /api/chat round; returns the assistant message dict."""
        ...


class OllamaChatClient:
    def __init__(self, config: ServiceConfig) -> None:
        self._base_url = config.ollama.base_url.rstrip("/")
        self._model = config.script.model or config.ollama.model
        self._timeout = config.script.timeout_seconds

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "model": self._model,
                "messages": messages,
                "tools": tools,
                "stream": False,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base_url}/api/chat",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                body = response.read()
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:400]
            raise AgentUnavailable(f"Ollama 返回 {error.code}: {detail}") from error
        except (TimeoutError, URLError, OSError) as error:
            raise AgentUnavailable(
                f"连不上 Ollama({self._base_url}),请先启动它: {error}"
            ) from error
        try:
            document = json.loads(body)
        except json.JSONDecodeError as error:
            raise AgentUnavailable("Ollama 的响应不是 JSON") from error
        message = document.get("message") if isinstance(document, dict) else None
        if not isinstance(message, dict):
            raise AgentUnavailable("Ollama 没有返回 message")
        return message


class WorkerChatClient:
    """ChatClient over one external CLI worker (claude/codex): the same tool
    loop as the local brain, with tool calls carried in a fenced-JSON text
    protocol because headless CLIs have no native tool-calling seam. The CLIs
    are stateless between invocations, so each round is one fresh call with
    the whole conversation in the prompt."""

    def __init__(self, workers: AgentManager, agent: str) -> None:
        self._workers = workers
        self._agent = agent

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        # send() runs on FastAPI's worker threads, so this thread owns no
        # event loop and asyncio.run is safe.
        result = asyncio.run(
            self._workers.run(self._agent, _worker_prompt(messages, tools))
        )
        if not result.success or not result.output:
            raise AgentUnavailable(
                f"外部 Agent {self._agent} 没答上来: {result.error or '回答是空的'}"
            )
        return _parse_worker_reply(result.output)


def _worker_prompt(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]]
) -> str:
    """One self-contained prompt: the system rules, the tool catalog with the
    calling protocol, then the transcript so far."""
    lines = [
        str(message.get("content") or "")
        for message in messages
        if message.get("role") == "system"
    ]
    lines += [
        "",
        "## 工具调用协议",
        "下面是你能调用的工具(JSON Schema)。要调用工具时,这一轮只输出一个",
        '```json 代码块,内容是 {"tool": "<工具名>", "arguments": {...}};',
        "一次一个工具,代码块外不要写别的。结果会出现在下一轮的对话记录里。",
        "不需要工具时,直接输出给用户的最终回答(纯文本,不要 ```json 块)。",
        "你自带的本地工具(文件读写、命令执行、联网搜索等)一律不要用:",
        "平台工具只认上面的 JSON 协议,别的动作都是白费。",
        f"一次用户请求最多 {_MAX_TOOL_ROUNDS} 轮工具调用,超限会被截断,",
        "规划好再动手。",
        "",
        "## 可用工具",
        "```json",
        json.dumps(tools, ensure_ascii=False, indent=2),
        "```",
        "",
        "## 对话记录",
    ]
    for message in messages:
        role = message.get("role")
        content = str(message.get("content") or "")
        if role == "user":
            lines.append(f"用户: {content}")
        elif role == "assistant":
            calls = message.get("tool_calls")
            if calls:
                lines.append(f"你(工具调用): {json.dumps(calls, ensure_ascii=False)}")
            if content:
                lines.append(f"你: {content}")
        elif role == "tool":
            name = message.get("tool_name") or "unknown"
            lines.append(f"工具 {name} 返回: {content}")
    lines += ["", "现在输出你的下一步:工具调用的 ```json 块,或给用户的最终回答。"]
    return "\n".join(lines)


_JSON_BLOCK = re.compile(r"```(?:json)?\s*\n(.*?)```", re.DOTALL)


def _parse_worker_reply(output: str) -> dict[str, Any]:
    """The worker's text turned into an Ollama-shaped assistant message:
    fenced {"tool": …} JSON becomes tool_calls, everything else stays text."""
    calls: list[dict[str, Any]] = []
    content = output
    for match in _JSON_BLOCK.finditer(output):
        payloads = _tool_payloads(match.group(1))
        if payloads:
            calls += payloads
            content = content.replace(match.group(0), "", 1)
    if not calls:
        # 有的模型不写 fence,整条回复就是一个裸 JSON 工具调用。
        bare = _tool_payloads(output)
        if bare:
            return {"content": "", "tool_calls": bare}
    return {"content": content.strip(), "tool_calls": calls or None}


def _tool_payloads(text: str) -> list[dict[str, Any]]:
    """{"tool": …, "arguments": …} objects (one or a list) in Ollama's
    tool_calls shape; anything else → []."""
    try:
        parsed = json.loads(text.strip())
    except json.JSONDecodeError:
        return []
    items = parsed if isinstance(parsed, list) else [parsed]
    payloads: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
            return []
        arguments = item.get("arguments")
        payloads.append(
            {
                "function": {
                    "name": item["tool"],
                    "arguments": arguments if isinstance(arguments, dict) else {},
                }
            }
        )
    return payloads


class ToolBox:
    """Every tool the agent may call, each a thin wrapper over a platform
    service. Tools return JSON-serialisable dicts; errors come back as
    {"error": ...} so the model can read them and correct course."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        renders: RenderService,
        jobs: JobService,
        studio: ScriptStudio,
        assets: AssetStore,
        memory: MemoryStore,
    ) -> None:
        self._config = config
        self._renders = renders
        self._jobs = jobs
        self._studio = studio
        self._assets = assets
        self._memory = memory

    def definitions(self) -> list[dict[str, Any]]:
        def tool(
            name: str, description: str, properties: dict[str, Any], required: list[str]
        ) -> dict[str, Any]:
            return {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": {
                        "type": "object",
                        "properties": properties,
                        "required": required,
                    },
                },
            }

        text = {"type": "string"}
        integer = {"type": "integer"}
        return [
            tool(
                "list_capabilities",
                "列出这台机器能生成什么(视频模式和图片/配音能力)。",
                {},
                [],
            ),
            tool(
                "generate_image",
                "用本地文生图能力排一张图片任务,立即返回 job_id;用 check_job 查进度。",
                {
                    "capability": {**text, "description": "t2i 能力 id"},
                    "prompt": {**text, "description": "画面描述"},
                    "width": integer,
                    "height": integer,
                },
                ["capability", "prompt"],
            ),
            tool(
                "generate_speech",
                "用本地 TTS 能力朗读解说词,立即返回 job_id。",
                {
                    "capability": {**text, "description": "tts 能力 id"},
                    "text": {**text, "description": "要朗读的解说词"},
                },
                ["capability", "text"],
            ),
            tool(
                "generate_video",
                "排一条 H3 视频渲染,立即返回 render_id;用 check_render 查进度。"
                "分镜长片用 story 模式,prompt 里写 [0s-6s] 时间轴,总时长可到 "
                f"{self._config.renderer.max_total_seconds:g} 秒(内部自动拆成 ≤"
                f"{self._config.renderer.max_seconds:g} 秒的块串行渲染);"
                "图生视频用 i2v 并给 first_frame_asset。"
                "本机分辨率上限 864x480,其他模式单条 ≤"
                f"{self._config.renderer.max_seconds:g} 秒,超限直接报错。",
                {
                    "mode": {**text, "description": "t2v/i2v/flf2v/story/r2v"},
                    "prompt": text,
                    "seconds": {"type": "number"},
                    "width": integer,
                    "height": integer,
                    "first_frame_asset": {**text, "description": "资产 id,i2v/flf2v 用"},
                    "last_frame_asset": {**text, "description": "资产 id,flf2v 用"},
                    "ref_assets": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "资产 id 列表(≤9),r2v 参考生成用:"
                        "参考图锁定角色/物体身份,提示词里按序用"
                        " <Picture 1>…<Picture N> 引用",
                    },
                },
                ["mode", "prompt"],
            ),
            tool(
                "write_storyboard",
                "把一段想法或资料文字改写成可渲染的分镜脚本(带 [0s-6s] 时间轴),"
                "返回的 prompt 可直接交给 generate_video 的 story 模式。",
                {
                    "idea": {**text, "description": "想法、主题或资料原文"},
                    "target_seconds": {"type": "number"},
                    "skill": {**text, "description": "套用的 Skill id,可省略"},
                },
                ["idea"],
            ),
            tool(
                "compose_video",
                "用 FFmpeg 把一条完成的渲染合成为成片:配音解说压混在 H3 原生"
                "音轨上、烧入字幕、放大到高清。配音先用 generate_speech 生成,"
                "字幕用 write_storyboard 返回的 narration_srt;用户要 1080p 就传"
                " upscale_height=1080(渲染本身保持 864x480)。立即返回 job_id。",
                {
                    "render_id": {**text, "description": "已完成的渲染 id"},
                    "audio_job_id": {**text, "description": "配音任务 id,可省略"},
                    "subtitles_srt": {**text, "description": "SRT 字幕原文,可省略"},
                    "upscale_height": {
                        **integer,
                        "description": "放大目标高度(如 1080),宽按比例,可省略",
                    },
                },
                ["render_id"],
            ),
            tool(
                "check_job",
                "查一条图片/配音/合成任务的状态。",
                {"job_id": text},
                ["job_id"],
            ),
            tool(
                "check_render",
                "查一条视频渲染的状态。",
                {"render_id": text},
                ["render_id"],
            ),
            tool(
                "inspect_failure",
                "失败诊断:打包一条失败渲染/任务的完整上下文(报错原文、提交"
                "参数、对应工作流配置、ComfyUI 连通性),据此分析原因并给出"
                "修复步骤。给 render_id 或 job_id 其中一个。",
                {"render_id": text, "job_id": text},
                [],
            ),
            tool(
                "list_assets",
                "列出资产中心里可复用的参考图(角色、场景、风格包…)。",
                {},
                [],
            ),
            tool(
                "save_memory",
                "用户明确要求记住某个长期偏好时调用;不要自作主张。",
                {"text": {**text, "description": "偏好原文"}},
                ["text"],
            ),
        ]

    def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handlers = {
            "list_capabilities": self._list_capabilities,
            "generate_image": self._generate_image,
            "generate_speech": self._generate_speech,
            "generate_video": self._generate_video,
            "compose_video": self._compose_video,
            "write_storyboard": self._write_storyboard,
            "check_job": self._check_job,
            "check_render": self._check_render,
            "inspect_failure": self._inspect_failure,
            "list_assets": self._list_assets,
            "save_memory": self._save_memory,
        }
        handler = handlers.get(name)
        if handler is None:
            return {"error": f"没有这个工具: {name}"}
        try:
            return handler(arguments)
        except (
            JobError,
            JobNotFound,
            RenderError,
            RenderConflict,
            RenderNotFound,
            ScriptError,
            ValueError,
        ) as error:
            return {"error": str(error)}
        except Exception as error:  # a tool bug must not kill the chat turn
            _LOGGER.exception("工具 %s 执行失败", name)
            return {"error": f"{type(error).__name__}: {error}"}

    def _list_capabilities(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "video_modes": sorted(self._config.modes),
            "capabilities": [
                {
                    "capability_id": capability_id,
                    "kind": capability.kind,
                    "output": "image" if capability.kind == "t2i" else "audio",
                }
                for capability_id, capability in sorted(
                    self._config.capabilities.items()
                )
            ],
        }

    def _generate_image(self, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = JobSpec(
            job_id=_mint_id("job"),
            capability=str(arguments["capability"]),
            prompt=str(arguments["prompt"]),
            width=_optional_int(arguments.get("width")),
            height=_optional_int(arguments.get("height")),
        )
        view = self._jobs.submit(spec)
        return {"job_id": view.job_id, "status": view.status}

    def _generate_speech(self, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = JobSpec(
            job_id=_mint_id("job"),
            capability=str(arguments["capability"]),
            text=str(arguments["text"]),
        )
        view = self._jobs.submit(spec)
        return {"job_id": view.job_id, "status": view.status}

    def _generate_video(self, arguments: dict[str, Any]) -> dict[str, Any]:
        spec = RenderSpec(
            render_id=_mint_id("ag"),
            mode=arguments["mode"],
            prompt=str(arguments["prompt"]),
            width=_optional_int(arguments.get("width")) or 864,
            height=_optional_int(arguments.get("height")) or 480,
            seconds=float(arguments.get("seconds") or 6.0),
        )
        self._guard_render_envelope(spec)
        raw_refs = arguments.get("ref_assets")
        refs = [
            frame
            for asset_id in (raw_refs if isinstance(raw_refs, list) else [])
            if (frame := self._asset_frame(asset_id)) is not None
        ]
        view = self._renders.submit(
            spec,
            first_frame=self._asset_frame(arguments.get("first_frame_asset")),
            last_frame=self._asset_frame(arguments.get("last_frame_asset")),
            refs=refs or None,
        )
        return {"render_id": view.render_id, "status": view.status}

    def _guard_render_envelope(self, spec: RenderSpec) -> None:
        """The machine's OOM red lines, enforced where the agent queues work.
        The manual workbench path stays unrestricted — a person typing into
        the form is deliberate; a model relaying "4k画质" is not. story 的
        时长不在这里管:渲染器会把长分镜拆成安全大小的块串行渲染,总长
        上限由 validate_storyboard 在提交时把关。"""
        if spec.width * spec.height > _MAX_RENDER_PIXELS:
            raise ValueError(
                f"分辨率 {spec.width}x{spec.height} 超过本机内存红线"
                "(最高 864x480 的像素量,1080p 实测必 OOM);"
                "请用 864x480 渲染,compose_video 传 upscale_height 放大成片。"
            )
        limit = self._config.renderer.max_seconds
        mode_settings = self._config.modes.get(spec.mode)
        timeline = mode_settings is not None and is_timeline_mode(mode_settings)
        if not timeline and spec.seconds > limit:
            raise ValueError(
                f"{spec.seconds:g} 秒超过本机单条渲染红线 ≤{limit:g} 秒;"
                "请缩短,或改用 story 分镜(会自动拆块串行渲染)。"
            )

    def _compose_video(self, arguments: dict[str, Any]) -> dict[str, Any]:
        subtitles = arguments.get("subtitles_srt")
        spec = JobSpec(
            job_id=_mint_id("job"),
            capability=COMPOSE_CAPABILITY_ID,
            render_id=str(arguments["render_id"]),
            audio_job_id=(
                str(arguments["audio_job_id"])
                if arguments.get("audio_job_id")
                else None
            ),
            subtitles=str(subtitles) if subtitles else None,
            upscale_height=_optional_int(arguments.get("upscale_height")),
        )
        view = self._jobs.submit(spec)
        return {"job_id": view.job_id, "status": view.status}

    def _write_storyboard(self, arguments: dict[str, Any]) -> dict[str, Any]:
        options_payload: dict[str, Any] = {}
        if arguments.get("target_seconds"):
            options_payload["target_seconds"] = float(arguments["target_seconds"])
        if arguments.get("skill"):
            options_payload["skill"] = str(arguments["skill"])
        result = self._studio.create_from_document(
            ScriptOptions.model_validate(options_payload),
            filename="idea.txt",
            text=str(arguments["idea"]),
        )
        return {
            "title": result.title,
            "summary": result.summary,
            "mode": result.mode,
            "seconds": result.seconds,
            "prompt": result.prompt,
            "narrations": [shot.narration for shot in result.shots],
            # Ready for compose_video's subtitles_srt: cue times follow the
            # padded timeline the renderer actually produces.
            "narration_srt": narration_srt(
                result.shots, fps=self._config.renderer.fps
            ),
        }

    def _check_job(self, arguments: dict[str, Any]) -> dict[str, Any]:
        view = self._jobs.get(str(arguments["job_id"]))
        return {
            "job_id": view.job_id,
            "status": view.status,
            "error": view.error,
            "media_url": view.media_url,
        }

    def _check_render(self, arguments: dict[str, Any]) -> dict[str, Any]:
        view = self._renders.get(str(arguments["render_id"]))
        return {
            "render_id": view.render_id,
            "status": view.status,
            "error": view.error,
            "media_url": view.media_url,
        }

    def _inspect_failure(self, arguments: dict[str, Any]) -> dict[str, Any]:
        render_id = str(arguments.get("render_id") or "").strip()
        job_id = str(arguments.get("job_id") or "").strip()
        if not render_id and not job_id:
            raise ValueError("要给 render_id 或 job_id 其中一个")
        report: dict[str, Any] = {"comfyui": self._comfyui_status()}
        if render_id:
            view = self._renders.get(render_id)
            report["render"] = {
                "render_id": view.render_id,
                "status": view.status,
                "error": view.error,
            }
            # The submitted parameters live on the console listing; the frozen
            # RenderView contract carries state only.
            summary = next(
                (
                    entry
                    for entry in self._renders.list_renders()
                    if entry.render_id == render_id
                ),
                None,
            )
            if summary is not None:
                report["render"] |= {
                    "mode": summary.mode,
                    "width": summary.width,
                    "height": summary.height,
                    "seconds": summary.seconds,
                    "prompt": summary.prompt,
                }
                mode = self._config.modes.get(summary.mode)
                if mode is not None:
                    report["workflow"] = {
                        "workflow_file": str(mode.workflow_file),
                        "inputs": mode.inputs,
                    }
        if job_id:
            job = self._jobs.get(job_id)
            report["job"] = {
                "job_id": job.job_id,
                "capability": job.capability,
                "kind": job.kind,
                "status": job.status,
                "error": job.error,
                "render_id": job.render_id,
                "audio_job_id": job.audio_job_id,
            }
        return report

    def _comfyui_status(self) -> dict[str, Any]:
        """Reachability first: the single most common failure is ComfyUI
        simply not running."""
        base_url = self._config.comfyui.base_url.rstrip("/")
        try:
            with urllib.request.urlopen(
                f"{base_url}/system_stats", timeout=3
            ) as response:
                response.read()
        except (URLError, TimeoutError, OSError) as error:
            return {"base_url": base_url, "reachable": False, "detail": str(error)}
        return {"base_url": base_url, "reachable": True}

    def _list_assets(self, _arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "assets": [
                {
                    "asset_id": record.asset_id,
                    "name": record.name,
                    "category": record.category,
                }
                for record in self._assets.list()
            ]
        }

    def _save_memory(self, arguments: dict[str, Any]) -> dict[str, Any]:
        entry = self._memory.add(str(arguments["text"]))
        return {"entry_id": entry.entry_id, "text": entry.text}

    def _asset_frame(self, asset_id: object) -> tuple[str, bytes] | None:
        if not asset_id or not str(asset_id).strip():
            return None
        path = self._assets.media_path(str(asset_id).strip())
        return path.name, path.read_bytes()


class AgentService:
    """Chat sessions plus the tool-calling loop."""

    def __init__(
        self,
        config: ServiceConfig,
        *,
        toolbox: ToolBox,
        client: ChatClient | None = None,
        memory: MemoryStore | None = None,
        # 外部 Agent worker 层;None = 只有本地大脑。
        workers: AgentManager | None = None,
    ) -> None:
        self._config = config
        self._toolbox = toolbox
        self._client = client or OllamaChatClient(config)
        self._memory = memory or MemoryStore(config.work_dir)
        self._workers = workers
        self._directory = config.work_dir / CHATS_DIRNAME
        self._lock = Lock()

    def create(self, *, title: str | None) -> ChatRecord:
        with self._lock:
            now = _now()
            record = ChatRecord(
                chat_id=_mint_id("chat"),
                title=(title or "新对话").strip() or "新对话",
                created_at=now,
                updated_at=now,
            )
            self._save(record)
            return record

    # Defined before `list` below shadows the builtin in this class body.
    def _conversation(self, messages: list[ChatMessage]) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = [
            {"role": "system", "content": self._system_prompt()}
        ]
        for message in messages[-_MAX_TURN_MESSAGES:]:
            entry: dict[str, Any] = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_calls:
                entry["tool_calls"] = message.tool_calls
            if message.tool_name:
                entry["tool_name"] = message.tool_name
            payload.append(entry)
        return payload

    # Sits above `list` below: the annotation mentions the shadowed builtin.
    def _worker_client(self, agent: str) -> ChatClient:
        """The external worker wrapped as a ChatClient, so a cloud CLI turn
        runs the exact same tool loop as the local brain. An unknown agent
        name raises UnknownAgent here — before any quota is spent."""
        if self._workers is None:
            raise AgentUnavailable("外部 Agent 层没有装配,这台服务只有本地大脑")
        self._workers.adapter(agent)
        return WorkerChatClient(self._workers, agent)

    def list(self) -> list[ChatSummary]:
        with self._lock:
            summaries: list[ChatSummary] = []
            for path in self._directory.glob("*/chat.json"):
                record = self._read(path)
                if record is None or record.chat_id != path.parent.name:
                    continue
                summaries.append(
                    ChatSummary(
                        chat_id=record.chat_id,
                        title=record.title,
                        message_count=len(record.messages),
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                    )
                )
        summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
        return summaries

    def get(self, chat_id: str) -> ChatRecord:
        with self._lock:
            return self._require(chat_id)

    def delete(self, chat_id: str) -> None:
        with self._lock:
            self._require(chat_id)
            directory = (self._directory / chat_id).resolve()
            root = self._directory.resolve()
            if directory == root or root not in directory.parents:
                raise AgentError(f"拒绝删除会话目录之外的路径: {directory}")
            shutil.rmtree(directory)

    def send(
        self, chat_id: str, text: str, *, agent: str | None = None
    ) -> ChatRecord:
        """One user turn: appends the message, loops model+tools until the
        model answers in text, persists the whole exchange. With `agent` the
        same loop runs on that external worker instead of the local brain —
        the cloud CLI picks the tool calls, this process executes them."""
        client = self._worker_client(agent) if agent is not None else self._client
        with self._lock:
            record = self._require(chat_id)
        messages = [*record.messages, _message("user", text)]
        tools = self._toolbox.definitions()
        ran_tools = False
        for _ in range(_MAX_TOOL_ROUNDS):
            try:
                reply = client.chat(self._conversation(messages), tools=tools)
            except AgentUnavailable:
                if not ran_tools:
                    # 一轮工具都没跑:不落盘,对外表现和大脑掉线一样。
                    raise
                # 工具已经排过任务:宁可留下半程记录,也不能丢任务 id。
                messages.append(
                    _message(
                        "assistant",
                        "(大脑中途失联,这一轮先停在这里;"
                        "上面工具结果里的任务 id 仍然有效,可继续查询)",
                        agent=agent,
                    )
                )
                break
            tool_calls = reply.get("tool_calls")
            assistant = _message(
                "assistant",
                str(reply.get("content") or ""),
                tool_calls=tool_calls if isinstance(tool_calls, list) else None,
                agent=agent,
            )
            messages.append(assistant)
            if not assistant.tool_calls:
                break
            for call in assistant.tool_calls:
                function = call.get("function")
                name = ""
                arguments: dict[str, Any] = {}
                if isinstance(function, dict):
                    name = str(function.get("name") or "")
                    raw = function.get("arguments")
                    if isinstance(raw, dict):
                        arguments = raw
                    elif isinstance(raw, str):
                        try:
                            parsed = json.loads(raw)
                            if isinstance(parsed, dict):
                                arguments = parsed
                        except json.JSONDecodeError:
                            pass
                result = self._toolbox.call(name, arguments)
                ran_tools = True
                messages.append(
                    _message(
                        "tool",
                        json.dumps(result, ensure_ascii=False),
                        tool_name=name or "unknown",
                    )
                )
        else:
            messages.append(
                _message(
                    "assistant",
                    "工具调用轮数超限,先停在这里;请把需求拆小一点再继续。",
                    agent=agent,
                )
            )
        with self._lock:
            current = self._require(chat_id)
            updated = current.model_copy(
                update={
                    "messages": messages,
                    "title": _title(current, text),
                    "updated_at": _now(),
                }
            )
            self._save(updated)
            return updated

    def _system_prompt(self) -> str:
        limit = self._config.renderer.max_seconds
        lines = [
            "你是一个本地视频创作平台的 Agent;所有生成(视频/图片/配音)都在",
            "用户这台电脑上用本机模型完成,你负责规划并调用平台工具。",
            "你可以写分镜脚本、排图片/配音任务、排 H3 视频渲染、查看资产。",
            "生成类工具只是排队,立即返回任务 id;告诉用户 id,别谎称已完成,",
            "用户追问进度时用 check_job/check_render 查。",
            "任务失败时先用 inspect_failure 拿诊断上下文(报错、参数、",
            "ComfyUI 状态),分析原因并给出具体修复步骤。",
            "先弄清需求再动手;大的创作先给方案,用户点头再排任务。",
            "只在用户明确要求记住偏好时调用 save_memory。",
            "回答用用户的语言,默认中文。",
            "",
            "本机硬性红线(实测,超限会 OOM 击杀渲染后端):分辨率最高 864x480。",
            f"story 分镜总长可到 {self._config.renderer.max_total_seconds:g} 秒,",
            f"渲染器会自动拆成 ≤{limit:g} 秒的块串行渲染再拼接;其他模式单条",
            f" ≤{limit:g} 秒。用户要 1080p/4k 时照常用 864x480 渲染,",
            "compose_video 传 upscale_height=1080 在成片阶段放大。",
            "",
            "写视频画面提示词(generate_video 的 prompt)时:每段两到四句,写具体:",
            "先主体和动作(谁、在哪、做什么),镜头运动用类型+幅度+速度",
            "(如\"缓慢小幅推近\"),再一笔光线氛围。不要让画面里出现文字、字幕、",
            "Logo、水印——字幕和片名用 compose_video 烧进成片;",
            "同一角色或场景跨段出现时,每段用同一组关键词描述外观、服装、色调,",
            "否则各段是独立渲染的,角色会长得不一样。",
            "H3 会原生生成音频:段内可写\"音效:具体声音\"(如雨声、金属剐蹭),",
            "开头无时间戳的全局行可写\"配乐:配器/速度/节奏\";写了才有指挥,",
            "不写模型就自己瞎配。",
            "某一镜是上一镜同一动作的无缝延续时,时间轴写成 [12s-18s续]:",
            "跨块渲染会用上一镜的最后一帧接力,动作不断线;场景切换别标。",
            "角色要跨条锁脸时用 r2v 模式:ref_assets 给资产中心的定妆图 id,",
            "提示词里按序用 <Picture 1> 引用它(如\"<Picture 1> 中的红衣女子",
            "走进咖啡馆\");首次出现描述一次外观,之后只用标签。",
        ]
        preferences = self._memory.texts()
        if preferences:
            lines += ["", "用户的长期偏好:"]
            lines += [f"- {text}" for text in preferences]
        return "\n".join(lines)

    def _require(self, chat_id: str) -> ChatRecord:
        if not _CHAT_ID.fullmatch(chat_id):
            raise ChatNotFound("chat id 无效")
        path = self._directory / chat_id / "chat.json"
        record = self._read(path) if path.is_file() else None
        if record is None:
            raise ChatNotFound(f"会话 {chat_id} 不存在")
        return record

    def _read(self, path: Path) -> ChatRecord | None:
        try:
            return ChatRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self, record: ChatRecord) -> None:
        write_json_atomic(
            self._directory / record.chat_id / "chat.json",
            record.model_dump(mode="json"),
        )


def _message(
    role: Literal["system", "user", "assistant", "tool"],
    content: str,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    tool_name: str | None = None,
    agent: str | None = None,
) -> ChatMessage:
    return ChatMessage(
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_name=tool_name,
        agent=agent,
        created_at=_now(),
    )


def _title(record: ChatRecord, text: str) -> str:
    if record.messages or record.title != "新对话":
        return record.title
    cleaned = " ".join(text.split())
    return cleaned[:40] or record.title


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        # Surfaces as a tool error the model can read and correct.
        raise ValueError(f"不是整数: {value!r}")
    return int(value)


def _mint_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"{prefix}_{stamp}_{secrets.token_hex(3)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
