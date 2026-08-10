# Named, reusable creative presets. A skill is a directory holding the two
# files MiniMax Design popularised: meta.yaml (identity plus default knobs) and
# SKILL.md (instructions a model reads). The library rescans the directory on
# every call, so editing a skill takes effect on the next request without a
# service restart.

import logging
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError
import yaml

_LOGGER = logging.getLogger("videogen_service.skills")
_SKILL_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# Reference filenames are flat: no separators means no path traversal.
_REFERENCE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,80}\.md$")
# The instructions ride inside the summary prompt next to the transcript, so a
# runaway SKILL.md must not crowd the actual subtitles out of the context.
_MAX_INSTRUCTION_CHARS = 8_000
# References load on demand (one at a time), so they get a roomier budget.
_MAX_REFERENCE_CHARS = 12_000


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SkillDefaults(StrictModel):
    """Values the skill contributes where the request leaves them unset."""

    style: str | None = None
    target_seconds: float | None = Field(default=None, gt=0)
    shot_seconds: float | None = Field(default=None, gt=0)
    max_shots: int | None = Field(default=None, gt=0)
    output_language: str | None = Field(default=None, min_length=1, max_length=40)


class SkillMeta(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    category: str = Field(default="", max_length=40)
    version: str = Field(default="", max_length=20)
    # Routing knowledge for the agent: when to pick this skill, when to stay
    # away (and which skill to use instead), and which skills it chains with.
    use_when: list[str] = Field(default_factory=list, max_length=12)
    not_for: list[str] = Field(default_factory=list, max_length=12)
    chain: list[str] = Field(default_factory=list, max_length=8)
    defaults: SkillDefaults = Field(default_factory=SkillDefaults)


class Skill(StrictModel):
    skill_id: str
    meta: SkillMeta
    instructions: str
    # Filenames under references/, loadable one at a time via read_reference.
    references: list[str] = Field(default_factory=list)


class SkillLibrary:
    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def list(self) -> list[Skill]:
        if not self._directory.is_dir():
            return []
        skills: list[Skill] = []
        for path in sorted(self._directory.iterdir()):
            if path.is_dir():
                skill = self._load(path)
                if skill is not None:
                    skills.append(skill)
        return skills

    def get(self, skill_id: str) -> Skill | None:
        if not _SKILL_ID.fullmatch(skill_id):
            return None
        path = self._directory / skill_id
        return self._load(path) if path.is_dir() else None

    def read_reference(self, skill_id: str, name: str) -> str | None:
        """One reference document's text, or None when it doesn't exist.
        Names are flat filenames; the regex rejects anything path-like."""
        if not _SKILL_ID.fullmatch(skill_id) or not _REFERENCE_NAME.fullmatch(name):
            return None
        path = self._directory / skill_id / "references" / name
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            return None
        return content.strip()[:_MAX_REFERENCE_CHARS]

    def _load(self, path: Path) -> Skill | None:
        # A broken skill only disables itself: the service keeps starting and
        # the warning tells the operator which directory to fix.
        if not _SKILL_ID.fullmatch(path.name):
            _LOGGER.warning(
                "跳过 Skill %s: 目录名要匹配 [A-Za-z0-9_-]{1,80}", path.name
            )
            return None
        try:
            raw = yaml.safe_load((path / "meta.yaml").read_text(encoding="utf-8"))
            meta = SkillMeta.model_validate(raw or {})
            instructions = (path / "SKILL.md").read_text(encoding="utf-8").strip()
        except (OSError, ValidationError, yaml.YAMLError) as error:
            _LOGGER.warning("跳过 Skill %s: %s", path.name, error)
            return None
        references_dir = path / "references"
        references = (
            sorted(
                entry.name
                for entry in references_dir.iterdir()
                if entry.is_file() and _REFERENCE_NAME.fullmatch(entry.name)
            )
            if references_dir.is_dir()
            else []
        )
        return Skill(
            skill_id=path.name,
            meta=meta,
            instructions=instructions[:_MAX_INSTRUCTION_CHARS],
            references=references,
        )
