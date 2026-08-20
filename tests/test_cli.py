import json
import shutil
from pathlib import Path

import pytest

from test_core import write_skill


def test_cli_reports_public_name_and_installed_version(capsys) -> None:
    from importlib.metadata import version

    from hermes_jack_in.cli import _parser

    with pytest.raises(SystemExit) as exc_info:
        _parser().parse_args(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"hermes-jack-in {version('hermes-jack-in')}"


def test_cli_feedback_propose_emits_the_written_review_only_proposal(
    tmp_path: Path,
    capsys,
) -> None:
    from hermes_jack_in.cli import main
    from hermes_jack_in.sync import sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    feedback = tmp_path / "feedback.json"
    output = tmp_path / "proposal.json"
    write_skill(source, "one/plain", "name: plain\ndescription: Plain.")
    sync_library(source, destination, prefer_symlinks=False)
    feedback.write_text(
        json.dumps(
            {
                "claimed_provenance": "Operator-supplied Claude Code session note.",
                "version": "feedback-v1",
                "skill": "plain",
                "summary": "Clarify one instruction.",
                "observation": "The projected instructions were ambiguous.",
                "recommendation": "Clarify the expected operator response.",
            }
        ),
        encoding="utf-8",
    )

    code = main(
        [
            "feedback-propose",
            "--source",
            str(source),
            "--destination",
            str(destination),
            "--input",
            str(feedback),
            "--output",
            str(output),
            "--json",
        ]
    )

    assert code == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted == json.loads(output.read_text(encoding="utf-8"))
    assert emitted["review_status"] == "required"


def test_cli_feedback_propose_reports_failure_as_json_without_output(
    tmp_path: Path,
    capsys,
) -> None:
    from hermes_jack_in.cli import main
    from hermes_jack_in.sync import sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    feedback = tmp_path / "feedback.json"
    output = tmp_path / "proposal.json"
    write_skill(source, "one/plain", "name: plain\ndescription: Plain.")
    sync_library(source, destination, prefer_symlinks=False)
    feedback.write_text(
        '{"version":"feedback-v1","skill":"missing",'
        '"claimed_provenance":"operator assertion","summary":"s",'
        '"observation":"o","recommendation":"r"}',
        encoding="utf-8",
    )

    code = main(
        [
            "feedback-propose",
            "--source",
            str(source),
            "--destination",
            str(destination),
            "--input",
            str(feedback),
            "--output",
            str(output),
            "--json",
        ]
    )

    assert code == 2
    assert "selected projection" in json.loads(capsys.readouterr().err)["error"]
    assert not output.exists()


def test_cli_scan_and_plan_emit_machine_readable_inventory(tmp_path: Path, capsys) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/plain", "name: plain\ndescription: Plain.")
    write_skill(source, "two/hermes", "name: hermes\ndescription: Hermes.", "Use `skill_view`.\n")

    assert main(["scan", "--source", str(source), "--json"]) == 0
    scan = json.loads(capsys.readouterr().out)
    assert scan["summary"] == {"directly-portable": 1, "hermes-only": 1}
    assert {row["name"] for row in scan["skills"]} == {"plain", "hermes"}

    assert main(["plan", "--source", str(source), "--destination", str(destination), "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["actions"][0]["operation"] == "install"
    assert plan["excluded"][0]["name"] == "hermes"
    assert not destination.exists()


def test_cli_sync_check_and_remove_lifecycle(tmp_path: Path, capsys) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/plain", "name: plain\ndescription: Plain.\nversion: 1")
    common = ["--source", str(source), "--destination", str(destination), "--json"]

    assert main(["sync", *common, "--copy"]) == 0
    assert json.loads(capsys.readouterr().out)["actions"][0]["mode"] == "materialized"
    assert main(["check", *common]) == 0
    assert json.loads(capsys.readouterr().out)["issues"] == []
    assert main(["remove", "--destination", str(destination), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["actions"][0]["operation"] == "remove"


def test_cli_empty_source_plan_is_reportable_but_sync_requires_explicit_intent(
    tmp_path: Path,
    capsys,
) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/plain", "name: plain\ndescription: Plain.")
    common = ["--source", str(source), "--destination", str(destination), "--json"]
    assert main(["sync", *common, "--copy"]) == 0
    capsys.readouterr()
    shutil.rmtree(source / "one")
    before = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }

    assert main(["scan", "--source", str(source), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"] == {}
    assert main(["plan", *common]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert [(action["operation"], action["name"]) for action in plan["actions"]] == [
        ("remove-stale", "plain")
    ]

    assert main(["sync", *common, "--copy"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert "--allow-empty" in error
    after = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    assert after == before

    assert main(["sync", *common, "--copy", "--allow-empty"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [(action["operation"], action["name"]) for action in result["actions"]] == [
        ("remove-stale", "plain")
    ]


def test_cli_all_excluded_source_sync_requires_explicit_empty_intent(
    tmp_path: Path,
    capsys,
) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    overrides = tmp_path / "overrides.yaml"
    write_skill(
        source,
        "one/adapted",
        "name: adapted\ndescription: Adapted.",
        "Use `terminal`.\n",
    )
    overrides.write_text(
        "skills:\n"
        "  adapted:\n"
        "    classification: semantic-adaptation\n"
        "    replacements:\n"
        "      - from: 'Use `terminal`.'\n"
        "        to: 'Use `Bash`.'\n",
        encoding="utf-8",
    )
    common = ["--source", str(source), "--destination", str(destination), "--json"]
    assert main(["sync", *common, "--copy", "--overrides", str(overrides)]) == 0
    capsys.readouterr()
    before = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }

    assert main(["scan", "--source", str(source), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["summary"] == {"hermes-only": 1}
    assert main(["sync", *common, "--copy"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert "--allow-empty" in error
    after = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }
    assert after == before

    assert main(["sync", *common, "--copy", "--allow-empty"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert [(action["operation"], action["name"]) for action in result["actions"]] == [
        ("remove-stale", "adapted")
    ]


def test_cli_loads_yaml_semantic_overrides(tmp_path: Path, capsys) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    override_path = tmp_path / "overrides.yaml"
    write_skill(source, "dev/tdd", "name: tdd\ndescription: TDD.", "Use `terminal`.\n")
    override_path.write_text(
        "skills:\n  tdd:\n    classification: semantic-adaptation\n    replacements:\n      - from: 'Use `terminal`.'\n        to: 'Use `Bash`.'\n",
        encoding="utf-8",
    )

    assert main(["scan", "--source", str(source), "--overrides", str(override_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"] == {"semantic-adaptation": 1}


@pytest.mark.parametrize(
    "contents",
    [
        "",
        "{}\n",
        "[]\n",
        "skills: []\n",
        "skills: {}\nunexpected: true\n",
    ],
)
def test_explicit_override_yaml_requires_exact_top_level_shape(
    tmp_path: Path, capsys, contents: str
) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    override_path = tmp_path / "overrides.yaml"
    write_skill(source, "dev/plain", "name: plain\ndescription: Plain.")
    override_path.write_text(contents, encoding="utf-8")

    code = main(["scan", "--source", str(source), "--overrides", str(override_path), "--json"])

    assert code == 2
    payload = json.loads(capsys.readouterr().err)
    assert "exactly one top-level 'skills' mapping" in payload["error"]


def test_explicit_override_yaml_rejects_unused_skill_names(tmp_path: Path, capsys) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    override_path = tmp_path / "overrides.yaml"
    write_skill(source, "dev/plain", "name: plain\ndescription: Plain.")
    override_path.write_text(
        "skills:\n  typo:\n    classification: hermes-only\n    reason: not used\n",
        encoding="utf-8",
    )

    code = main(["scan", "--source", str(source), "--overrides", str(override_path), "--json"])

    assert code == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "unused override skill names: typo"


@pytest.mark.parametrize(
    "contents",
    [
        "skills: &rules\n"
        "  tool:\n"
        "    classification: semantic-adaptation\n"
        "    replacements: &replacements\n"
        "      - from: 'Use `terminal`.'\n"
        "        to: 'Use `Bash`.'\n"
        "skills: *rules\n",
        "skills:\n"
        "  tool: &rule\n"
        "    classification: semantic-adaptation\n"
        "    replacements:\n"
        "      - from: 'Use `terminal`.'\n"
        "        to: 'Use `Bash`.'\n"
        "  tool: *rule\n",
        "skills:\n"
        "  tool:\n"
        "    classification: semantic-adaptation\n"
        "    classification: semantic-adaptation\n"
        "    replacements:\n"
        "      - from: 'Use `terminal`.'\n"
        "        to: 'Use `Bash`.'\n",
        "skills:\n"
        "  tool:\n"
        "    classification: semantic-adaptation\n"
        "    replacements:\n"
        "      - from: stale\n"
        "        from: 'Use `terminal`.'\n"
        "        to: 'Use `Bash`.'\n",
    ],
    ids=["top-level", "skill", "rule", "replacement"],
)
def test_explicit_override_yaml_rejects_duplicate_mapping_keys_at_every_level(
    tmp_path: Path, capsys, contents: str
) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    override_path = tmp_path / "overrides.yaml"
    write_skill(source, "dev/tool", "name: tool\ndescription: Tool.", "Use `terminal`.\n")
    override_path.write_text(contents, encoding="utf-8")

    code = main(["scan", "--source", str(source), "--overrides", str(override_path), "--json"])

    assert code == 2
    payload = json.loads(capsys.readouterr().err)
    assert "duplicate YAML mapping key" in payload["error"]


def test_cli_json_failure_is_structured(tmp_path: Path, capsys) -> None:
    from hermes_jack_in.cli import main

    source = tmp_path / "skills"
    write_skill(source, "bad/skill", "name: skill\ndescription: Bad.")

    code = main([
        "sync",
        "--source",
        str(source),
        "--destination",
        str(source / "nested"),
        "--json",
    ])
    captured = capsys.readouterr()
    payload = json.loads(captured.err)

    assert code == 2
    assert "must not overlap" in payload["error"]
