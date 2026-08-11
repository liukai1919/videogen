# 白板手绘动画能力:标注校验拦在提交口,渲染走 vendored 引擎子进程。
# 端到端渲染用最小画布跑真引擎,依赖(cv2)缺席时跳过而不是失败——
# 与 preflight 的降级口径一致。

import json
from pathlib import Path

import pytest

from videogen_service.assets import AssetStore
from videogen_service.jobs import (
    WHITEBOARD_CAPABILITY_ID,
    WHITEBOARD_ENGINE_DIR,
    JobError,
    JobService,
    JobSpec,
    LocalJobRunner,
    WhiteboardCapability,
    whiteboard_annotation,
)

from tests.test_jobs import FakeRunner, make_config


def _annotation(*, canvas: int = 96) -> dict[str, object]:
    return {
        "sceneId": "scene-01",
        "canvas": {"width": canvas, "height": canvas},
        "sceneDurationMs": 900,
        "elements": [
            {
                "id": "line-1",
                "label": "一条线",
                "sequence": 1,
                "type": "structure",
                "region": {"x": 8, "y": 8, "width": canvas - 16, "height": canvas - 16},
                "reveal": {
                    "direction": "top_to_bottom",
                    "startMs": 0,
                    "durationMs": 400,
                    "maskPaddingPx": 4,
                    "protectedRegions": [],
                },
            }
        ],
    }


def _spec(**overrides: object) -> JobSpec:
    values: dict[str, object] = {
        "job_id": "wb-1",
        "capability": WHITEBOARD_CAPABILITY_ID,
        "asset_id": "asset-1",
        "annotation": json.dumps(_annotation()),
    }
    values.update(overrides)
    return JobSpec.model_validate(values)


def test_annotation_validator_accepts_wellformed() -> None:
    document = whiteboard_annotation(json.dumps(_annotation()))
    assert isinstance(document["elements"], list)


@pytest.mark.parametrize(
    "text, message",
    [
        (None, "需要 annotation"),
        ("not json", "不是合法 JSON"),
        ("[]", "必须是 JSON 对象"),
        ("{}", "elements 必须是非空数组"),
        ('{"elements": [{"reveal": {}}]}', "startMs/durationMs"),
    ],
)
def test_annotation_validator_rejects(text: str | None, message: str) -> None:
    with pytest.raises(JobError, match=message):
        whiteboard_annotation(text)


def test_submit_validates_whiteboard_inputs(tmp_path: Path) -> None:
    service = JobService(make_config(tmp_path), runner=FakeRunner())
    try:
        with pytest.raises(JobError, match="asset_id"):
            service.submit(_spec(asset_id=None))
        with pytest.raises(JobError, match="elements"):
            service.submit(_spec(annotation="{}"))
    finally:
        service.close()


def test_runner_requires_asset_store(tmp_path: Path) -> None:
    runner = LocalJobRunner(make_config(tmp_path))
    with pytest.raises(JobError, match="资产库"):
        runner.run(
            _spec(), WhiteboardCapability(), directory=tmp_path / "job"
        )


def test_runner_reports_missing_asset(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    runner = LocalJobRunner(config, assets=AssetStore(config.work_dir))
    with pytest.raises(JobError, match="asset-1"):
        runner.run(
            _spec(), WhiteboardCapability(), directory=tmp_path / "job"
        )


def test_whiteboard_renders_scene(tmp_path: Path) -> None:
    cv2 = pytest.importorskip("cv2")
    numpy = pytest.importorskip("numpy")
    script = WHITEBOARD_ENGINE_DIR / "scripts" / "render_stream_whiteboard.py"
    assert script.is_file(), "vendor/srt-whiteboard 应随仓库在位"

    canvas = numpy.full((96, 96, 3), 245, dtype=numpy.uint8)
    cv2.line(canvas, (10, 10), (86, 86), (40, 40, 40), 3)
    ok, encoded = cv2.imencode(".png", canvas)
    assert ok

    config = make_config(tmp_path)
    assets = AssetStore(config.work_dir)
    record = assets.create(
        name="线稿", category="参考帧", filename="line.png", data=encoded.tobytes()
    )
    runner = LocalJobRunner(config, assets=assets)
    directory = config.work_dir / ".jobs" / "wb-1"
    output = runner.run(
        _spec(asset_id=record.asset_id),
        WhiteboardCapability(),
        directory=directory,
    )
    # 有 ffmpeg 时是转码产物,没有时引擎保留 mp4v 原始文件,两者都算成功。
    assert output in {"whiteboard.mp4", "whiteboard_raw.mp4"}
    produced = directory / output
    assert produced.is_file() and produced.stat().st_size > 0
