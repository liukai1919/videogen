# Project export: one zip holding everything a cut deserves to leave with —
# finished MP4s, an SRT per draft built from the narration timeline, the
# storyboard texts, and a manifest. The zip is the seam to local editors
# (剪映、DaVinci、Premiere all take MP4+SRT); dedicated draft formats can come
# later without changing what is archived here.

from io import BytesIO
import json
import zipfile

from videogen_service.config import ServiceConfig
from videogen_service.models import ScriptShot
from videogen_service.projects import ProjectRecord
from videogen_service.renderer import align_length_frames
from videogen_service.service import RenderNotFound, RenderService


def build_project_export(
    project: ProjectRecord, *, config: ServiceConfig, renders: RenderService
) -> tuple[str, bytes]:
    """Returns (download filename, zip bytes). Renders that are not DONE are
    listed in the manifest as skipped rather than failing the whole export."""
    buffer = BytesIO()
    skipped: list[str] = []
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for draft in project.drafts:
            base = f"drafts/draft-{draft.draft_id:02d}"
            archive.writestr(f"{base}/storyboard.txt", draft.result.prompt)
            narration = "\n".join(
                shot.narration for shot in draft.result.shots if shot.narration
            )
            if narration:
                archive.writestr(f"{base}/narration.txt", narration)
            subtitles = narration_srt(
                draft.result.shots, fps=config.renderer.fps
            )
            if subtitles:
                archive.writestr(f"{base}/narration.srt", subtitles)
        for render_id in project.render_ids:
            try:
                path = renders.media_path(render_id)
            except RenderNotFound:
                skipped.append(render_id)
                continue
            archive.write(path, f"renders/{render_id}.mp4")
        manifest = {
            "project_id": project.project_id,
            "name": project.name,
            "created_at": project.created_at,
            "updated_at": project.updated_at,
            "drafts": [
                {
                    "draft_id": draft.draft_id,
                    "title": draft.result.title,
                    "seconds": draft.result.seconds,
                    "created_at": draft.created_at,
                }
                for draft in project.drafts
            ],
            "renders": [
                render_id
                for render_id in project.render_ids
                if render_id not in skipped
            ],
            "skipped_renders": skipped,
        }
        archive.writestr(
            "project.json",
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )
    return f"{project.name}.zip", buffer.getvalue()


def narration_srt(shots: list[ScriptShot], *, fps: int, padded: bool = True) -> str:
    """Subtitles on the timeline the renderer actually produces. story 模式
    每段补齐到 H3 帧网格,cue 用补齐后的时长;t2v 单发的时间轴只是提示词
    指挥,cue 直接用原始秒数(padded=False)。"""
    entries: list[str] = []
    start = 0.0
    index = 1
    for shot in shots:
        length = (
            align_length_frames(shot.seconds, fps=fps) / fps
            if padded
            else shot.seconds
        )
        end = start + length
        narration = shot.narration.strip()
        if narration:
            entries.append(
                f"{index}\n{_timestamp(start)} --> {_timestamp(end)}\n{narration}\n"
            )
            index += 1
        start = end
    return "\n".join(entries)


def _timestamp(seconds: float) -> str:
    total_ms = round(seconds * 1000)
    ms = total_ms % 1000
    total = total_ms // 1000
    return f"{total // 3600:02d}:{total % 3600 // 60:02d}:{total % 60:02d},{ms:03d}"
