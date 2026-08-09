# The platform's brain: a local tool-calling agent over Ollama's /api/chat.
# The user talks, the model plans, and every tool it may call is one of this
# machine's own capabilities — submit an image or TTS job, queue an H3 render,
# draft a storyboard, browse assets, remember a preference. Nothing here
# touches a cloud API.
#
# Long generations never block a chat turn: generation tools SUBMIT work and
# return a job/render id immediately; the model reports the id and can check
# on it in a later turn with the status tools. A turn is therefore bounded by
# Ollama's own thinking time plus a handful of fast local calls.
#
# Sessions are durable: work/.chats/<chat_id>/chat.json, same atomic style as
# every other store.

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

from videogen_service.artifacts import write_json_atomic
from videogen_service.assets import AssetStore
from videogen_service.config import ServiceConfig
from videogen_service.jobs import JobError, JobNotFound, JobService, JobSpec
from videogen_service.memory import MemoryStore
from videogen_service.models import RenderSpec, ScriptOptions
from videogen_service.renderer import RenderError
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
_MAX_TURN_MESSAGES = 60  # context sent to Ollama per turn, newest kept


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
                "分镜长片用 story 模式,prompt 里写 [0s-6s] 时间轴;"
                "图生视频用 i2v 并给 first_frame_asset。",
                {
                    "mode": {**text, "description": "t2v/i2v/flf2v/story"},
                    "prompt": text,
                    "seconds": {"type": "number"},
                    "width": integer,
                    "height": integer,
                    "first_frame_asset": {**text, "description": "资产 id,i2v/flf2v 用"},
                    "last_frame_asset": {**text, "description": "资产 id,flf2v 用"},
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
                "check_job",
                "查一条图片/配音任务的状态。",
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
            "write_storyboard": self._write_storyboard,
            "check_job": self._check_job,
            "check_render": self._check_render,
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
        view = self._renders.submit(
            spec,
            first_frame=self._asset_frame(arguments.get("first_frame_asset")),
            last_frame=self._asset_frame(arguments.get("last_frame_asset")),
        )
        return {"render_id": view.render_id, "status": view.status}

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
    ) -> None:
        self._config = config
        self._toolbox = toolbox
        self._client = client or OllamaChatClient(config)
        self._memory = memory or MemoryStore(config.work_dir)
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

    def send(self, chat_id: str, text: str) -> ChatRecord:
        """One user turn: appends the message, loops model+tools until the
        model answers in text, persists the whole exchange."""
        with self._lock:
            record = self._require(chat_id)
        messages = [*record.messages, _message("user", text)]
        tools = self._toolbox.definitions()
        for _ in range(_MAX_TOOL_ROUNDS):
            reply = self._client.chat(
                self._conversation(messages), tools=tools
            )
            tool_calls = reply.get("tool_calls")
            assistant = _message(
                "assistant",
                str(reply.get("content") or ""),
                tool_calls=tool_calls if isinstance(tool_calls, list) else None,
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
        lines = [
            "你是一个本地视频创作平台的 Agent,运行在用户自己的电脑上,",
            "所有生成都用本机模型完成,不经过任何云端 API。",
            "你可以写分镜脚本、排图片/配音任务、排 H3 视频渲染、查看资产。",
            "生成类工具只是排队,立即返回任务 id;告诉用户 id,别谎称已完成,",
            "用户追问进度时用 check_job/check_render 查。",
            "先弄清需求再动手;大的创作先给方案,用户点头再排任务。",
            "只在用户明确要求记住偏好时调用 save_memory。",
            "回答用用户的语言,默认中文。",
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
) -> ChatMessage:
    return ChatMessage(
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_name=tool_name,
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
