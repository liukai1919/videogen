# The project workspace: one directory per creation, holding its script drafts
# and the ids of the renders it produced. This is the aggregate that turns the
# two isolated seams (scripts, renders) into one traceable flow, mirroring the
# render store's style: versioned JSON on disk, atomic writes, a lock around
# read-modify-write.

from datetime import UTC, datetime
from pathlib import Path
import re
import secrets
import shutil
from threading import Lock
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from videogen_service.artifacts import write_json_atomic
from videogen_service.models import ScriptRequest, ScriptResult

# Same shape as render ids. The store itself lives under ".projects", which can
# never collide with a render directory because a dot is not a legal id char.
_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
PROJECTS_DIRNAME = ".projects"


class ProjectError(RuntimeError):
    pass


class ProjectNotFound(ProjectError):
    pass


class ProjectConflict(ProjectError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectDraft(StrictModel):
    """One archived script run: what was asked and what came back."""

    draft_id: int = Field(ge=1)
    created_at: str
    request: ScriptRequest
    result: ScriptResult


class ProjectRecord(StrictModel):
    schema_version: Literal[1] = 1
    project_id: str
    name: str = Field(min_length=1, max_length=120)
    notes: str = ""
    created_at: str
    updated_at: str
    drafts: list[ProjectDraft] = Field(default_factory=list)
    render_ids: list[str] = Field(default_factory=list)


class ProjectSummary(StrictModel):
    project_id: str
    name: str
    draft_count: int
    render_count: int
    created_at: str
    updated_at: str


class ProjectStore:
    def __init__(self, work_dir: Path) -> None:
        self._directory = work_dir / PROJECTS_DIRNAME
        self._lock = Lock()

    def create(self, *, project_id: str | None, name: str) -> ProjectRecord:
        with self._lock:
            minted = project_id or _mint_project_id()
            if not _PROJECT_ID.fullmatch(minted):
                raise ProjectError("project id 无效")
            if self._record_path(minted).is_file():
                raise ProjectConflict(f"项目 {minted} 已存在")
            now = _now()
            record = ProjectRecord(
                project_id=minted, name=name, created_at=now, updated_at=now
            )
            self._save(record)
            return record

    def list(self) -> list[ProjectSummary]:
        """Newest first, like the render list."""
        with self._lock:
            summaries: list[ProjectSummary] = []
            for path in self._directory.glob("*/project.json"):
                record = self._read(path)
                if record is None or record.project_id != path.parent.name:
                    continue
                summaries.append(
                    ProjectSummary(
                        project_id=record.project_id,
                        name=record.name,
                        draft_count=len(record.drafts),
                        render_count=len(record.render_ids),
                        created_at=record.created_at,
                        updated_at=record.updated_at,
                    )
                )
        summaries.sort(key=lambda summary: summary.created_at, reverse=True)
        return summaries

    def get(self, project_id: str) -> ProjectRecord:
        with self._lock:
            return self._load(project_id)

    def delete(self, project_id: str) -> None:
        """Removes the project and its drafts; linked renders stay untouched."""
        with self._lock:
            self._load(project_id)
            directory = (self._directory / project_id).resolve()
            root = self._directory.resolve()
            if directory == root or root not in directory.parents:
                raise ProjectConflict(f"拒绝删除项目目录之外的路径: {directory}")
            shutil.rmtree(directory)

    def add_draft(
        self, project_id: str, *, request: ScriptRequest, result: ScriptResult
    ) -> ProjectDraft:
        with self._lock:
            record = self._load(project_id)
            draft = ProjectDraft(
                draft_id=len(record.drafts) + 1,
                created_at=_now(),
                request=request,
                result=result,
            )
            self._save(
                record.model_copy(
                    update={
                        "drafts": [*record.drafts, draft],
                        "updated_at": draft.created_at,
                    }
                )
            )
            return draft

    def link_render(self, project_id: str, render_id: str) -> ProjectRecord:
        with self._lock:
            record = self._load(project_id)
            if render_id in record.render_ids:
                return record
            record = record.model_copy(
                update={
                    "render_ids": [*record.render_ids, render_id],
                    "updated_at": _now(),
                }
            )
            self._save(record)
            return record

    def _load(self, project_id: str) -> ProjectRecord:
        if not _PROJECT_ID.fullmatch(project_id):
            raise ProjectNotFound("project id 无效")
        path = self._record_path(project_id)
        if not path.is_file():
            raise ProjectNotFound(f"项目 {project_id} 不存在")
        record = self._read(path)
        if record is None:
            raise ProjectNotFound(f"项目 {project_id} 的记录读不出来")
        return record

    def _read(self, path: Path) -> ProjectRecord | None:
        try:
            return ProjectRecord.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def _record_path(self, project_id: str) -> Path:
        return self._directory / project_id / "project.json"

    def _save(self, record: ProjectRecord) -> None:
        write_json_atomic(
            self._record_path(record.project_id), record.model_dump(mode="json")
        )


def _mint_project_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    return f"proj_{stamp}_{secrets.token_hex(3)}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
