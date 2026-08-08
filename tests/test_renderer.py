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


# The storyboard case is headerless on purpose: that is the branch where the
# shot inherits its length from the request and so round-trips the duration.
@pytest.mark.parametrize("mode,prompt", [("t2v", "山谷"), ("story", "山谷")])
def test_the_longest_legal_render_survives_being_validated_twice(
    mode: str, prompt: str
) -> None:
    # submit() writes the validated duration back onto the spec and the worker
    # validates that spec again, so max_seconds has to be judged on the frame
    # grid: 15s snaps up to 362 frames, which is 15.08s.
    config = project_config()
    first = validate_render(
        request(mode=mode, prompt=prompt, seconds=15.0),
        config=config,
        has_first_frame=False,
        has_last_frame=False,
    )

    second = validate_render(
        request(mode=mode, prompt=prompt, seconds=first.seconds),
        config=config,
        has_first_frame=False,
        has_last_frame=False,
    )

    assert first.seconds == pytest.approx(362 / 24)
    assert second.seconds == pytest.approx(first.seconds)


def test_durations_outside_the_trained_range_are_still_refused() -> None:
    config = project_config()

    for seconds in (4.0, 16.0):
        with pytest.raises(RenderError, match="时长要在"):
            validate_render(
                request(seconds=seconds),
                config=config,
                has_first_frame=False,
                has_last_frame=False,
            )


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


def test_progress_tracker_reports_one_monotonic_bar_across_shots() -> None:
    tracker = ProgressTracker(passes=2)
    values = [
        tracker.update(ProgressEvent(node="5", value=step, maximum=10)).percent
        for step in (1, 10, 1, 10)
    ]

    assert values == sorted(values)
    assert values[-1] == pytest.approx(0.99)


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
