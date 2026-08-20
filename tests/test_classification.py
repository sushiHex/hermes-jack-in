import os
from pathlib import Path

import pytest

from test_core import write_skill


def test_missing_source_root_is_a_blocking_scan_issue(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    result = scan_library(tmp_path / "missing")

    assert result.skills == ()
    assert any("source root does not exist" in issue for issue in result.issues)


def test_file_source_root_is_a_blocking_scan_issue(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "not-a-library"
    source.write_text("not a directory", encoding="utf-8")

    result = scan_library(source)

    assert result.skills == ()
    assert any("source root is not a directory" in issue for issue in result.issues)


def test_existing_empty_source_library_is_valid(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    source.mkdir()

    result = scan_library(source)

    assert result.skills == ()
    assert result.issues == ()


def test_root_level_skill_is_rejected_in_categorized_library(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(source, ".", "name: root-skill\ndescription: Root skill.")

    result = scan_library(source)

    assert result.skills == ()
    assert any("root-level SKILL.md is not allowed" in issue for issue in result.issues)


def test_current_agent_skills_frontmatter_is_directly_portable(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "creative/example",
        "name: example\ndescription: Example skill.\nlicense: MIT\n"
        f"compatibility: {'x' * 500}\nmetadata:\n  author: Hermes\n  version: 1.2.3\n"
        "allowed-tools: Bash Read",
    )

    skill = scan_library(source).skills[0]
    assert skill.classification is Classification.DIRECT
    assert skill.reasons == ()


@pytest.mark.parametrize(
    "field, frontmatter",
    [
        ("hooks", "hooks:\n  PreToolUse:\n    - command: echo unsafe"),
        ("shell", "shell: echo unsafe"),
        ("agent", "agent: Explore"),
        ("background", "background: true"),
        ("context", "context: fork"),
    ],
)
def test_execution_bearing_frontmatter_is_excluded(
    tmp_path: Path, field: str, frontmatter: str
) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "dev/unsafe-frontmatter",
        f"name: unsafe-frontmatter\ndescription: Unsafe frontmatter.\n{frontmatter}",
    )

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.EXCLUDE
    assert skill.reasons == (f"execution-bearing frontmatter: {field}",)


def test_body_override_cannot_approve_execution_bearing_frontmatter(tmp_path: Path) -> None:
    from hermes_jack_in.core import OverrideError, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "dev/unsafe-frontmatter",
        "name: unsafe-frontmatter\ndescription: Unsafe frontmatter.\nshell: echo unsafe",
        "Use `terminal`.\n",
    )
    overrides = {
        "unsafe-frontmatter": {
            "classification": "semantic-adaptation",
            "replacements": [{"from": "Use `terminal`.", "to": "Use `Bash`."}],
        }
    }

    with pytest.raises(OverrideError, match="execution-bearing frontmatter: shell"):
        scan_library(source, overrides=overrides)


def test_non_executable_claude_fields_are_preserved_through_conversion(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, render_skill, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "docs/safe-fields",
        "name: safe-fields\ndescription: Safe fields.\nargument-hint: <topic>\n"
        "model: sonnet\npaths: ['references/guide.md']",
    )

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.CONVERT
    assert skill.reasons == ("Claude-specific frontmatter: argument-hint, model, paths",)
    rendered = render_skill(skill)
    assert "argument-hint: <topic>" in rendered
    assert "model: sonnet" in rendered
    assert "references/guide.md" in rendered


def test_invalid_optional_frontmatter_requires_sanitizing_conversion(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, render_skill, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "creative/example",
        "name: example\ndescription: Example skill.\nlicense: 7\n"
        f"compatibility: {'x' * 501}\nallowed-tools:\n  - Bash",
    )

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.CONVERT
    assert skill.reasons == (
        "invalid optional frontmatter: allowed-tools, compatibility, license",
    )
    rendered = render_skill(skill)
    assert "license:" not in rendered
    assert "compatibility:" not in rendered
    assert "allowed-tools:" not in rendered


def test_nested_or_non_string_metadata_requires_conversion(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, render_skill, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "creative/example",
        "name: example\ndescription: Example skill.\nversion: 1.2.3\nmetadata:\n  hermes:\n    tags: [example]",
    )

    skill = scan_library(source).skills[0]
    assert skill.classification is Classification.CONVERT
    assert skill.reasons == ("unsupported frontmatter: version", "unsupported metadata shape")
    assert "metadata:" not in render_skill(skill)


def test_hermes_tool_semantics_are_excluded_without_override(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(source, "ops/run", "name: run\ndescription: Run it.", "Call `terminal` and then `read_file`.\n")

    skill = scan_library(source).skills[0]
    assert skill.classification is Classification.EXCLUDE
    assert "Hermes tools: read_file, terminal" in skill.reasons


def test_execute_code_is_treated_as_a_hermes_tool(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(source, "dev/python", "name: python\ndescription: Python.", "Use `execute_code`.\n")

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.EXCLUDE
    assert skill.reasons == ("Hermes tools: execute_code",)


@pytest.mark.parametrize(
    "runtime_reference",
    [
        ".hermes/skills/research/example/SKILL.md",
        r"C:\Users\alice\.hermes\config.yaml",
        "${HERMES_SKILL_DIR}/scripts/run.sh",
        "${HERMES_SESSION_ID}",
    ],
)
def test_documented_hermes_runtime_paths_are_excluded(
    tmp_path: Path, runtime_reference: str
) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "dev/runtime-path",
        "name: runtime-path\ndescription: Runtime path.",
        f"Use {runtime_reference}.\n",
    )

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.EXCLUDE
    assert "Hermes runtime" in skill.reasons


def test_declarative_override_enables_exact_semantic_adaptation(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(source, "dev/tdd", "name: tdd\ndescription: Test first.", "Use the `terminal` tool.\n")
    overrides = {
        "tdd": {
            "classification": "semantic-adaptation",
            "replacements": [{"from": "Use the `terminal` tool.", "to": "Use the `Bash` tool."}],
        }
    }

    skill = scan_library(source, overrides=overrides).skills[0]
    assert skill.classification is Classification.ADAPT
    assert skill.body == "Use the `Bash` tool.\n"
    assert skill.reasons == ("explicit semantic override",)


def test_override_fails_closed_when_exact_replacement_is_stale(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(source, "dev/tdd", "name: tdd\ndescription: Test first.", "Changed wording.\n")
    overrides = {
        "tdd": {
            "classification": "semantic-adaptation",
            "replacements": [{"from": "Old wording.", "to": "New wording."}],
        }
    }

    with pytest.raises(ValueError, match="replacement source not found"):
        scan_library(source, overrides=overrides)


def test_malformed_yaml_and_missing_skill_are_reported(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    broken = source / "bad" / "broken"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text("---\nname: [\n---\nbody", encoding="utf-8")
    missing = source / "bad" / "missing"
    missing.mkdir(parents=True)
    (missing / "reference.md").write_text("orphan", encoding="utf-8")

    result = scan_library(source)
    assert result.skills == ()
    assert any("malformed YAML" in issue for issue in result.issues)
    assert any("missing SKILL.md" in issue for issue in result.issues)


def test_category_directory_with_descendant_skills_is_not_reported_missing(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    category = source / "research"
    category.mkdir(parents=True)
    (category / "catalog.json").write_text("{}", encoding="utf-8")
    write_skill(source, "research/plain", "name: plain\ndescription: Plain.")

    result = scan_library(source)

    assert result.issues == ()


def test_duplicate_names_are_reported_as_collisions(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(source, "one/shared", "name: shared\ndescription: One.")
    write_skill(source, "two/shared", "name: shared\ndescription: Two.")

    result = scan_library(source)
    assert all(skill.blocked for skill in result.skills)
    assert any("name collision: shared" in issue for issue in result.issues)


@pytest.mark.parametrize(
    "name",
    ["../escape", "C:/escape", "UpperCase", "con", "two words", "double--hyphen", "trailing-"],
)
def test_unsafe_or_nonportable_skill_names_are_rejected(tmp_path: Path, name: str) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(source, "bad/skill", f"name: {name!r}\ndescription: Unsafe.")

    result = scan_library(source)

    assert result.skills == ()
    assert any("invalid portable skill name" in issue for issue in result.issues)


def test_agent_skills_description_length_is_enforced(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(source, "bad/description", f"name: description\ndescription: {'x' * 1025}")

    result = scan_library(source)

    assert result.skills == ()
    assert any("description exceeds 1024 characters" in issue for issue in result.issues)


def test_unbackticked_tool_calls_and_allowed_tools_are_excluded(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "dev/unsafe",
        "name: unsafe\ndescription: Unsafe.\nallowed-tools: [terminal]",
        "```python\nexecute_code('print(1)')\n```\n",
    )

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.EXCLUDE
    assert "Hermes tools: execute_code, terminal" in skill.reasons


def test_claude_interpreted_frontmatter_is_scanned_for_hermes_semantics(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "dev/unsafe-description",
        "name: unsafe-description\ndescription: Use execute_code for arithmetic.",
    )

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.EXCLUDE
    assert "Hermes tools: execute_code" in skill.reasons


def test_opaque_metadata_does_not_create_runtime_semantics(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "dev/provenance",
        "name: provenance\ndescription: Safe.\nmetadata:\n  hermes-author: Hermes Agent\n  hermes-tags: '[\"terminal\"]'",
    )

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.DIRECT
    assert skill.reasons == ()


def test_malformed_replacements_override_raises_clear_error(tmp_path: Path) -> None:
    from hermes_jack_in.core import OverrideError, scan_library

    source = tmp_path / "skills"
    write_skill(source, "dev/tool", "name: tool\ndescription: Tool.", "Use `terminal`.\n")

    with pytest.raises(OverrideError, match="replacements must be a list"):
        scan_library(source, overrides={"tool": {"classification": "semantic-adaptation", "replacements": "bad"}})


def test_empty_replacement_source_anchor_is_rejected(tmp_path: Path) -> None:
    from hermes_jack_in.core import OverrideError, scan_library

    source = tmp_path / "skills"
    write_skill(source, "dev/tool", "name: tool\ndescription: Tool.", "Portable body.\n")
    overrides = {
        "tool": {
            "classification": "semantic-adaptation",
            "replacements": [{"from": "", "to": "injected"}],
        }
    }

    with pytest.raises(OverrideError, match="replacement 'from' must not be empty"):
        scan_library(source, overrides=overrides)


def test_windows_relative_links_are_normalized_during_conversion(tmp_path: Path) -> None:
    from hermes_jack_in.core import render_skill, scan_library

    source = tmp_path / "skills"
    skill_dir = write_skill(
        source,
        "docs/linked",
        "name: linked\ndescription: Uses a reference.\nversion: 1",
        "Read [the guide](references\\guide.md).\n",
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "guide.md").write_text("guide", encoding="utf-8")

    rendered = render_skill(scan_library(source).skills[0])
    assert "[the guide](references/guide.md)" in rendered
    assert "version:" not in rendered
    assert "source: docs/linked" in rendered


@pytest.mark.parametrize("target", ["../../secret.txt", "/etc/passwd", "C:\\Users\\secret.txt"])
def test_local_markdown_links_must_stay_inside_skill_root(tmp_path: Path, target: str) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "docs/escape-link",
        "name: escape-link\ndescription: Escape link.",
        f"Read [outside]({target}).\n",
    )

    result = scan_library(source)

    assert result.skills == ()
    assert any("unsafe local Markdown link" in issue for issue in result.issues)


@pytest.mark.parametrize(
    "body",
    [
        "Read [outside][ref].\n\n[ref]: %2e%2e/secret.txt\n",
        "Read [outside](file:///etc/passwd).\n",
        '<img src="../secret.png">\n',
        "Read [blank]( ).\n",
        "Read [blank](%20).\n",
        "Read [blank][ref].\n\n[ref]: <>\n",
        '<img src=" ">\n',
    ],
)
def test_alternate_link_forms_cannot_escape_skill_root(tmp_path: Path, body: str) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(source, "docs/alternate-link", "name: alternate-link\ndescription: Alternate.", body)

    result = scan_library(source)

    assert result.skills == ()
    assert any("unsafe local Markdown link" in issue for issue in result.issues)


@pytest.mark.parametrize(
    "body",
    [
        "Read [ambiguous](references/guide(v2).md).\n",
        "Read [outside](&#46;&#46;/secret.txt).\n",
        "<img src=&#46;&#46;/secret.png>\n",
        "<a href=..&#x2f;secret.txt>outside</a>\n",
        "<img src=&#x25;2e&#x25;2e/secret.png>\n",
    ],
)
def test_ambiguous_or_entity_encoded_links_cannot_bypass_containment(
    tmp_path: Path, body: str
) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(source, "docs/encoded-link", "name: encoded-link\ndescription: Encoded.", body)

    result = scan_library(source)

    assert result.skills == ()
    assert any("unsafe local Markdown link" in issue for issue in result.issues)


@pytest.mark.parametrize(
    "target",
    [
        "%23/../../secret.txt",
        "%2523/../../secret.txt",
        "&#35;/../../secret.txt",
        "%3F/../../secret.txt",
        "https%3A/../../secret.txt",
        "https&#58;/../../secret.txt",
        "%23section",
        "https%3A//example.com/guide",
    ],
)
def test_encoded_url_class_delimiters_are_rejected_before_decoded_path_validation(
    tmp_path: Path, target: str
) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "docs/encoded-prefix",
        "name: encoded-prefix\ndescription: Encoded prefix.",
        f"Read [target]({target}).\n",
    )

    result = scan_library(source)

    assert result.skills == ()
    assert any("unsafe local Markdown link" in issue for issue in result.issues)


@pytest.mark.parametrize(
    "body",
    [
        "<file:///etc/passwd>\n",
        '<form action="../../secret.txt"></form>\n',
        '<button formaction="../secret.txt">submit</button>\n',
        '<video poster="/etc/passwd"></video>\n',
        '<object data="file:///etc/passwd"></object>\n',
        '<img srcset="assets/safe.png 1x, ../../secret.png 2x">\n',
    ],
)
def test_autolinks_and_additional_html_url_attributes_cannot_escape_skill_root(
    tmp_path: Path, body: str
) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "docs/raw-url",
        "name: raw-url\ndescription: Raw URL.",
        body,
    )

    result = scan_library(source)

    assert result.skills == ()
    assert any("unsafe local Markdown link" in issue for issue in result.issues)


def test_non_html_keyword_arguments_are_not_treated_as_url_attributes(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "docs/function-call",
        "name: function-call\ndescription: Function call.",
        'process(action="write", data="\\x03")\n',
    )

    result = scan_library(source)

    assert len(result.skills) == 1
    assert result.issues == ()


def test_parentheses_in_external_link_and_safe_unquoted_html_are_allowed(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "docs/safe-links",
        "name: safe-links\ndescription: Safe links.",
        "Read [external](https://example.com/docs_(v2)/part%23one).\n"
        "<https://example.com/reference>\n"
        "<img src=assets/image.png>\n"
        '<form action="https://example.com/submit"></form>\n'
        '<video poster="assets/poster.png"></video>\n',
    )

    result = scan_library(source)

    assert len(result.skills) == 1
    assert result.issues == ()


def test_semantic_override_cannot_introduce_unsafe_link(tmp_path: Path) -> None:
    from hermes_jack_in.core import OverrideError, scan_library

    source = tmp_path / "skills"
    write_skill(source, "docs/adapted", "name: adapted\ndescription: Adapted.", "Use `terminal`.\n")
    overrides = {
        "adapted": {
            "classification": "semantic-adaptation",
            "replacements": [{"from": "Use `terminal`.", "to": "Read [outside](../secret.txt)."}],
        }
    }

    with pytest.raises(OverrideError, match="unsafe local Markdown link"):
        scan_library(source, overrides=overrides)


def test_hidden_source_artifact_forces_filtered_materialization(tmp_path: Path) -> None:
    from hermes_jack_in.core import Classification, scan_library

    source = tmp_path / "skills"
    skill_dir = write_skill(source, "docs/hidden", "name: hidden\ndescription: Hidden.")
    (skill_dir / ".env").write_text("SECRET=not-copied", encoding="utf-8")

    skill = scan_library(source).skills[0]

    assert skill.classification is Classification.CONVERT
    assert "hidden source artifacts" in skill.reasons


def test_conversion_does_not_corrupt_regex_or_shell_escapes(tmp_path: Path) -> None:
    from hermes_jack_in.core import render_skill, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "docs/escapes",
        "name: escapes\ndescription: Escapes.\nversion: 1",
        "Use regex `\\d+` and a shell continuation `cmd \\\\ next`.\n",
    )

    rendered = render_skill(scan_library(source).skills[0])

    assert "`\\d+`" in rendered
    assert "`cmd \\\\ next`" in rendered


def test_frontmatter_paths_are_normalized(tmp_path: Path) -> None:
    from hermes_jack_in.core import render_skill, scan_library

    source = tmp_path / "skills"
    write_skill(
        source,
        "docs/paths",
        "name: paths\ndescription: Paths.\npaths: ['references\\guide.md']",
    )

    rendered = render_skill(scan_library(source).skills[0])

    assert "references/guide.md" in rendered


def test_source_junction_is_rejected_instead_of_scanning_external_content(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("NTFS junctions are Windows-only")

    from hermes_jack_in.core import scan_library
    from hermes_jack_in.sync import _create_junction

    source = tmp_path / "skills"
    source.mkdir()
    external = write_skill(tmp_path / "external", "linked", "name: linked\ndescription: External.")
    _create_junction(source / "linked", external)

    result = scan_library(source)

    assert result.skills == ()
    assert any("source reparse point is not allowed" in issue for issue in result.issues)


def test_source_root_junction_is_rejected_before_resolution(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("NTFS junctions are Windows-only")

    from hermes_jack_in.core import scan_library
    from hermes_jack_in.sync import _create_junction

    external = tmp_path / "external"
    write_skill(external, "linked", "name: linked\ndescription: External.")
    source = tmp_path / "skills"
    _create_junction(source, external)

    result = scan_library(source)

    assert result.skills == ()
    assert any("source root reparse point is not allowed" in issue for issue in result.issues)


def test_source_root_with_junction_ancestor_is_rejected_before_resolution(tmp_path: Path) -> None:
    if os.name != "nt":
        pytest.skip("NTFS junctions are Windows-only")

    from hermes_jack_in.core import scan_library
    from hermes_jack_in.sync import _create_junction

    external = tmp_path / "external"
    write_skill(external / "library", "linked", "name: linked\ndescription: External.")
    alias = tmp_path / "alias"
    _create_junction(alias, external)

    result = scan_library(alias / "library")

    assert result.skills == ()
    assert any("source root reparse ancestor is not allowed" in issue for issue in result.issues)


def test_source_symlink_is_rejected_instead_of_copying_external_content(tmp_path: Path) -> None:
    from hermes_jack_in.core import scan_library

    source = tmp_path / "skills"
    skill_dir = write_skill(source, "docs/linked", "name: linked\ndescription: Linked.")
    external = tmp_path / "secret.txt"
    external.write_text("not for copying", encoding="utf-8")
    try:
        os.symlink(external, skill_dir / "reference.md")
    except OSError:
        pytest.skip("Windows symlink privilege unavailable")

    result = scan_library(source)

    assert result.skills == ()
    assert any("source symlink is not allowed" in issue for issue in result.issues)
