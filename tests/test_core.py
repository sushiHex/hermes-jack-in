from pathlib import Path


def write_skill(root: Path, relative: str, frontmatter: str, body: str = "Use this procedure.\n") -> Path:
    skill_dir = root / relative
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return skill_dir


def test_scan_classifies_minimal_skill_as_directly_portable(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "Hermes Skills"
    write_skill(source, "research/plain", "name: plain\ndescription: Use when plain work is requested.")

    result = scan_library(source)

    assert len(result.skills) == 1
    skill = result.skills[0]
    assert skill.name == "plain"
    assert skill.relative_dir.as_posix() == "research/plain"
    assert skill.classification is Classification.DIRECT
    assert skill.reasons == ()


def test_source_hash_uses_typed_length_framing(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    first = tmp_path / "first"
    first_skill = write_skill(first, "research/plain", "name: plain\ndescription: Plain.")
    (first_skill / "a").write_bytes(b"bc")

    second = tmp_path / "second"
    second_skill = write_skill(second, "research/plain", "name: plain\ndescription: Plain.")
    (second_skill / "ab").write_bytes(b"c")

    assert scan_library(first).skills[0].source_hash != scan_library(second).skills[0].source_hash
