"""Run the hermetic local public-release gate."""

from __future__ import annotations

import configparser
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
# The release gate launches fixed argv only; shell=True is never used.
import subprocess  # nosec B404
import tarfile
import tempfile
import zipfile
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
BUILD_CONSTRAINTS = ROOT / "build-constraints.txt"
LINT_TARGETS = ("src", "tests", "scripts")
SDIST_TOP_LEVEL = (
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "build-constraints.txt",
    "docs",
    "overrides.example.yaml",
    "pyproject.toml",
    "scripts",
    "src",
    "tests",
    "uv.lock",
)
SDIST_MEMBERS = {
    ".gitignore",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "PKG-INFO",
    "README.md",
    "SECURITY.md",
    "SUPPORT.md",
    "build-constraints.txt",
    "docs/CLAUDE_CODE_GUIDE.md",
    "docs/MAPPING_RULES.md",
    "docs/RELEASING.md",
    "docs/VALIDATION.md",
    "overrides.example.yaml",
    "pyproject.toml",
    "scripts/protect_hermes_skills.py",
    "scripts/release_gate.py",
    "src/hermes_jack_in/__init__.py",
    "src/hermes_jack_in/__main__.py",
    "src/hermes_jack_in/cli.py",
    "src/hermes_jack_in/core.py",
    "src/hermes_jack_in/guard.py",
    "src/hermes_jack_in/sync.py",
    "tests/fixtures/manifests/schema-v1-copy.json",
    "tests/fixtures/manifests/schema-v2-copy.json",
    "tests/test_classification.py",
    "tests/test_claude_guard.py",
    "tests/test_cli.py",
    "tests/test_core.py",
    "tests/test_public_release.py",
    "tests/test_release_gate.py",
    "tests/test_sync.py",
    "uv.lock",
}
SDIST_DIRECTORIES = {
    parent.as_posix()
    for member in SDIST_MEMBERS
    for parent in PurePosixPath(member).parents
    if parent != PurePosixPath(".")
}
WHEEL_PACKAGE_MEMBERS = {
    "hermes_jack_in/__init__.py",
    "hermes_jack_in/__main__.py",
    "hermes_jack_in/cli.py",
    "hermes_jack_in/core.py",
    "hermes_jack_in/guard.py",
    "hermes_jack_in/sync.py",
}
FORBIDDEN_PUBLIC_MARKERS_ENV = "HERMES_JACK_IN_FORBIDDEN_MARKERS"
SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"gh[opusr]_[A-Za-z0-9_]{20,}"),
    re.compile(rb"sk-[A-Za-z0-9]{20,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
)


def forbidden_public_bytes() -> tuple[bytes, ...]:
    return tuple(
        marker.strip().encode()
        for marker in os.environ.get(FORBIDDEN_PUBLIC_MARKERS_ENV, "").split(",")
        if marker.strip()
    )
PROVENANCE_CHECK = """
import sysconfig
from pathlib import Path

import hermes_jack_in

module_path = Path(hermes_jack_in.__file__).resolve()
site_packages = {
    Path(sysconfig.get_path(name)).resolve()
    for name in ("purelib", "platlib")
}
if not any(module_path.is_relative_to(path) for path in site_packages):
    raise SystemExit(f"import did not resolve from site-packages: {module_path}")
print(f"import provenance: {module_path}")
"""


def run(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: dict[str, str],
) -> None:
    args = [os.fspath(part) for part in command]
    rendered = subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)
    print(f"+ {rendered}", flush=True)
    subprocess.run(args, cwd=cwd, env=env, check=True)  # nosec B603


def capture(
    command: Sequence[str | os.PathLike[str]],
    *,
    cwd: Path,
    env: dict[str, str],
) -> str:
    args = [os.fspath(part) for part in command]
    result = subprocess.run(  # nosec B603
        args,
        cwd=cwd,
        env=env,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def run_guard_event(
    command: Sequence[str | os.PathLike[str]],
    event: dict[str, object],
    *,
    cwd: Path,
    env: dict[str, str],
) -> dict[str, object] | None:
    args = [os.fspath(part) for part in command]
    result = subprocess.run(  # nosec B603
        args,
        cwd=cwd,
        env=env,
        input=json.dumps(event),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output = result.stdout.strip()
    if not output:
        return None
    decision = json.loads(output)
    if not isinstance(decision, dict):
        raise RuntimeError("installed guard returned a non-object decision")
    return decision


def project_identity(metadata: Path = ROOT / "pyproject.toml") -> tuple[str, str]:
    text = metadata.read_text(encoding="utf-8")
    match = re.search(r"(?ms)^\[project\]\s*(.*?)(?=^\[|\Z)", text)
    if match is None:
        raise RuntimeError("pyproject.toml has no [project] table")
    block = match.group(1)

    def field(name: str) -> str:
        value = re.search(rf'(?m)^{re.escape(name)}\s*=\s*"([^"]+)"\s*$', block)
        if value is None:
            raise RuntimeError(f"pyproject.toml project.{name} is missing or invalid")
        return value.group(1)

    return field("name"), field("version")


def built_artifacts(dist_dir: Path) -> tuple[Path, Path]:
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise RuntimeError(
            "release build must produce exactly one wheel and one sdist; "
            f"found {len(wheels)} wheel(s) and {len(sdists)} sdist(s)"
        )
    return wheels[0], sdists[0]


def build_command(
    uv: str | os.PathLike[str],
    dist_dir: Path,
    *,
    constraints: Path = BUILD_CONSTRAINTS,
) -> list[str | os.PathLike[str]]:
    return [
        uv,
        "build",
        "--no-sources",
        "--build-constraints",
        constraints,
        "--require-hashes",
        "--out-dir",
        dist_dir,
    ]


def install_command(
    uv: str | os.PathLike[str],
    python: Path,
    artifact_requirements: Path,
    *,
    runtime_constraints: Path,
    build_constraints: Path,
) -> list[str | os.PathLike[str]]:
    return [
        uv,
        "pip",
        "install",
        "--python",
        python,
        "--require-hashes",
        "--constraint",
        runtime_constraints,
        "--build-constraints",
        build_constraints,
        "--requirement",
        artifact_requirements,
    ]


def venv_python(venv_dir: Path, *, os_name: str = os.name) -> Path:
    if os_name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def venv_console(venv_dir: Path, *, os_name: str = os.name) -> Path:
    if os_name == "nt":
        return venv_dir / "Scripts" / "hermes-jack-in.exe"
    return venv_dir / "bin" / "hermes-jack-in"


def venv_guard(venv_dir: Path, *, os_name: str = os.name) -> Path:
    if os_name == "nt":
        return venv_dir / "Scripts" / "hermes-jack-in-guard.exe"
    return venv_dir / "bin" / "hermes-jack-in-guard"


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    for variable in (
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTEST_ADDOPTS",
        "UV_BUILD_CONSTRAINT",
        "UV_NO_BUILD_ISOLATION",
        "UV_NO_VERIFY_HASHES",
        "UV_REQUIRE_HASHES",
        "VIRTUAL_ENV",
    ):
        env.pop(variable, None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def require_command(name: str) -> str:
    command = shutil.which(name)
    if command is None:
        raise RuntimeError(f"required command is not available: {name}")
    return command


def validate_archive_member(name: str) -> None:
    if "\\" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise RuntimeError(f"unsafe archive member: {name}")
    lexical_name = name[:-1] if name.endswith("/") else name
    path = PurePosixPath(lexical_name)
    if (
        not lexical_name
        or path.as_posix() != lexical_name
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise RuntimeError(f"unsafe archive member: {name}")


def _safe_extract(source_archive: Path, destination: Path) -> None:
    with tarfile.open(source_archive, "r:") as archive:
        members = archive.getmembers()
        seen: set[str] = set()
        for member in members:
            validate_archive_member(member.name)
            if member.name in seen:
                raise RuntimeError(f"duplicate source archive member: {member.name}")
            seen.add(member.name)
            if member.issym() or member.islnk() or not (
                member.isdir() or member.isfile()
            ):
                raise RuntimeError(f"unsupported source archive member: {member.name}")

        destination.mkdir(parents=True, exist_ok=False)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"unable to read source archive member: {member.name}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            if os.name != "nt":
                target.chmod(stat.S_IMODE(member.mode))
            os.utime(target, (member.mtime, member.mtime))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_reproducible_artifacts(first: Path, second: Path) -> None:
    first_files = {path.name: _sha256(path) for path in first.iterdir() if path.is_file()}
    second_files = {path.name: _sha256(path) for path in second.iterdir() if path.is_file()}
    if first_files != second_files:
        raise RuntimeError(
            f"release artifacts are not reproducible: {first_files} != {second_files}"
        )


def _inspect_public_bytes(name: str, content: bytes) -> None:
    lowered = content.lower()
    for forbidden in forbidden_public_bytes():
        if forbidden.lower() in lowered:
            raise RuntimeError(f"private identity leaked into artifact member: {name}")
    for pattern in SECRET_PATTERNS:
        if pattern.search(content):
            raise RuntimeError(f"credential-like content in artifact member: {name}")


def _inspect_wheel(wheel: Path) -> None:
    project_name, project_version = project_identity()
    dist_info = f"{project_name.replace('-', '_')}-{project_version}.dist-info/"
    with zipfile.ZipFile(wheel) as archive:
        raw_names = archive.namelist()
        canonical_names: set[str] = set()
        for name in raw_names:
            validate_archive_member(name)
            canonical = PurePosixPath(name.rstrip("/")).as_posix()
            if canonical in canonical_names:
                raise RuntimeError(f"duplicate wheel member: {name}")
            canonical_names.add(canonical)
            _inspect_public_bytes(name, archive.read(name))
        names = set(raw_names)

        package_members = {name for name in names if name.startswith("hermes_jack_in/")}
        if package_members != WHEEL_PACKAGE_MEMBERS:
            raise RuntimeError(
                f"unexpected wheel package members: {sorted(package_members)}"
            )
        unexpected_top = {
            name
            for name in names
            if not name.startswith("hermes_jack_in/") and not name.startswith(dist_info)
        }
        if unexpected_top:
            raise RuntimeError(f"unexpected wheel top-level members: {sorted(unexpected_top)}")

        metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
        entry_names = [name for name in names if name.endswith(".dist-info/entry_points.txt")]
        license_names = [name for name in names if ".dist-info/licenses/LICENSE" in name]
        if len(metadata_names) != 1 or len(entry_names) != 1 or len(license_names) != 1:
            raise RuntimeError("wheel metadata, entry points, or license are incomplete")

        metadata = BytesParser(policy=policy.default).parsebytes(
            archive.read(metadata_names[0])
        )
        if metadata["Name"] != project_name or metadata["Version"] != project_version:
            raise RuntimeError("wheel project identity is incorrect")
        if metadata["License-Expression"] != "MIT":
            raise RuntimeError("wheel SPDX license metadata is incorrect")

        entries = configparser.ConfigParser()
        entries.read_string(archive.read(entry_names[0]).decode("utf-8"))
        expected = {
            "hermes-jack-in": "hermes_jack_in.cli:main",
            "hermes-jack-in-guard": "hermes_jack_in.guard:main",
        }
        if dict(entries["console_scripts"]) != expected:
            raise RuntimeError("wheel console entry points are incorrect")


def _inspect_sdist(sdist: Path) -> None:
    project_name, project_version = project_identity()
    expected_root = f"{project_name.replace('-', '_')}-{project_version}"
    with tarfile.open(sdist, "r:gz") as archive:
        roots: set[str] = set()
        files: set[str] = set()
        seen: set[str] = set()
        for member in archive.getmembers():
            validate_archive_member(member.name)
            canonical_name = PurePosixPath(member.name.rstrip("/")).as_posix()
            if canonical_name in seen:
                raise RuntimeError(f"duplicate sdist member: {member.name}")
            seen.add(canonical_name)
            parts = PurePosixPath(member.name).parts
            if not parts or parts[0] != expected_root:
                raise RuntimeError(f"member outside expected sdist root: {member.name}")
            roots.add(parts[0])
            if len(parts) == 1:
                if not member.isdir():
                    raise RuntimeError(f"unsupported sdist member: {member.name}")
                continue
            relative = PurePosixPath(*parts[1:]).as_posix()
            if parts[1] not in SDIST_TOP_LEVEL:
                raise RuntimeError(f"unexpected sdist member: {member.name}")
            if member.isdir():
                if relative not in SDIST_DIRECTORIES:
                    raise RuntimeError(f"unexpected sdist directory: {member.name}")
                continue
            if not member.isfile():
                raise RuntimeError(f"unsupported sdist member: {member.name}")
            files.add(relative)
            stream = archive.extractfile(member)
            if stream is None:
                raise RuntimeError(f"cannot inspect sdist member: {member.name}")
            _inspect_public_bytes(member.name, stream.read())
        if roots != {expected_root}:
            raise RuntimeError(f"unexpected sdist root: {sorted(roots)}")
        if files != SDIST_MEMBERS:
            raise RuntimeError(
                "unexpected sdist members: "
                f"added={sorted(files - SDIST_MEMBERS)} "
                f"missing={sorted(SDIST_MEMBERS - files)}"
            )


def inspect_artifacts(wheel: Path, sdist: Path) -> None:
    _inspect_wheel(wheel)
    _inspect_sdist(sdist)


def _require_clean_candidate(git: str, env: dict[str, str]) -> str:
    run([git, "diff", "--check"], cwd=ROOT, env=env)
    run([git, "diff", "--cached", "--check"], cwd=ROOT, env=env)
    status = capture(
        [git, "status", "--porcelain=v1", "--untracked-files=all"],
        cwd=ROOT,
        env=env,
    )
    if status:
        raise RuntimeError(f"release source is not clean:\n{status}")
    return capture([git, "rev-parse", "HEAD"], cwd=ROOT, env=env)


def _archive_candidate(git: str, destination: Path, env: dict[str, str]) -> Path:
    source_archive = destination / "candidate.tar"
    candidate = destination / "candidate"
    run(
        [git, "archive", "--format=tar", "--output", source_archive, "HEAD"],
        cwd=ROOT,
        env=env,
    )
    _safe_extract(source_archive, candidate)
    return candidate


def _exercise_install(
    uv: str,
    artifact: Path,
    *,
    label: str,
    temp_root: Path,
    candidate_python: Path,
    runtime_constraints: Path,
    build_constraints: Path,
    env: dict[str, str],
) -> None:
    venv_dir = temp_root / f"venv-{label}"
    run([uv, "venv", "--python", candidate_python, venv_dir], cwd=temp_root, env=env)
    isolated_python = venv_python(venv_dir)
    artifact_requirements = temp_root / f"{label}-artifact.txt"
    artifact_requirements.write_text(
        f"hermes-jack-in @ {artifact.resolve().as_uri()} "
        f"--hash=sha256:{_sha256(artifact)}\n",
        encoding="utf-8",
    )
    run(
        install_command(
            uv,
            isolated_python,
            artifact_requirements,
            runtime_constraints=runtime_constraints,
            build_constraints=build_constraints,
        ),
        cwd=temp_root,
        env=env,
    )
    run([uv, "pip", "check", "--python", isolated_python], cwd=temp_root, env=env)
    run([isolated_python, "-I", "-c", PROVENANCE_CHECK], cwd=temp_root, env=env)

    console = venv_console(venv_dir)
    guard = venv_guard(venv_dir)
    run([console, "--version"], cwd=temp_root, env=env)
    run([console, "--help"], cwd=temp_root, env=env)
    run([guard, "--help"], cwd=temp_root, env=env)
    run([isolated_python, "-I", "-m", "hermes_jack_in", "--help"], cwd=temp_root, env=env)

    fixture = (temp_root / f"installed-{label}").resolve()
    source = fixture / "source" / "demo"
    destination = fixture / "destination"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "SKILL.md").write_text(
        "---\nname: demo\ndescription: Installed artifact lifecycle canary.\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    sentinel = destination / "unmanaged-sentinel.txt"
    sentinel.write_bytes(b"preserve-me")
    source_root = source.parent.resolve(strict=True)
    deny = run_guard_event(
        [guard, "--protected-root", source_root],
        {
            "tool_name": "Bash",
            "tool_input": {"command": f"rm -rf -- {source_root}"},
            "cwd": str(fixture),
        },
        cwd=fixture,
        env=env,
    )
    deny_output = deny.get("hookSpecificOutput") if deny is not None else None
    deny_reason = (
        deny_output.get("permissionDecisionReason")
        if isinstance(deny_output, dict)
        else None
    )
    if (
        not isinstance(deny_output, dict)
        or deny_output.get("permissionDecision") != "deny"
        or not isinstance(deny_reason, str)
        or "skill trees are protected" not in deny_reason
    ):
        raise RuntimeError("installed guard did not deny protected-root mutation")
    allow = run_guard_event(
        [guard, "--protected-root", source_root],
        {
            "tool_name": "Bash",
            "tool_input": {"command": "pwd -P"},
            "cwd": str(fixture),
        },
        cwd=fixture,
        env=env,
    )
    if allow is not None:
        raise RuntimeError("installed guard denied a literal read-only control")
    run([console, "scan", "--source", source_root, "--json"], cwd=fixture, env=env)
    run(
        [console, "sync", "--source", source_root, "--destination", destination, "--copy", "--json"],
        cwd=fixture,
        env=env,
    )
    run(
        [console, "check", "--source", source_root, "--destination", destination, "--json"],
        cwd=fixture,
        env=env,
    )
    run(
        [console, "sync", "--source", source_root, "--destination", destination, "--copy", "--json"],
        cwd=fixture,
        env=env,
    )
    run([console, "remove", "--destination", destination, "--json"], cwd=fixture, env=env)
    if sentinel.read_bytes() != b"preserve-me" or (destination / "demo").exists():
        raise RuntimeError("installed artifact lifecycle did not preserve unmanaged data")


def main() -> int:
    env = clean_environment()
    uv = require_command("uv")
    git = require_command("git")
    commit = _require_clean_candidate(git, env)

    with tempfile.TemporaryDirectory(prefix="hermes-jack-in-release-") as temporary:
        temp_root = Path(temporary)
        for variable in ("TEMP", "TMP", "TMPDIR"):
            env[variable] = str(temp_root)
        env["PYTHONPYCACHEPREFIX"] = str(temp_root / "pycache")
        env["SOURCE_DATE_EPOCH"] = capture(
            [git, "show", "-s", "--format=%ct", commit], cwd=ROOT, env=env
        )

        candidate = _archive_candidate(git, temp_root, env)
        run([uv, "lock", "--check"], cwd=candidate, env=env)
        run([uv, "sync", "--frozen"], cwd=candidate, env=env)
        candidate_venv = candidate / ".venv"
        candidate_python = venv_python(candidate_venv)

        run(
            [candidate_python, "-m", "pytest", "-q", "--basetemp", temp_root / "pytest"],
            cwd=candidate,
            env=env,
        )
        run(
            [candidate_python, "-m", "ruff", "check", "--no-cache", *LINT_TARGETS],
            cwd=candidate,
            env=env,
        )
        run(
            [candidate_python, "-m", "compileall", "-q", *LINT_TARGETS],
            cwd=candidate,
            env=env,
        )

        first_dist = temp_root / "dist-first"
        second_dist = temp_root / "dist-second"
        candidate_constraints = candidate / "build-constraints.txt"
        run(
            build_command(uv, first_dist, constraints=candidate_constraints),
            cwd=candidate,
            env=env,
        )
        run(
            build_command(uv, second_dist, constraints=candidate_constraints),
            cwd=candidate,
            env=env,
        )
        require_reproducible_artifacts(first_dist, second_dist)
        wheel, sdist = built_artifacts(first_dist)
        inspect_artifacts(wheel, sdist)
        print(f"reproducible wheel: {wheel.name} sha256={_sha256(wheel)}", flush=True)
        print(f"reproducible sdist: {sdist.name} sha256={_sha256(sdist)}", flush=True)

        constraints = temp_root / "runtime-constraints.txt"
        run(
            [
                uv,
                "export",
                "--frozen",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements.txt",
                "--output-file",
                constraints,
            ],
            cwd=candidate,
            env=env,
        )
        _exercise_install(
            uv,
            wheel,
            label="wheel",
            temp_root=temp_root,
            candidate_python=candidate_python,
            runtime_constraints=constraints,
            build_constraints=candidate_constraints,
            env=env,
        )
        _exercise_install(
            uv,
            sdist,
            label="sdist",
            temp_root=temp_root,
            candidate_python=candidate_python,
            runtime_constraints=constraints,
            build_constraints=candidate_constraints,
            env=env,
        )

    print(f"release gate passed for {commit}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
