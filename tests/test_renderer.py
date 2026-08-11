import json
from pathlib import Path

import pytest

from videogen_service.comfyui import ProgressEvent, load_workflow, patch_workflow
from videogen_service.config import ServiceConfig, load_config
from videogen_service.models import RenderRequest
from videogen_service.renderer import (
    DIRECTOR_TASK_TYPE,
    H3Renderer,
    ProgressTracker,
    RenderError,
    align_dimension,
    align_length_frames,
    build_timeline,
    build_values,
    concat_command,
    job_seed,
    mode_capability_schema,
    parse_storyboard,
    split_board,
    storyboard_seconds,
    validate_render,
)


def project_config(**renderer_overrides: object) -> ServiceConfig:
    root = Path(__file__).parents[1]
    config = load_config(root / "config.example.yaml")
    renderer = config.renderer.model_copy(update=renderer_overrides)
    ollama = config.ollama.model_copy(update={"unload_before_render": False})
    return config.model_copy(
        update={"renderer": renderer, "ollama": ollama}
    )


def request(**overrides: object) -> RenderRequest:
    values: dict[str, object] = {
        "render_id": "vg_abc123",
        "mode": "t2v",
        "prompt": "晨雾里的山谷",
        "width": 864,
        "height": 480,
        "seconds": 5.0,
        "seed": None,
        "first_frame": None,
        "last_frame": None,
    }
    values.update(overrides)
    return RenderRequest.model_validate(values)


@pytest.mark.parametrize("seconds", [5.0, 5.5, 9.9, 15.0])
def test_length_alignment_never_returns_less_than_requested(seconds: float) -> None:
    frames = align_length_frames(seconds, fps=24)

    assert (frames - 5) % 17 == 0
    assert frames >= seconds * 24


def test_dimensions_snap_to_the_comfyui_grid() -> None:
    assert align_dimension(1080) == 1088
    assert align_dimension(1920) == 1920
    assert align_dimension(100) == 96


def test_seed_is_stable_per_render() -> None:
    assert job_seed("vg_abc123") == job_seed("vg_abc123")
    assert job_seed("vg_abc123") != job_seed("vg_other")


def test_storyboard_builds_director_segments() -> None:
    config = project_config()
    board = parse_storyboard(
        "胶片质感\n[0s-5s] 山谷\n[5s-11s] 树冠", default_seconds=5
    )
    timeline = build_timeline(board, config=config, width=864, height=480)

    assert timeline["global"]["taskType"] == DIRECTOR_TASK_TYPE
    assert [segment["start"] for segment in timeline["segments"]] == [0, 124]
    assert [segment["prompt"] for segment in timeline["segments"]] == [
        "胶片质感\n\n山谷",
        "胶片质感\n\n树冠",
    ]
    assert timeline["output"]["exportMode"] == "all"


def test_long_storyboards_split_into_render_sized_chunks() -> None:
    # 6 秒补齐 158 帧,15 秒包络补齐 362 帧:一块装得下两段,第三段开新块。
    board = parse_storyboard(
        "统一风格\n[0s-6s] a\n[6s-12s] b\n[12s-18s] c\n[18s-24s] d",
        default_seconds=5,
    )
    plans = split_board(board, config=project_config())

    assert [len(plan.board.shots) for plan in plans] == [2, 2]
    assert all(not plan.anchored for plan in plans)
    assert all(plan.board.preamble == "统一风格" for plan in plans)
    assert [shot.prompt for plan in plans for shot in plan.board.shots] == [
        "a",
        "b",
        "c",
        "d",
    ]


def test_short_storyboards_stay_one_chunk() -> None:
    board = parse_storyboard("[0s-6s] a\n[6s-12s] b", default_seconds=5)
    assert len(split_board(board, config=project_config())) == 1


def test_continuation_marker_parses_and_rides_the_shot() -> None:
    board = parse_storyboard(
        "[0s-6s] a\n[6s-12s续] b\n[12s-18s 接] c", default_seconds=5
    )
    assert [shot.cont for shot in board.shots] == [False, True, True]
    assert [shot.prompt for shot in board.shots] == ["a", "b", "c"]
    assert [shot.seconds for shot in board.shots] == [6.0, 6.0, 6.0]


def test_continuation_at_chunk_boundary_becomes_an_anchored_chunk() -> None:
    # a+b 装满一块;c 带续标记落在边界 → 单独成接力块;d 正常开新块。
    board = parse_storyboard(
        "[0s-6s] a\n[6s-12s] b\n[12s-18s续] c\n[18s-24s] d", default_seconds=5
    )
    plans = split_board(board, config=project_config())

    assert [len(plan.board.shots) for plan in plans] == [2, 1, 1]
    assert [plan.anchored for plan in plans] == [False, True, False]
    assert plans[1].board.shots[0].prompt == "c"


def test_continuation_inside_a_chunk_needs_no_anchor() -> None:
    board = parse_storyboard("[0s-6s] a\n[6s-12s续] b", default_seconds=5)
    plans = split_board(board, config=project_config())
    assert [plan.anchored for plan in plans] == [False]


def test_continuation_on_the_first_shot_is_ignored() -> None:
    board = parse_storyboard("[0s-15s续] a\n[15s-30s] b", default_seconds=5)
    plans = split_board(board, config=project_config())
    assert [plan.anchored for plan in plans] == [False, False]


def test_capability_schema_exposes_chunk_reality() -> None:
    # 拆块的事实(每块段数上限、边界硬切、没有自动跨块锚)必须进
    # 能力表:agent 的规划靠它,不能再把拆块当透明实现细节。
    config = project_config()
    schema = mode_capability_schema(config.modes["story"], config=config)

    chunk = schema["chunk"]
    assert chunk["seconds_max"] == config.renderer.max_seconds
    assert chunk["max_shots_per_chunk"] == 2
    assert "硬切" in chunk["note"]
    assert not schema["accepts_segment_refs"]


def test_refs_sink_into_every_segment_for_r2v() -> None:
    # 节点核查(2026-08-10):editMode=segment 下段从不继承 global.refs,
    # 只写 global 的定妆等于没给——refs 必须下沉到每一段。
    board = parse_storyboard(
        "[0s-6s] <Picture 1> 的女子\n[6s-12s] <Picture 1> 的女子回头",
        default_seconds=5,
    )
    timeline = build_timeline(
        board,
        config=project_config(),
        width=864,
        height=480,
        refs_b64=["QUJD", "REVG"],
        task_type="r2v",
    )
    assert timeline["global"]["taskType"] == "r2v"
    for segment in timeline["segments"]:
        assert segment["refs"] == [
            {"index": 0, "imageB64": "QUJD"},
            {"index": 1, "imageB64": "REVG"},
        ]
    # global.refs 保留作对账,但真正生效的是段上的。
    assert timeline["global"]["refs"] == [
        {"index": 0, "imageB64": "QUJD"},
        {"index": 1, "imageB64": "REVG"},
    ]


def test_segment_refs_number_after_identity_refs() -> None:
    # 定妆图占 <Picture 1..N>,风格钥匙等段级参考接在其后,
    # 提示词里的既有引用不因加风格钥匙而错位。
    board = parse_storyboard("[0s-6s] <Picture 1> 的女子", default_seconds=5)
    timeline = build_timeline(
        board,
        config=project_config(),
        width=864,
        height=480,
        refs_b64=["QUJD"],
        segment_refs_b64=["U1RZTEU="],
        task_type="r2v",
    )
    assert timeline["segments"][0]["refs"] == [
        {"index": 0, "imageB64": "QUJD"},
        {"index": 1, "imageB64": "U1RZTEU="},
    ]


def test_reference_validation_gates_by_mode() -> None:
    config = project_config()
    r2v_mode = config.modes["story"].model_copy(update={"accepts_refs": True})
    with_r2v = config.model_copy(update={"modes": {**config.modes, "r2v": r2v_mode}})

    with pytest.raises(RenderError, match="不接参考图"):
        validate_render(
            request(mode="story", prompt="[0s-6s] a"),
            config=config,
            has_first_frame=False,
            has_last_frame=False,
            ref_count=1,
        )
    with pytest.raises(RenderError, match="至少一张参考图"):
        validate_render(
            request(mode="r2v", prompt="[0s-6s] a"),
            config=with_r2v,
            has_first_frame=False,
            has_last_frame=False,
            ref_count=0,
        )
    with pytest.raises(RenderError, match="最多 9 张"):
        validate_render(
            request(mode="r2v", prompt="[0s-6s] a"),
            config=with_r2v,
            has_first_frame=False,
            has_last_frame=False,
            ref_count=10,
        )
    accepted = validate_render(
        request(mode="r2v", prompt="[0s-6s] a"),
        config=with_r2v,
        has_first_frame=False,
        has_last_frame=False,
        ref_count=2,
    )
    assert accepted.timeline_mode


def test_segment_refs_ride_into_every_segment() -> None:
    board = parse_storyboard("[0s-6s] 甲\n[6s-12s] 乙", default_seconds=6)
    timeline = build_timeline(
        board,
        config=project_config(),
        width=864,
        height=480,
        segment_refs_b64=["U1RZTEU="],
    )
    for segment in timeline["segments"]:
        assert segment["refs"] == [{"index": 0, "imageB64": "U1RZTEU="}]
    # 每段拿到独立的字典,改一段不串味。
    timeline["segments"][0]["refs"][0]["imageB64"] = "X"
    assert timeline["segments"][1]["refs"][0]["imageB64"] == "U1RZTEU="


def test_segment_reference_validation_gates() -> None:
    # 节点只让 r2v 段消费参考图:story(t2v 段)挂段级参考是无效操作,
    # 必须在提交时拒绝,不能让用户以为风格钥匙生效了。
    config = project_config()
    r2v_mode = config.modes["story"].model_copy(update={"accepts_refs": True})
    with_r2v = config.model_copy(update={"modes": {**config.modes, "r2v": r2v_mode}})

    with pytest.raises(RenderError, match="只在 r2v 分镜"):
        validate_render(
            request(mode="t2v", prompt="a", seconds=6),
            config=config,
            has_first_frame=False,
            has_last_frame=False,
            segment_ref_count=1,
        )
    with pytest.raises(RenderError, match="只在 r2v 分镜"):
        validate_render(
            request(mode="story", prompt="[0s-6s] a"),
            config=config,
            has_first_frame=False,
            has_last_frame=False,
            segment_ref_count=1,
        )
    with pytest.raises(RenderError, match="参考图合计最多"):
        validate_render(
            request(mode="r2v", prompt="[0s-6s] <Picture 1> a"),
            config=with_r2v,
            has_first_frame=False,
            has_last_frame=False,
            ref_count=2,
            segment_ref_count=8,
        )
    accepted = validate_render(
        request(mode="r2v", prompt="[0s-6s] <Picture 1> a"),
        config=with_r2v,
        has_first_frame=False,
        has_last_frame=False,
        ref_count=1,
        segment_ref_count=2,
    )
    assert accepted.timeline_mode


def test_anchored_timeline_switches_to_gen_image_mode() -> None:
    board = parse_storyboard("风格\n[0s-6s续] 接着跑", default_seconds=5)
    timeline = build_timeline(
        board, config=project_config(), width=864, height=480, anchor_b64="UE5H"
    )

    assert timeline["timelineMode"] == "gen_image"
    assert timeline["global"]["taskType"] == "i2v"
    segment = timeline["segments"][0]
    assert segment["taskType"] == "i2v"
    assert segment["genImage"] == {"imageB64": "UE5H"}


def test_chunk_concat_copies_streams_without_reencoding() -> None:
    command = concat_command("list.txt", "joined.mp4")
    assert command[0] == "ffmpeg"
    assert command[command.index("-f") + 1] == "concat"
    assert command[command.index("-c") + 1] == "copy"
    assert command[-1] == "joined.mp4"


def test_continuity_flag_rides_from_config_into_the_timeline() -> None:
    board = parse_storyboard("[0s-5s] a\n[5s-10s] b", default_seconds=5)
    off = build_timeline(board, config=project_config(), width=864, height=480)
    on = build_timeline(
        board,
        config=project_config(continuous_reference=True),
        width=864,
        height=480,
    )
    assert off["global"]["continuousReference"] is False
    assert on["global"]["continuousReference"] is True


def test_storyboard_duration_is_aligned_per_shot() -> None:
    board = parse_storyboard("[0s-5s] a\n[5s-10s] b", default_seconds=5)

    assert storyboard_seconds(board, fps=24) == pytest.approx(248 / 24)


def test_invalid_storyboard_is_refused_before_rendering() -> None:
    config = project_config(max_shots=1)

    with pytest.raises(RenderError, match="最多 1 段"):
        validate_render(
            request(mode="story", prompt="[0s-5s] a\n[5s-10s] b"),
            config=config,
            has_first_frame=False,
            has_last_frame=False,
        )


def test_storyboard_values_carry_timeline_instead_of_uploaded_frames() -> None:
    config = project_config()
    values = build_values(
        request(mode="story", prompt="[0s-5s] 山谷"),
        config=config,
        uploaded={"first_frame": "ignored.png"},
    )

    timeline = json.loads(values["timeline"])
    assert values["length"] == timeline["totalFrames"] == 124
    assert "first_frame" not in values


def test_storyboard_without_a_preamble_keeps_timecodes_out_of_global_prompt() -> None:
    config = project_config()
    values = build_values(
        request(mode="story", prompt="[0s-5s] 山谷\n[5s-10s] 树冠"),
        config=config,
        uploaded={},
    )

    timeline = json.loads(values["timeline"])
    assert values["prompt"] == ""
    assert [segment["prompt"] for segment in timeline["segments"]] == ["山谷", "树冠"]


def test_storyboard_preamble_is_the_only_global_prompt() -> None:
    config = project_config()
    values = build_values(
        request(mode="story", prompt="胶片质感\n[0s-5s] 山谷\n[5s-10s] 树冠"),
        config=config,
        uploaded={},
    )

    assert values["prompt"] == "胶片质感"


def test_progress_tracker_reports_one_monotonic_bar_across_shots() -> None:
    tracker = ProgressTracker(passes=2)
    reports = [
        tracker.update(ProgressEvent(node="5", value=step, maximum=10))
        for step in (1, 10, 1, 10)
    ]

    assert [report.percent for report in reports] == pytest.approx(
        [0.05, 0.5, 0.55, 0.99]
    )
    assert [report.pass_index for report in reports] == [1, 1, 2, 2]


def test_progress_tracker_counts_a_pass_when_the_sampler_node_changes() -> None:
    tracker = ProgressTracker(passes=2)
    reports = [
        tracker.update(ProgressEvent(node=node, value=step, maximum=10))
        for node, step in (("5", 1), ("5", 10), ("6", 1), ("6", 10))
    ]

    assert [report.percent for report in reports] == pytest.approx(
        [0.05, 0.5, 0.55, 0.99]
    )
    assert [report.pass_index for report in reports] == [1, 1, 2, 2]


def test_progress_tracker_grows_the_denominator_past_the_expected_passes() -> None:
    tracker = ProgressTracker(passes=1)
    reports = [
        tracker.update(ProgressEvent(node=node, value=10, maximum=10))
        for node in ("5", "6", "7")
    ]

    assert [report.pass_total for report in reports] == [1, 2, 3]
    assert [report.percent for report in reports] == pytest.approx([0.99, 0.99, 0.99])


@pytest.mark.parametrize(
    ("mode", "first", "last", "message"),
    [
        ("t2v", True, False, "不接首帧"),
        ("t2v", False, True, "不接尾帧"),
        ("i2v", True, True, "不接尾帧"),
    ],
)
def test_modes_refuse_reference_frames_they_cannot_patch(
    mode: str, first: bool, last: bool, message: str
) -> None:
    config = project_config()

    with pytest.raises(RenderError, match=message):
        validate_render(
            request(mode=mode),
            config=config,
            has_first_frame=first,
            has_last_frame=last,
        )


def test_frame_modes_still_accept_the_frames_they_declare() -> None:
    config = project_config()

    assert validate_render(
        request(mode="i2v"), config=config, has_first_frame=True, has_last_frame=False
    ).seconds == pytest.approx(5.0)
    assert validate_render(
        request(mode="flf2v"), config=config, has_first_frame=True, has_last_frame=True
    ).seconds == pytest.approx(5.0)


def test_shipped_workflows_satisfy_every_configured_pointer() -> None:
    config = project_config()
    H3Renderer(config)
    for mode, settings in config.modes.items():
        values: dict[str, object] = {
            "prompt": "测试",
            "width": 864,
            "height": 480,
            "length": 124,
            "seed": 42,
        }
        for slot in ("first_frame", "last_frame"):
            if slot in settings.inputs:
                values[slot] = f"{slot}.png"
        if "timeline" in settings.inputs:
            values["timeline"] = json.dumps(
                build_timeline(
                    parse_storyboard("[0s-5s] 测试", default_seconds=5),
                    config=config,
                    width=864,
                    height=480,
                ),
                ensure_ascii=False,
            )
        patched = patch_workflow(
            load_workflow(settings.workflow_file),
            pointers=settings.inputs,
            values=values,
        )

        assert any(node.get("class_type") == "SaveVideo" for node in patched.values()), mode
