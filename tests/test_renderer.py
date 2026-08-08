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
    job_seed,
    parse_storyboard,
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
