# Skill 路由元数据与 references 机制(M2):路由三件套解析、参考文档发现、
# 按需读取的防穿越与限长。
from pathlib import Path

from videogen_service.skills import SkillLibrary


META_WITH_ROUTING = "\n".join(
    [
        "name: 概念预告片",
        "description: 电影级预告片质感",
        "category: 预告片",
        'version: "1.1"',
        "use_when:",
        "  - 概念预告片、片花",
        "  - 电影感悬念叙事",
        "not_for:",
        "  - 科普内容(用 science-doc)",
        "chain:",
        "  - science-doc",
        "defaults:",
        "  shot_seconds: 5",
    ]
)

LEGACY_META = "\n".join(
    [
        "name: 科普纪录片",
        "description: 老格式",
        "defaults:",
        "  shot_seconds: 7",
    ]
)


def write_skill(
    skills_dir: Path, skill_id: str, *, meta: str, instructions: str = "- 规则"
) -> Path:
    path = skills_dir / skill_id
    path.mkdir(parents=True)
    (path / "meta.yaml").write_text(meta, encoding="utf-8")
    (path / "SKILL.md").write_text(instructions, encoding="utf-8")
    return path


def test_routing_fields_parse(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "concept-trailer", meta=META_WITH_ROUTING)
    skill = SkillLibrary(skills_dir).get("concept-trailer")
    assert skill is not None
    assert skill.meta.version == "1.1"
    assert skill.meta.use_when == ["概念预告片、片花", "电影感悬念叙事"]
    assert skill.meta.not_for == ["科普内容(用 science-doc)"]
    assert skill.meta.chain == ["science-doc"]


def test_legacy_meta_without_routing_fields_still_loads(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    write_skill(skills_dir, "science-doc", meta=LEGACY_META)
    skill = SkillLibrary(skills_dir).get("science-doc")
    assert skill is not None
    assert skill.meta.use_when == []
    assert skill.meta.not_for == []
    assert skill.meta.version == ""
    assert skill.references == []


def test_references_are_discovered_sorted_and_filtered(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    path = write_skill(skills_dir, "concept-trailer", meta=META_WITH_ROUTING)
    refs = path / "references"
    refs.mkdir()
    (refs / "shot-rules.md").write_text("镜头细则", encoding="utf-8")
    (refs / "beat.md").write_text("节奏细则", encoding="utf-8")
    (refs / "bad name.md").write_text("文件名带空格,不收", encoding="utf-8")
    (refs / "notes.txt").write_text("不是 md,不收", encoding="utf-8")
    (refs / "sub").mkdir()

    skill = SkillLibrary(skills_dir).get("concept-trailer")
    assert skill is not None
    assert skill.references == ["beat.md", "shot-rules.md"]


def test_read_reference_round_trip_and_guards(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    path = write_skill(skills_dir, "concept-trailer", meta=META_WITH_ROUTING)
    refs = path / "references"
    refs.mkdir()
    (refs / "beat.md").write_text("  开场一镜定调。  ", encoding="utf-8")

    library = SkillLibrary(skills_dir)
    assert library.read_reference("concept-trailer", "beat.md") == "开场一镜定调。"
    # 防穿越:任何带路径分隔或不合规的名字都拿不到东西。
    assert library.read_reference("concept-trailer", "../SKILL.md") is None
    assert library.read_reference("concept-trailer", "refs/beat.md") is None
    assert library.read_reference("concept-trailer", "beat") is None
    assert library.read_reference("concept-trailer", "missing.md") is None
    assert library.read_reference("no-such-skill", "beat.md") is None


def test_reference_content_is_capped(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    path = write_skill(skills_dir, "concept-trailer", meta=META_WITH_ROUTING)
    refs = path / "references"
    refs.mkdir()
    (refs / "huge.md").write_text("字" * 20_000, encoding="utf-8")

    content = SkillLibrary(skills_dir).read_reference("concept-trailer", "huge.md")
    assert content is not None
    assert len(content) == 12_000
