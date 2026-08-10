# 对仓库真实 skills/ 目录的纪律检查(对标 higgsfield validate-skills CI):
# meta 合法且带路由三件套、目录名合规、SKILL.md 不超预算、references 无孤儿。
from pathlib import Path
import re

import yaml

from videogen_service.skills import SkillLibrary, SkillMeta

REPO_SKILLS = Path(__file__).resolve().parent.parent / "skills"
_SKILL_ID = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
# 与 skills.py 的 _MAX_INSTRUCTION_CHARS 对齐:超过会被加载器静默截断,
# 在这里直接测出来,别等线上被剪。
MAX_INSTRUCTION_CHARS = 8_000


def skill_dirs() -> list[Path]:
    return sorted(path for path in REPO_SKILLS.iterdir() if path.is_dir())


def test_repo_ships_at_least_one_skill() -> None:
    assert skill_dirs(), f"{REPO_SKILLS} 里一个技能都没有"


def test_every_skill_dir_loads_without_being_skipped() -> None:
    loaded = {skill.skill_id for skill in SkillLibrary(REPO_SKILLS).list()}
    expected = {path.name for path in skill_dirs()}
    assert loaded == expected, f"有技能加载失败被跳过: {expected - loaded}"


def test_meta_is_valid_and_carries_routing() -> None:
    for path in skill_dirs():
        assert _SKILL_ID.fullmatch(path.name), f"目录名不合规: {path.name}"
        raw = yaml.safe_load((path / "meta.yaml").read_text(encoding="utf-8"))
        meta = SkillMeta.model_validate(raw or {})
        assert meta.use_when, f"{path.name} 缺 use_when 触发条件,agent 无法路由"
        assert meta.not_for, f"{path.name} 缺 not_for 边界,容易被误用"


def test_skill_md_stays_within_budget() -> None:
    for path in skill_dirs():
        text = (path / "SKILL.md").read_text(encoding="utf-8")
        assert len(text) <= MAX_INSTRUCTION_CHARS, (
            f"{path.name}/SKILL.md 有 {len(text)} 字,超过 {MAX_INSTRUCTION_CHARS} "
            "会被加载器截断;把细则挪进 references/ 按需读"
        )


def test_references_have_no_orphans() -> None:
    for path in skill_dirs():
        refs_dir = path / "references"
        if not refs_dir.is_dir():
            continue
        skill_md = (path / "SKILL.md").read_text(encoding="utf-8")
        for entry in sorted(refs_dir.iterdir()):
            if not entry.is_file():
                continue
            assert entry.name in skill_md, (
                f"{path.name}/references/{entry.name} 没有被 SKILL.md 提到,"
                "是孤儿文档;在 SKILL.md 里说明它讲什么、什么时候读"
            )
