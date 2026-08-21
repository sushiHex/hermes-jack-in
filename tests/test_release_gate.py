import importlib.util
import json
import re
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest


RELEASE_GATE = Path(__file__).parents[1] / "scripts" / "release_gate.py"
BUILD_CONSTRAINTS = Path(__file__).resolve().parents[1] / "build-constraints.txt"
EXPECTED_BUILD_REQUIREMENTS = {
    "hatchling": (
        "1.31.0",
        {
            "6b48ad4068a482ed7239b3a8215bc55b47aad3345d58dfc94e553c5d2d46211b",
            "aac80bec8b6fe35e8480f1c335be8910fa210a0e6f735a139be205dadcacb544",
        },
    ),
    "packaging": (
        "26.3",
        {
            "94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79",
            "d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c",
        },
    ),
    "pathspec": (
        "1.1.1",
        {
            "17db5ecd524104a120e173814c90367a96a98d07c45b2e10c2f3919fff91bf5a",
            "a00ce642f577bf7f473932318056212bc4f8bfdf53128c78bbd5af0b9b20b189",
        },
    ),
    "pluggy": (
        "1.6.0",
        {
            "7dcc130b76258d33b90f61b658791dede3486c3e6bfb003ee5c9bfb396dd22f3",
            "e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746",
        },
    ),
    "tomli": (
        "2.0.2",
        {
            "2ebe24485c53d303f690b0ec092806a085f07af5a5aa1464f3931eec36caaa38",
            "d46d457a85337051c36524bc5349dd91b1877838e2979ac5ced3e710ed8a60ed",
        },
    ),
    "trove-classifiers": (
        "2026.6.1.19",
        {
            "ab4c4ec93cc4a4e7815fa759906e05e6bb3f2fbd92ea0f897288c6a43efd15b3",
            "c5132b4b61a829d11cfbd2d72e97f20a45ed6edb95e45c5efdeb5e00836b2745",
        },
    ),
}


def load_release_gate():
    spec = importlib.util.spec_from_file_location("release_gate", RELEASE_GATE)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_gate_has_authoritative_lint_scope() -> None:
    release_gate = load_release_gate()

    assert release_gate.LINT_TARGETS == ("src", "tests", "scripts")


def test_release_gate_allowlists_the_feedback_contract_artifacts() -> None:
    release_gate = load_release_gate()

    assert "hermes_jack_in/feedback.py" in release_gate.WHEEL_PACKAGE_MEMBERS
    assert {
        "docs/FEEDBACK_PROPOSALS.md",
        "src/hermes_jack_in/feedback.py",
        "tests/test_feedback.py",
    } <= release_gate.SDIST_MEMBERS


def test_release_gate_reads_private_markers_from_the_environment(monkeypatch) -> None:
    monkeypatch.setenv(
        "HERMES_JACK_IN_FORBIDDEN_MARKERS",
        "private-owner, private-machine",
    )
    release_gate = load_release_gate()

    assert release_gate.forbidden_public_bytes() == (
        b"private-owner",
        b"private-machine",
    )
    with pytest.raises(RuntimeError, match="private identity leaked"):
        release_gate._inspect_public_bytes("README.md", b"contains private-owner")


def test_release_gate_derives_project_identity_from_pyproject(tmp_path: Path) -> None:
    release_gate = load_release_gate()
    metadata = tmp_path / "pyproject.toml"
    metadata.write_text(
        '[build-system]\nrequires = ["hatchling"]\n\n'
        '[project]\nname = "example-project"\nversion = "9.8.7"\n',
        encoding="utf-8",
    )

    assert release_gate.project_identity(metadata) == ("example-project", "9.8.7")


def test_release_gate_uses_literal_hash_constrained_build_command() -> None:
    release_gate = load_release_gate()
    dist_dir = Path("dist")

    assert release_gate.build_command("uv", dist_dir) == [
        "uv",
        "build",
        "--no-sources",
        "--build-constraints",
        BUILD_CONSTRAINTS,
        "--require-hashes",
        "--out-dir",
        dist_dir,
    ]


def test_release_gate_checks_lock_before_frozen_sync() -> None:
    source = RELEASE_GATE.read_text(encoding="utf-8")
    lock_check = 'run([uv, "lock", "--check"], cwd=candidate, env=env)'
    frozen_sync = 'run([uv, "sync", "--frozen"], cwd=candidate, env=env)'

    assert lock_check in source
    assert frozen_sync in source
    assert source.index(lock_check) < source.index(frozen_sync)


def test_release_gate_guard_canary_uses_physical_root_and_expected_reason() -> None:
    source = RELEASE_GATE.read_text(encoding="utf-8")

    assert 'fixture = (temp_root / f"installed-{label}").resolve()' in source
    assert "source_root = source.parent.resolve(strict=True)" in source
    assert '"skill trees are protected"' in source


def test_installer_requires_hashes_for_artifact_runtime_and_build_inputs(tmp_path: Path) -> None:
    release_gate = load_release_gate()
    command = release_gate.install_command(
        "uv",
        tmp_path / "python",
        tmp_path / "artifact.txt",
        runtime_constraints=tmp_path / "runtime.txt",
        build_constraints=tmp_path / "build.txt",
    )

    assert command == [
        "uv",
        "pip",
        "install",
        "--python",
        tmp_path / "python",
        "--require-hashes",
        "--constraint",
        tmp_path / "runtime.txt",
        "--build-constraints",
        tmp_path / "build.txt",
        "--requirement",
        tmp_path / "artifact.txt",
    ]


def test_build_constraints_pin_and_hash_the_backend_closure() -> None:
    blocks = []
    current = []
    for line in BUILD_CONSTRAINTS.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current.append(stripped.removesuffix("\\").rstrip())
        if not stripped.endswith("\\"):
            blocks.append(" ".join(current))
            current = []

    assert not current
    assert len(blocks) == len(EXPECTED_BUILD_REQUIREMENTS)

    actual_requirements = {}
    for block in blocks:
        requirement, _, _ = block.partition(" --hash=")
        name, separator, version_and_marker = requirement.partition("==")
        assert separator == "=="
        version = version_and_marker.split(";", 1)[0].strip()
        hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})(?:\s|$)", block)
        assert len(hashes) == len(set(hashes)) == 2
        actual_requirements[name] = (version, set(hashes))

    assert actual_requirements == EXPECTED_BUILD_REQUIREMENTS
    tomli = next(block for block in blocks if block.startswith("tomli=="))
    assert 'python_version < "3.11"' in tomli


def test_release_gate_requires_one_wheel_and_one_sdist(tmp_path: Path) -> None:
    release_gate = load_release_gate()
    wheel = tmp_path / "package-1.0-py3-none-any.whl"
    sdist = tmp_path / "package-1.0.tar.gz"
    wheel.touch()
    sdist.touch()

    assert release_gate.built_artifacts(tmp_path) == (wheel, sdist)

    (tmp_path / "duplicate-1.0-py3-none-any.whl").touch()
    with pytest.raises(RuntimeError, match="exactly one wheel and one sdist"):
        release_gate.built_artifacts(tmp_path)


def test_release_gate_uses_native_venv_executable_paths(tmp_path: Path) -> None:
    release_gate = load_release_gate()

    assert release_gate.venv_python(tmp_path, os_name="nt") == (
        tmp_path / "Scripts" / "python.exe"
    )
    assert release_gate.venv_console(tmp_path, os_name="nt") == (
        tmp_path / "Scripts" / "hermes-jack-in.exe"
    )
    assert release_gate.venv_python(tmp_path, os_name="posix") == (
        tmp_path / "bin" / "python"
    )
    assert release_gate.venv_console(tmp_path, os_name="posix") == (
        tmp_path / "bin" / "hermes-jack-in"
    )


def test_release_gate_uses_native_guard_executable_paths(tmp_path: Path) -> None:
    release_gate = load_release_gate()

    assert release_gate.venv_guard(tmp_path, os_name="nt") == (
        tmp_path / "Scripts" / "hermes-jack-in-guard.exe"
    )
    assert release_gate.venv_guard(tmp_path, os_name="posix") == (
        tmp_path / "bin" / "hermes-jack-in-guard"
    )


def test_source_archive_extraction_is_regular_file_only(tmp_path: Path) -> None:
    import io

    release_gate = load_release_gate()
    safe_archive = tmp_path / "safe.tar"
    payload = b"public candidate\n"
    with tarfile.open(safe_archive, "w") as archive:
        directory = tarfile.TarInfo("docs")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        regular = tarfile.TarInfo("docs/readme.txt")
        regular.size = len(payload)
        archive.addfile(regular, io.BytesIO(payload))

    destination = tmp_path / "safe"
    release_gate._safe_extract(safe_archive, destination)
    assert (destination / "docs/readme.txt").read_bytes() == payload

    existing = tmp_path / "existing"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        release_gate._safe_extract(safe_archive, existing)

    link_archive = tmp_path / "link.tar"
    with tarfile.open(link_archive, "w") as archive:
        link = tarfile.TarInfo("escape")
        link.type = tarfile.SYMTYPE
        link.linkname = "../outside"
        archive.addfile(link)

    rejected = tmp_path / "rejected"
    with pytest.raises(RuntimeError, match="unsupported source archive member"):
        release_gate._safe_extract(link_archive, rejected)
    assert not (rejected / "escape").exists()


def test_release_gate_rejects_unsafe_archive_member_names() -> None:
    release_gate = load_release_gate()

    for name in (
        "../escape",
        "/absolute",
        "C:/absolute",
        "safe/../../escape",
        "safe\\name",
        "safe//name",
        "safe/./name",
        "./safe",
    ):
        with pytest.raises(RuntimeError):
            release_gate.validate_archive_member(name)
    release_gate.validate_archive_member("hermes_jack_in-0.2.0/src/module.py")


def test_release_gate_rejects_duplicate_wheel_member_names(tmp_path: Path) -> None:
    release_gate = load_release_gate()
    wheel = tmp_path / "duplicate.whl"
    with pytest.warns(UserWarning, match="Duplicate name"):
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr("hermes_jack_in/__init__.py", b"first")
            archive.writestr("hermes_jack_in/__init__.py", b"second")

    with pytest.raises(RuntimeError, match="duplicate wheel member"):
        release_gate._inspect_wheel(wheel)


def test_release_gate_requires_byte_reproducible_artifacts(tmp_path: Path) -> None:
    release_gate = load_release_gate()
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "package.whl").write_bytes(b"same")
    (second / "package.whl").write_bytes(b"same")

    release_gate.require_reproducible_artifacts(first, second)
    (second / "package.whl").write_bytes(b"different")
    with pytest.raises(RuntimeError, match="not reproducible"):
        release_gate.require_reproducible_artifacts(first, second)


def test_public_sdist_allowlist_excludes_private_evidence() -> None:
    release_gate = load_release_gate()

    assert ".hermes" not in release_gate.SDIST_TOP_LEVEL
    assert "reports" not in release_gate.SDIST_TOP_LEVEL
    assert "CLAUDE.md" not in release_gate.SDIST_TOP_LEVEL
    assert {".gitignore", "README.md", "LICENSE", "SECURITY.md", "src", "tests"} <= set(
        release_gate.SDIST_TOP_LEVEL
    )


def test_public_sdist_rejects_unlisted_nested_evidence(tmp_path: Path) -> None:
    release_gate = load_release_gate()
    sdist = tmp_path / "hermes_jack_in-0.2.0.tar.gz"
    payload = b"private audit\n"
    with tarfile.open(sdist, "w:gz") as archive:
        member = tarfile.TarInfo("hermes_jack_in-0.2.0/docs/private-audit.md")
        member.size = len(payload)
        import io

        archive.addfile(member, io.BytesIO(payload))

    with pytest.raises(RuntimeError, match="unexpected sdist members"):
        release_gate._inspect_sdist(sdist)


def _write_complete_test_sdist(release_gate, path: Path, extras: list[tarfile.TarInfo]) -> None:
    import io

    root = "hermes_jack_in-0.2.0"
    with tarfile.open(path, "w:gz") as archive:
        for relative in sorted(release_gate.SDIST_MEMBERS):
            payload = f"public fixture: {relative}\n".encode()
            member = tarfile.TarInfo(f"{root}/{relative}")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
        for member in extras:
            payload = b"private audit\n" if member.isfile() else None
            if payload is not None:
                member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload) if payload is not None else None)


def test_public_sdist_rejects_members_outside_the_distribution_root(tmp_path: Path) -> None:
    release_gate = load_release_gate()
    sdist = tmp_path / "hermes_jack_in-0.2.0.tar.gz"
    _write_complete_test_sdist(
        release_gate,
        sdist,
        [tarfile.TarInfo("private-audit.txt")],
    )

    with pytest.raises(RuntimeError, match="outside expected sdist root"):
        release_gate._inspect_sdist(sdist)


@pytest.mark.parametrize("member_type", [tarfile.SYMTYPE, tarfile.LNKTYPE])
def test_public_sdist_rejects_link_members(
    tmp_path: Path,
    member_type: bytes,
) -> None:
    release_gate = load_release_gate()
    sdist = tmp_path / "hermes_jack_in-0.2.0.tar.gz"
    link = tarfile.TarInfo("hermes_jack_in-0.2.0/docs/unlisted-link")
    link.type = member_type
    link.linkname = "../../outside"
    _write_complete_test_sdist(release_gate, sdist, [link])

    with pytest.raises(RuntimeError, match="unsupported sdist member"):
        release_gate._inspect_sdist(sdist)


def test_guard_event_runner_parses_decisions_and_empty_allows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release_gate = load_release_gate()
    calls = []
    outputs = iter(
        [
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                    }
                }
            ),
            "",
        ]
    )

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout=next(outputs))

    monkeypatch.setattr(release_gate.subprocess, "run", fake_run)
    event = {"tool_name": "Bash", "tool_input": {"command": "rm -rf protected"}}
    decision = release_gate.run_guard_event(
        ["guard", "--protected-root", tmp_path],
        event,
        cwd=tmp_path,
        env={"PATH": "test"},
    )
    allowed = release_gate.run_guard_event(
        ["guard", "--protected-root", tmp_path],
        {"tool_name": "Bash", "tool_input": {"command": "pwd -P"}},
        cwd=tmp_path,
        env={"PATH": "test"},
    )

    assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert allowed is None
    assert json.loads(calls[0][1]["input"]) == event
    assert calls[0][1]["check"] is True


def test_release_gate_removes_environment_overrides(monkeypatch) -> None:
    release_gate = load_release_gate()
    monkeypatch.setenv("PYTHONPATH", "outside")
    monkeypatch.setenv("PYTEST_ADDOPTS", "--ignore=tests")
    monkeypatch.setenv("UV_BUILD_CONSTRAINT", "outside.txt")
    monkeypatch.setenv("UV_NO_BUILD_ISOLATION", "1")
    monkeypatch.setenv("UV_NO_VERIFY_HASHES", "1")
    monkeypatch.setenv("UV_REQUIRE_HASHES", "0")

    env = release_gate.clean_environment()

    assert "PYTHONPATH" not in env
    assert "PYTEST_ADDOPTS" not in env
    assert "UV_BUILD_CONSTRAINT" not in env
    assert "UV_NO_BUILD_ISOLATION" not in env
    assert "UV_NO_VERIFY_HASHES" not in env
    assert "UV_REQUIRE_HASHES" not in env
