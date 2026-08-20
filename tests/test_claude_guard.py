from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "src" / "hermes_jack_in" / "guard.py"
GUIDE = Path(__file__).parents[1] / "docs" / "CLAUDE_CODE_GUIDE.md"
SOURCE_ROOT = "C:/Users/example/repos/hermes-profile/.hermes/skills"
CLAUDE_ROOT = "C:/Users/example/.claude/skills"
PROTECTED_ROOTS = (SOURCE_ROOT, CLAUDE_ROOT)


def _evaluate(guard, event):
    return guard.evaluate(event, protected_roots=PROTECTED_ROOTS)


DOCUMENTED_GUARD_EXEMPTIONS = (
    "ls -la C:/Users/example/repos/hermes-profile/.hermes/skills",
    "pwd -P",
    "git status --short",
    "env -u PYTHONPATH uv run hermes-jack-in scan --source "
    "C:/Users/example/repos/hermes-profile/.hermes/skills --overrides "
    "C:/Users/example/repos/hermes-jack-in/overrides.example.yaml --json",
    "env -u PYTHONPATH uv run hermes-jack-in plan --source "
    "C:/Users/example/repos/hermes-profile/.hermes/skills --destination "
    "C:/Users/example/repos/hermes-jack-in/.canary/project/.claude/skills "
    "--overrides C:/Users/example/repos/hermes-jack-in/overrides.example.yaml --json",
    "env -u PYTHONPATH uv run hermes-jack-in check --source "
    "C:/Users/example/repos/hermes-profile/.hermes/skills --destination "
    "C:/Users/example/repos/hermes-jack-in/.canary/project/.claude/skills "
    "--overrides C:/Users/example/repos/hermes-jack-in/overrides.example.yaml --json",
)


def load_guard():
    spec = importlib.util.spec_from_file_location("protect_hermes_skills", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guard_configuration_requires_unique_existing_physical_directories(
    tmp_path: Path,
) -> None:
    guard = load_guard()
    source = tmp_path / "source"
    source.mkdir()

    assert guard._validated_guard_roots((source,)) == (source.resolve(),)
    for roots in ((), (Path("relative"),), (tmp_path / "missing",), (source, source)):
        with pytest.raises(ValueError):
            guard._validated_guard_roots(roots)


def test_guard_configuration_rejects_symlinked_root(tmp_path: Path) -> None:
    guard = load_guard()
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")

    with pytest.raises(ValueError):
        guard._validated_guard_roots((alias,))


def test_guard_main_denies_bash_when_roots_are_not_configured(monkeypatch, capsys) -> None:
    guard = load_guard()
    event = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))

    assert guard.main([]) == 0

    decision = json.loads(capsys.readouterr().out)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "configuration" in decision["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_guard_main_denies_bash_when_evaluation_raises(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    guard = load_guard()
    protected = tmp_path / "protected"
    protected.mkdir()
    event = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("unexpected guard failure")

    monkeypatch.setattr(guard, "evaluate", fail)

    assert guard.main(["--protected-root", str(protected)]) == 0

    decision = json.loads(capsys.readouterr().out)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "evaluation failed" in decision["hookSpecificOutput"][
        "permissionDecisionReason"
    ].lower()


def test_guard_main_decodes_hook_input_as_utf8(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    guard = load_guard()
    protected = tmp_path / "protected"
    protected.mkdir()
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": f'rm -rf "{protected}/\u0410"'},
    }
    payload = json.dumps(event, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(payload), encoding="cp1252"),
    )

    assert guard.main(["--protected-root", str(protected)]) == 0

    decision = json.loads(capsys.readouterr().out)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_main_denies_bash_when_root_validation_raises(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    guard = load_guard()
    protected = tmp_path / "protected"
    protected.mkdir()
    event = {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(event)))

    def fail(*args: object, **kwargs: object) -> None:
        raise PermissionError("root metadata unavailable")

    monkeypatch.setattr(guard, "_validated_guard_roots", fail)

    assert guard.main(["--protected-root", str(protected)]) == 0

    decision = json.loads(capsys.readouterr().out)
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "configuration" in decision["hookSpecificOutput"][
        "permissionDecisionReason"
    ].lower()


def test_guard_canonicalizes_windows_short_aliases_before_comparison(
    tmp_path: Path,
    monkeypatch,
) -> None:
    guard = load_guard()
    physical = tmp_path / "runneradmin" / "source"
    physical.mkdir(parents=True)
    alias = tmp_path / "RUNNER~1" / "source"

    def expand_alias(path: Path) -> Path:
        return physical if path == alias else path

    monkeypatch.setattr(guard, "_windows_long_path", expand_alias)
    decision = guard.evaluate(
        {
            "tool_name": "Bash",
            "tool_input": {"command": f'rm -rf -- "{alias.as_posix()}"'},
        },
        protected_roots=(physical,),
    )

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_native_and_posix_canonical_paths() -> None:
    guard = load_guard()

    for command in (
        r'python -c "open(\"C:\\Users\\example\\repos\\hermes-profile\\.hermes\\skills\\x\\SKILL.md\", \"w\")"',
        "python -c 'open(\"/c/Users/example/repos/hermes-profile/.hermes/skills/x/SKILL.md\",\"w\")'",
    ):
        decision = _evaluate(guard, {"tool_name": "Bash", "tool_input": {"command": command}})
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_more_bash_literal_expansions() -> None:
    guard = load_guard()

    commands = (
        "rm -rf ../hermes-profile/.hermes/skill{s..s}",
        "rm -rf ../hermes-profile/.hermes/skill[^x]",
        'cp payload -t../hermes-profile/.hermes/skill"s"',
        'rm -rf ~example/repos/hermes-profile/.hermes/skill"s"',
        "rm -rf /c/Users/example/repos/hermes-profile/.hermes/$'skills'",
        "rm -rf C:/Users/example/repos/hermes-profile/.hermes/skill\\\ns",
        "bash -O extglob -c 'rm -rf ../hermes-profile/.hermes/@(skills)'",
        "bash -O extglob -c 'rm -rf ../hermes-profile/.hermes/+(skill)s'",
        "bash -O extglob -c 'rm -rf ../hermes-profile/.hermes/+(skill|s)'",
        "bash -O extglob -c 'rm -rf ../hermes-profile/.hermes/skill?(x)s'",
        "bash -O extglob -c 'rm -rf ../hermes-profile/.hermes/!(decoy)'",
        "bash -O globstar -c 'rm -rf C:/Users/**/.hermes/skills'",
        (
            "bash -O globstar -c 'rm -rf "
            "C:/Users/example/repos/hermes-jack-in/**/"
            "../../../../repos/hermes-profile/.hermes/skills'"
        ),
        "bash -c 'rm -rf ../hermes-profile/.hermes/skill\"s\"'",
        "bash -c 'rm -rf ../hermes-profile/.hermes/{skills,decoy}'",
    )
    for command in commands:
        decision = _evaluate(guard,
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/hermes-jack-in",
                "tool_input": {"command": command},
            }
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_literal_expansion_that_exceeds_analysis_bound() -> None:
    guard = load_guard()
    alternatives = "|".join([*(f"decoy{index}" for index in range(65)), "skills"])
    command = f"bash -O extglob -c 'rm -rf ../hermes-profile/.hermes/@({alternatives})'"

    decision = _evaluate(guard,
        {
            "tool_name": "Bash",
            "cwd": "C:/Users/example/repos/hermes-jack-in",
            "tool_input": {"command": command},
        }
    )

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"

    long_glob = "rm -rf " + "/".join(["segment"] * 3_000) + "/*"
    decision = _evaluate(guard, {"tool_name": "Bash", "tool_input": {"command": long_glob}})
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"

    unicode_range = "rm -rf ../hermes-profile/.hermes/skill[A-\U0010ffff]"
    decision = _evaluate(guard, {"tool_name": "Bash", "tool_input": {"command": unicode_range}})
    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_shell_expansion_and_path_options() -> None:
    guard = load_guard()

    commands = (
        "rm -rf ../hermes-profile/.hermes/skill?",
        "rm -rf ../hermes-profile/.hermes/{skills,decoy}",
        'cp --target-directory=../hermes-profile/.hermes/skill"s" payload',
    )
    for command in commands:
        decision = _evaluate(guard,
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/hermes-jack-in",
                "tool_input": {"command": command},
            }
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_glob_protection_does_not_depend_on_live_filesystem(monkeypatch) -> None:
    guard = load_guard()
    monkeypatch.setattr(
        guard.glob,
        "glob",
        lambda pattern: (_ for _ in ()).throw(AssertionError(f"filesystem glob used: {pattern}")),
    )

    for command in (
        "rm -rf ../hermes-profile/.hermes/skill?",
        "rm -rf ../hermes-profile/.hermes/skill[^x]",
        "rm -rf ../hermes-profile/.hermes/skill[[:lower:]]",
        "printf safe > ../hermes-profile/.hermes/skill[[:lower:]]/probe",
        "rm -rf ../hermes-profile/.hermes/skill[x[:lower:]]",
        "rm -rf ../hermes-profile/.hermes/skill[[:lower:]x]",
        "rm -rf ../hermes-profile/.hermes/skill[![:upper:]]",
        "rm -rf ../hermes-profile/.hermes/skill[^[:upper:]]",
        "rm -rf ../hermes-profile/.hermes/skill[[:alpha:][:digit:]]",
        "rm -rf ../hermes[[:punct:]]profile/.hermes/skills",
        "rm -rf ../hermes[[:print:]]profile/.hermes/skills",
        "rm -rf ../hermes[[:graph:]]profile/.hermes/skills",
        "rm -rf ../hermes-profile/.hermes/skill[[=s=]]",
        "rm -rf ../hermes-profile/.hermes/skill[[.s.]]",
    ):
        decision = _evaluate(guard,
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/hermes-jack-in",
                "tool_input": {"command": command},
            }
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_shell_literal_edge_cases() -> None:
    guard = load_guard()

    cases = (
        (
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos",
                "tool_input": {"command": "rm -rf hermes-profile"},
            }
        ),
        (
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/hermes-profile",
                "tool_input": {"command": "rm -rf ."},
            }
        ),
        (
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/hermes-jack-in",
                "tool_input": {"command": 'rm -rf ../hermes-profile/.hermes/skill"s"'},
            }
        ),
        (
            {
                "tool_name": "Bash",
                "cwd": "/c/Users/example/repos/hermes-profile/.hermes/skills/research/example",
                "tool_input": {"command": "sed -i s/a/b/ SKILL.md"},
            }
        ),
        (
            {
                "tool_name": "Bash",
                "tool_input": {"command": "rm -rf C:/Users/example/repos/hermes-profile"},
            }
        ),
    )
    for event in cases:
        decision = _evaluate(guard, event)
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_compound_cd_and_protected_ancestor_mutations() -> None:
    guard = load_guard()

    cases = (
        (
            "C:/Users/example/repos/hermes-jack-in",
            "cd ../hermes-profile/.hermes && rm -rf skills/research/example",
        ),
        (
            "C:/Users/example/repos/hermes-jack-in",
            "cd C:/Users/example/.claude && sed -i s/a/b/ skills/codebase-inspection/SKILL.md",
        ),
        (
            "C:/Users/example/repos/hermes-jack-in",
            "rm -rf ../hermes-profile/.hermes",
        ),
        (
            "C:/Users/example/repos/hermes-jack-in",
            "rm -rf ../hermes-profile",
        ),
        (
            "C:/Users/example/repos/hermes-jack-in",
            "ls . && rm -rf C:/Users/example/repos",
        ),
        (
            "C:/Users/example/repos/hermes-jack-in",
            "find C:/Users/example/repos -delete",
        ),
    )
    for cwd, command in cases:
        decision = _evaluate(guard,
            {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_bare_relative_paths_from_protected_or_parent_cwd() -> None:
    guard = load_guard()

    cases = (
        (
            "C:/Users/example/.claude/skills/codebase-inspection",
            "sed -i s/old/new/ SKILL.md",
        ),
        (
            "C:/Users/example/repos/hermes-profile/.hermes/skills",
            "rm -rf research/example",
        ),
        (
            "C:/Users/example/repos",
            "rm -rf hermes-profile/.hermes/skills/research/example",
        ),
    )
    for cwd, command in cases:
        decision = _evaluate(guard,
            {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}
        )
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_sibling_relative_runtime_path_from_hook_cwd() -> None:
    guard = load_guard()

    decision = _evaluate(guard,
        {
            "tool_name": "Bash",
            "cwd": "C:/Users/example/repos/hermes-jack-in",
            "tool_input": {"command": "rm -rf ../hermes-profile/.hermes/skills/research/example"},
        }
    )

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_sibling_relative_root_when_hook_cwd_is_missing() -> None:
    guard = load_guard()

    for command in (
        "rm -rf ../hermes-profile/.hermes/skills",
        "rm -rf ../hermes-profile/.hermes/skills/research/example",
        'rm -rf "../hermes-profile/.hermes/skills"',
        "rm -rf ./../hermes-profile/.hermes/skills",
    ):
        decision = _evaluate(guard, {"tool_name": "Bash", "tool_input": {"command": command}})
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_writes_through_personal_skill_alias() -> None:
    guard = load_guard()

    decision = _evaluate(guard,
        {
            "tool_name": "Bash",
            "cwd": "C:/Users/example",
            "tool_input": {
                "command": "sed -i s/old/new/ C:/Users/example/.claude/skills/codebase-inspection/SKILL.md"
            },
        }
    )

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_relative_runtime_skill_path() -> None:
    guard = load_guard()

    decision = _evaluate(guard,
        {"tool_name": "Bash", "tool_input": {"command": "rm -rf .hermes/skills/research/example"}}
    )

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_guard_denies_locale_translation_quote_bypasses_before_analysis() -> None:
    guard = load_guard()
    reason = (
        "Bash locale-translation quotes are outside the guard's literal analysis; "
        "refusing execution."
    )
    commands = (
        'rm -rf $".hermes/skills/research/example"',
        'LC_ALL=C rm -rf $".hermes/skills/research/example"',
        "bash -c 'rm -rf $\".hermes/skills/research/example\"'",
        "LC_ALL=en_US.UTF-8 bash -c 'rm -rf $\".hermes/skills/research/example\"'",
        'ls $".hermes/skills/research/example"',
    )

    for command in commands:
        decision = _evaluate(guard,
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/hermes-profile",
                "tool_input": {"command": command},
            }
        )
        assert decision is not None, command
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert decision["hookSpecificOutput"]["permissionDecisionReason"] == reason


def test_guard_rejects_benign_locale_quote_markers_fail_closed() -> None:
    guard = load_guard()
    reason = (
        "Bash locale-translation quotes are outside the guard's literal analysis; "
        "refusing execution."
    )
    commands = (
        "printf '%s\\n' '$\"quoted as ordinary text\"'",
        "printf '%s\\n' \\$\"escaped marker\"",
        'printf harmless # $"marker in a shell comment"',
    )

    for command in commands:
        decision = _evaluate(guard,
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/example",
                "tool_input": {"command": command},
            }
        )
        assert decision is not None, command
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert decision["hookSpecificOutput"]["permissionDecisionReason"] == reason


def test_guard_denies_shell_evaluation_before_read_only_exemption() -> None:
    guard = load_guard()
    root = "C:/Users/example/repos/hermes-profile/.hermes/skills"
    home_root = "$HOME/repos/hermes-profile/.hermes/skills"
    commands = (
        ("command substitution", f'ls "$(touch {root}/command-substitution)"'),
        ("backticks", f"pwd `touch {root}/backticks`"),
        ("input process substitution", f'git status <(touch "{home_root}/input-process")'),
        ("output process substitution", f'ls >(touch "{home_root}/output-process")'),
        (
            "arithmetic substitution",
            f'pwd "$(( values[$(touch "{home_root}/arithmetic")] ))"',
        ),
        ("HOME expansion", f'ls "{home_root}"'),
        ("braced HOME expansion", 'ls "${HOME}/repos/hermes-profile/.hermes/skills"'),
        ("parameter default expansion", f'ls "${{HERMES_SKILLS:-{root}}}"'),
        ("indirect parameter expansion", 'git status "${!HERMES_SKILLS}"'),
        ("ANSI-C evaluation", "ls $'C:/Users/example/repos/hermes-profile/.hermes/skill\\x73'"),
        ("tilde expansion", "ls ~/repos/hermes-profile/.hermes/skills"),
        ("brace expansion", f"ls {root[:-6]}{{skills,decoy}}"),
        ("pathname expansion", f"ls {root[:-1]}?"),
        ("extended glob evaluation", f"ls {root[:-6]}@(skills)"),
        (
            "adapter command substitution",
            "env -u PYTHONPATH uv run hermes-jack-in check "
            f'--source "$(printf %s {root})"',
        ),
    )

    bypasses = [
        label
        for label, command in commands
        if _evaluate(guard, {"tool_name": "Bash", "tool_input": {"command": command}})
        is None
    ]

    assert bypasses == []


def test_guard_allows_only_genuinely_literal_read_only_commands() -> None:
    guard = load_guard()

    for command in DOCUMENTED_GUARD_EXEMPTIONS:
        assert _evaluate(guard, {"tool_name": "Bash", "tool_input": {"command": command}}) is None


def test_guide_locks_exact_literal_guard_exemptions() -> None:
    guard = load_guard()
    guide = GUIDE.read_text(encoding="utf-8")
    start_marker = "<!-- guard-exemptions:start -->"
    end_marker = "<!-- guard-exemptions:end -->"

    assert start_marker in guide
    assert end_marker in guide
    section = guide.split(start_marker, 1)[1].split(end_marker, 1)[0]
    commands = tuple(
        line
        for line in section.splitlines()
        if line and not line.startswith("```")
    )

    assert commands == DOCUMENTED_GUARD_EXEMPTIONS
    for command in commands:
        assert guard.LITERAL_READ_ONLY_COMMAND.fullmatch(command) is not None
        assert guard._is_read_only_invocation(guard._read_only_tokens(command))
        assert _evaluate(guard, {"tool_name": "Bash", "tool_input": {"command": command}}) is None


def test_guard_allows_unrelated_bash_and_non_bash_tools(tmp_path: Path) -> None:
    guard = load_guard()
    protected_roots = (tmp_path / "source", tmp_path / "destination")

    def _evaluate(_guard, event):
        return guard.evaluate(event, protected_roots=protected_roots)

    assert (
        guard.evaluate(
            {"tool_name": "Bash", "tool_input": {"command": "pytest -q"}},
            protected_roots=protected_roots,
        )
        is None
    )
    for command, cwd in (
        ("echo scripts/*.py", "C:/Users/example/repos/hermes-jack-in"),
        ("gcc -o out src/*.c", "C:/Users/example/repos/example"),
        ("cat notes?.txt", "C:/Users/example"),
        (
            "rm -rf ../hermes-profile/.hermes/skill[[:space:]]",
            "C:/Users/example/repos/hermes-jack-in",
        ),
        (
            "bash -O extglob -c 'rm -rf ../hermes-profile/.hermes/+(decoy)'",
            "C:/Users/example/repos/hermes-jack-in",
        ),
        ("printf x > /tmp/" + "*" * 80 + "z", "C:/Users/example"),
    ):
        assert (
            guard.evaluate(
                {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}},
                protected_roots=protected_roots,
            )
            is None
        )
    assert (
        _evaluate(guard,
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "ls -d C:/Users/example/repos/hermes-profile/.hermes/skills"
                },
            }
        )
        is None
    )
    assert (
        _evaluate(guard,
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/hermes-jack-in",
                "tool_input": {
                    "command": "env -u PYTHONPATH uv run hermes-jack-in scan "
                    "--source C:/Users/example/repos/hermes-profile/.hermes/skills --json"
                },
            }
        )
        is None
    )
    assert (
        _evaluate(guard,
            {
                "tool_name": "Bash",
                "cwd": "C:/Users/example/repos/example",
                "tool_input": {"command": "git add ./.claude/skills/example/SKILL.md"},
            }
        )
        is None
    )
    assert (
        _evaluate(guard,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "ls C:/Users/example/repos"},
            }
        )
        is None
    )
    assert (
        _evaluate(guard,
            {
                "tool_name": "Bash",
                "tool_input": {"command": "printf harmless > /tmp/guard-fp-probe"},
            }
        )
        is None
    )
    assert (
        _evaluate(guard,
            {
                "tool_name": "Bash",
                "tool_input": {
                    "command": "printf 'hermes-profile/.hermes/skills' > /tmp/guard-fp-probe"
                },
            }
        )
        is None
    )
    assert (
        _evaluate(guard,
            {
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": "C:/Users/example/repos/hermes-profile/.hermes/skills/x/SKILL.md"
                },
            }
        )
        is None
    )


def test_guard_does_not_exempt_mutating_adapter_commands() -> None:
    guard = load_guard()
    root = "C:/Users/example/repos/hermes-profile/.hermes/skills"
    personal = "C:/Users/example/.claude/skills"
    commands = (
        f"uv run hermes-jack-in sync --source {root} --destination {personal}",
        f"ls\nrm -rf {root}",
        f"ls\rrm -rf {root}",
        f"uv run hermes-jack-in scan --source {root} --json\n"
        f"uv run hermes-jack-in sync --source {root} --destination {personal}",
        f"ls -la > {root}/x/SKILL.md",
        f"git diff --output={root}/x/SKILL.md",
    )

    for command in commands:
        decision = _evaluate(guard, {"tool_name": "Bash", "tool_input": {"command": command}})
        assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
