import json
from pathlib import Path
import time
from typing import Any, Callable

from fastapi.testclient import TestClient

from videogen_service.api import create_app
from videogen_service.artifacts import write_bytes_atomic
from videogen_service.assets import AssetStore
from videogen_service.config import Capability, ServiceConfig
from videogen_service.jobs import JobSpec
from videogen_service.memory import MemoryStore
from videogen_service.models import RenderProgress, RenderRequest, ScriptRequest, ScriptResult, ScriptShot, ScriptSource


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
                "t2v": {
                    "workflow_file": str(tmp_path / "t2v.json"),
                    "inputs": {"prompt": ["1.text"]},
                },
                "story": {
                    "workflow_file": str(tmp_path / "story.json"),
                    "inputs": {"prompt": ["1.text"], "timeline": ["1.timeline"]},
                },
                "r2v": {
                    "workflow_file": str(tmp_path / "r2v.json"),
                    "accepts_refs": True,
                    "inputs": {"prompt": ["1.text"], "timeline": ["1.timeline"]},
                },
            },
            "capabilities": {
                "flux-t2i": {
                    "kind": "t2i",
                    "workflow_file": str(tmp_path / "flux.json"),
                    "inputs": {"prompt": ["6.text"]},
                },
            },
        }
    )


class FakeRenderer:
    def __init__(self) -> None:
        self.requests: list[RenderRequest] = []

    def render(
        self,
        request: RenderRequest,
        *,
        on_progress: Callable[[RenderProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        self.requests.append(request)
        return b"FAKE-MP4"


class FakeRunner:
    def run(self, spec: JobSpec, capability: Capability, *, directory: Path) -> str:
        if getattr(capability, "kind", "") == "review":
            write_bytes_atomic(
                directory / "review.json",
                json.dumps(
                    {
                        "hook": 7,
                        "consistency": 9,
                        "visual_quality": 8,
                        "alignment": 8,
                        "notes": "跨段色调统一,开场略平。",
                    },
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            return "review.json"
        write_bytes_atomic(directory / "output.png", b"PNG")
        return "output.png"


class StubStudio:
    def create(self, request: ScriptRequest) -> ScriptResult:
        raise AssertionError("agent tests never fetch captions")

    def create_from_document(self, options: Any, *, filename: str, text: str) -> ScriptResult:
        return ScriptResult(
            source={"kind": "document", "filename": filename, "chars": len(text), "truncated": False},
            title="想法成片",
            summary="从想法生成的脚本。",
            mode="story",
            prompt="科普短片。\n\n[0s-6s] 想法画面",
            seconds=6.0,
            shots=[ScriptShot(seconds=6, prompt="想法画面", narration="解说")],
        )


class ScriptedChat:
    """Plays back a fixed sequence of assistant messages, recording every
    request the agent sends to the model."""

    def __init__(self, replies: list[dict[str, Any]]) -> None:
        self.replies = list(replies)
        self.requests: list[list[dict[str, Any]]] = []
        self.tools_seen: list[list[dict[str, Any]]] = []

    def chat(
        self, messages: list[dict[str, Any]], *, tools: list[dict[str, Any]]
    ) -> dict[str, Any]:
        self.requests.append(messages)
        self.tools_seen.append(tools)
        if not self.replies:
            return {"content": "(没有更多台词了)"}
        return self.replies.pop(0)


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {"function": {"name": name, "arguments": arguments}}


def make_client(tmp_path: Path, chat: ScriptedChat) -> TestClient:
    return TestClient(
        create_app(
            make_config(tmp_path),
            renderer=FakeRenderer(),
            studio=StubStudio(),
            job_runner=FakeRunner(),
            chat_client=chat,
        )
    )


def test_a_chat_is_created_listed_and_deleted(tmp_path: Path) -> None:
    with make_client(tmp_path, ScriptedChat([])) as client:
        created = client.post("/v1/chats", json={"title": "宠物短片"})
        assert created.status_code == 201
        chat_id = created.json()["chat_id"]

        listed = client.get("/v1/chats").json()
        assert [chat["chat_id"] for chat in listed] == [chat_id]
        assert listed[0]["title"] == "宠物短片"

        assert client.delete(f"/v1/chats/{chat_id}").status_code == 204
        assert client.get(f"/v1/chats/{chat_id}").status_code == 404


def test_a_plain_answer_needs_no_tools(tmp_path: Path) -> None:
    chat = ScriptedChat([{"content": "你好,想做什么样的视频?"}])
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "你好"}
        ).json()

    roles = [message["role"] for message in record["messages"]]
    assert roles == ["user", "assistant"]
    assert record["messages"][-1]["content"] == "你好,想做什么样的视频?"
    # The system prompt rides in the request but is not stored in the record.
    assert chat.requests[0][0]["role"] == "system"
    assert "本地" in chat.requests[0][0]["content"]
    assert record["title"] == "你好"


def test_a_tool_call_runs_and_its_result_feeds_back(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_image",
                        {"capability": "flux-t2i", "prompt": "深蓝网络"},
                    )
                ],
            },
            {"content": "图片任务已排队,可以随时问我进度。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "画一张深蓝网络"}
        ).json()

        roles = [message["role"] for message in record["messages"]]
        assert roles == ["user", "assistant", "tool", "assistant"]
        result = json.loads(record["messages"][2]["content"])
        assert result["status"] in {"QUEUED", "RUNNING", "DONE"}
        job_id = result["job_id"]

        # The submitted job is real and finishes on the job queue.
        deadline = time.monotonic() + 3
        state = client.get(f"/v1/jobs/{job_id}").json()
        while state["status"] != "DONE" and time.monotonic() < deadline:
            time.sleep(0.01)
            state = client.get(f"/v1/jobs/{job_id}").json()
        assert state["status"] == "DONE"

    # The second model round saw the tool result in its context.
    tool_rounds = chat.requests[1]
    assert tool_rounds[-1]["role"] == "tool"
    assert job_id in tool_rounds[-1]["content"]


def test_storyboard_and_video_tools_compose(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [tool_call("write_storyboard", {"idea": "宠物屋测评"})],
            },
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {"mode": "story", "prompt": "科普短片。\n\n[0s-6s] 想法画面"},
                    )
                ],
            },
            {"content": "分镜写好了,渲染也排上了。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "帮我做一条宠物屋测评短片"},
        ).json()

        board = json.loads(record["messages"][2]["content"])
        assert board["mode"] == "story"
        assert "[0s-6s]" in board["prompt"]

        video = json.loads(record["messages"][4]["content"])
        render = client.get(f"/v1/renders/{video['render_id']}")
        assert render.status_code == 200


def test_the_render_envelope_guard_blocks_oom_specs(tmp_path: Path) -> None:
    # 2026-08-09 的真实死法:1080p 在模型加载阶段 OOM。直渲超限被拦;
    # 长 story 现在合法——渲染器自动拆成安全大小的块,守卫放行。
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {
                            "mode": "t2v",
                            "prompt": "夜景",
                            "width": 1920,
                            "height": 1080,
                        },
                    )
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {"mode": "t2v", "prompt": "夜景", "seconds": 30},
                    )
                ],
            },
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {"mode": "story", "prompt": "[0s-15s] 开场\n[15s-30s] 结尾"},
                    )
                ],
            },
            {"content": "1080p 和 30 秒 t2v 被拦,30 秒 story 已拆块排上。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages",
            json={"text": "来一条 1080p 的 30 秒大片"},
        ).json()
        resolution = json.loads(record["messages"][2]["content"])
        duration = json.loads(record["messages"][4]["content"])
        story = json.loads(record["messages"][6]["content"])
        assert "864x480" in resolution["error"]
        assert "15" in duration["error"]
        assert story.get("error") is None and story["render_id"]
        # Only the chunk-safe story render reached the queue.
        renders = client.get("/v1/renders").json()
        assert [entry["render_id"] for entry in renders] == [story["render_id"]]


def test_r2v_reference_render_carries_asset_images(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    asset = AssetStore(config.work_dir).create(
        name="女主定妆照", category="角色", filename="face.png", data=b"PNG"
    )
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {
                            "mode": "r2v",
                            "prompt": "[0s-6s] <Picture 1> 的红衣女子走进咖啡馆",
                            "ref_assets": [asset.asset_id],
                        },
                    )
                ],
            },
            {"content": "参考渲染排上了。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "用定妆照锁脸生成"}
        ).json()
        video = json.loads(record["messages"][2]["content"])
        assert video.get("error") is None and video["render_id"]

    # The stored request carries the reference image for retries.
    stored = json.loads(
        (config.work_dir / video["render_id"] / "request.json").read_text(
            encoding="utf-8"
        )
    )
    assert [Path(ref).name for ref in stored["refs"]] == ["ref0.png"]


def test_story_render_carries_segment_ref_assets(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    asset = AssetStore(config.work_dir).create(
        name="风格钥匙", category="风格", filename="style.png", data=b"PNG"
    )
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "generate_video",
                        {
                            "mode": "story",
                            "prompt": "[0s-6s] 山谷\n[6s-12s] 峰顶",
                            "segment_ref_assets": [asset.asset_id],
                        },
                    )
                ],
            },
            {"content": "带风格钥匙的渲染排上了。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "全片锁一个风格"}
        ).json()
        video = json.loads(record["messages"][2]["content"])
        assert video.get("error") is None and video["render_id"]

    stored = json.loads(
        (config.work_dir / video["render_id"] / "request.json").read_text(
            encoding="utf-8"
        )
    )
    assert [Path(ref).name for ref in stored["segment_refs"]] == ["segref0.png"]


def test_a_broken_tool_reports_instead_of_crashing(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call("generate_image", {"capability": "nope", "prompt": "x"})
                ],
            },
            {"content": "这个能力不存在,我换个方式。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "画一张图"}
        ).json()
    result = json.loads(record["messages"][2]["content"])
    assert "未配置的能力" in result["error"]


def test_a_looping_model_is_cut_off(tmp_path: Path) -> None:
    reply = {
        "content": "",
        "tool_calls": [tool_call("list_capabilities", {})],
    }
    chat = ScriptedChat([dict(reply) for _ in range(20)])
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "随便"}
        ).json()
    assert "轮数超限" in record["messages"][-1]["content"]


def test_memory_preferences_ride_in_the_system_prompt(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    MemoryStore(config.work_dir).add("旁白都用口语")
    chat = ScriptedChat([{"content": "好的。"}])
    app = create_app(
        config,
        renderer=FakeRenderer(),
        studio=StubStudio(),
        job_runner=FakeRunner(),
        chat_client=chat,
    )
    with TestClient(app) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        client.post(f"/v1/chats/{chat_id}/messages", json={"text": "在吗"})
    assert "旁白都用口语" in chat.requests[0][0]["content"]


def write_routed_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "concept-trailer"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "meta.yaml").write_text(
        "\n".join(
            [
                "name: 概念预告片",
                "description: 电影级预告片质感",
                "use_when:",
                "  - 概念预告片、片花",
                "not_for:",
                "  - 科普内容(用 science-doc)",
                "defaults:",
                "  shot_seconds: 5",
            ]
        ),
        encoding="utf-8",
    )
    (skill_dir / "SKILL.md").write_text(
        "- 三段式节奏;节拍细则见 references/beat.md", encoding="utf-8"
    )
    (skill_dir / "references" / "beat.md").write_text(
        "开场一镜定调,中段冲突升级。", encoding="utf-8"
    )


def test_skill_routing_table_rides_in_the_system_prompt(tmp_path: Path) -> None:
    write_routed_skill(tmp_path)
    chat = ScriptedChat([{"content": "好的。"}])
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        client.post(f"/v1/chats/{chat_id}/messages", json={"text": "在吗"})

    system = chat.requests[0][0]["content"]
    # 路由表:id、触发条件、边界、参考文档名都在;全文不在(两级注入)。
    assert "concept-trailer" in system
    assert "概念预告片、片花" in system
    assert "科普内容(用 science-doc)" in system
    assert "beat.md" in system
    assert "三段式节奏" not in system


def test_read_skill_reference_tool_round_trips(tmp_path: Path) -> None:
    write_routed_skill(tmp_path)
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "read_skill_reference",
                        {"skill_id": "concept-trailer", "name": "beat.md"},
                    )
                ],
            },
            {"content": "细则已读。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "做个预告片"}
        ).json()

    names = [tool["function"]["name"] for tool in chat.tools_seen[0]]
    assert "read_skill_reference" in names
    result = json.loads(record["messages"][2]["content"])
    assert result["content"] == "开场一镜定调,中段冲突升级。"


def test_read_skill_reference_reports_unknowns_readably(tmp_path: Path) -> None:
    write_routed_skill(tmp_path)
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "read_skill_reference",
                        {"skill_id": "concept-trailer", "name": "nope.md"},
                    )
                ],
            },
            {"content": "没有这份文档。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "读细则"}
        ).json()

    result = json.loads(record["messages"][2]["content"])
    assert "nope.md" in result["error"]
    assert "beat.md" in result["error"]


def test_review_video_scores_ride_back_through_check_job(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call("review_video", {"render_id": "r-done"})
                ],
            },
            {"content": "", "tool_calls": []},
        ]
    )
    with make_client(tmp_path, chat) as client:
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "r-done",
                "mode": "t2v",
                "prompt": "雪山",
                "width": 864,
                "height": 480,
                "seconds": 6,
            },
        )
        assert submitted.status_code == 202
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "给成片打个分"}
        ).json()
        review_submit = json.loads(record["messages"][2]["content"])
        job_id = review_submit["job_id"]

        # 评分任务在 FakeRunner 里立即完成;check_job 应带回分数。
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = client.get(f"/v1/jobs/{job_id}").json()
            if state["status"] == "DONE":
                break
            time.sleep(0.01)
        assert state["status"] == "DONE"

        chat2 = ScriptedChat(
            [
                {
                    "content": "",
                    "tool_calls": [tool_call("check_job", {"job_id": job_id})],
                },
                {"content": "分数出来了。"},
            ]
        )
    with make_client(tmp_path, chat2) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "查评分"}
        ).json()
    checked = json.loads(record["messages"][2]["content"])
    assert checked["review"]["consistency"] == 9
    assert "跨段色调统一" in checked["review"]["notes"]


def test_save_render_frame_rejects_bad_slots(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "save_render_frame_as_asset",
                        {"render_id": "r-x", "slot": "middle", "name": "钥匙"},
                    )
                ],
            },
            {"content": "槽位不对。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "存帧"}
        ).json()
    result = json.loads(record["messages"][2]["content"])
    assert "first 或 last" in result["error"]


def test_wait_render_returns_the_terminal_state(tmp_path: Path) -> None:
    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "wait_render",
                        {"render_id": "r-wait", "timeout_seconds": 30},
                    )
                ],
            },
            {"content": "等到了。"},
        ]
    )
    with make_client(tmp_path, chat) as client:
        submitted = client.post(
            "/v1/renders",
            data={
                "render_id": "r-wait",
                "mode": "t2v",
                "prompt": "日出雪山",
                "width": 864,
                "height": 480,
                "seconds": 6,
            },
        )
        assert submitted.status_code == 202
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        record = client.post(
            f"/v1/chats/{chat_id}/messages", json={"text": "等它渲完"}
        ).json()

    result = json.loads(record["messages"][2]["content"])
    assert result["status"] == "DONE"
    assert result.get("timed_out") is None


def test_write_storyboard_forwards_style(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class CapturingStudio(StubStudio):
        def create_from_document(
            self, options: Any, *, filename: str, text: str
        ) -> ScriptResult:
            captured["style"] = options.style
            return super().create_from_document(
                options, filename=filename, text=text
            )

    chat = ScriptedChat(
        [
            {
                "content": "",
                "tool_calls": [
                    tool_call(
                        "write_storyboard",
                        {"idea": "雨夜霓虹下的流浪猫", "style": "电影感雨夜霓虹,冷暖对比"},
                    )
                ],
            },
            {"content": "分镜好了。"},
        ]
    )
    app = create_app(
        make_config(tmp_path),
        renderer=FakeRenderer(),
        studio=CapturingStudio(),
        job_runner=FakeRunner(),
        chat_client=chat,
    )
    with TestClient(app) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        client.post(f"/v1/chats/{chat_id}/messages", json={"text": "做条氛围片"})

    assert captured["style"] == "电影感雨夜霓虹,冷暖对比"


def test_h3_prompt_rules_ride_in_prompt_and_tool_descriptions(
    tmp_path: Path,
) -> None:
    chat = ScriptedChat([{"content": "好的。"}])
    with make_client(tmp_path, chat) as client:
        chat_id = client.post("/v1/chats", json={}).json()["chat_id"]
        client.post(f"/v1/chats/{chat_id}/messages", json={"text": "在吗"})

    system = chat.requests[0][0]["content"]
    # H3 官方对白语法与轮询纪律都写进了系统提示(R1 的 S10/S1 缺口)。
    assert "<d>[Chinese] 台词</d>" in system
    assert "最多查一次进度" in system
    video = next(
        tool
        for tool in chat.tools_seen[0]
        if tool["function"]["name"] == "generate_video"
    )
    # story 每段时长界限进了工具描述(R1 三次踩坑的服务端拦截,前移到这里)。
    assert "5-15 秒之间" in video["function"]["description"]
