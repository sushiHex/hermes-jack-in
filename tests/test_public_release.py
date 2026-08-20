from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised by the Python 3.10 job
    import tomli as tomllib


ROOT = Path(__file__).parents[1]
PUBLIC_GOVERNANCE_FILES = {
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "SECURITY.md",
}
PRIVATE_ONLY_DIRECTORIES = {".hermes", "reports"}


def project_metadata() -> dict[str, object]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def candidate_text_files() -> tuple[Path, ...]:
    ignored_parts = {".git", ".venv", "__pycache__", ".pytest_cache"}
    return tuple(
        path
        for path in ROOT.rglob("*")
        if path.is_file() and not ignored_parts.intersection(path.parts)
    )


def test_public_package_identity_is_hermes_jack_in() -> None:
    metadata = project_metadata()
    project = metadata["project"]
    scripts = project["scripts"]
    wheel = metadata["tool"]["hatch"]["build"]["targets"]["wheel"]
    sdist = metadata["tool"]["hatch"]["build"]["targets"]["sdist"]

    assert project["name"] == "hermes-jack-in"
    assert project["description"] == "Safely share Hermes Agent skills with Claude Code"
    assert scripts == {
        "hermes-jack-in": "hermes_jack_in.cli:main",
        "hermes-jack-in-guard": "hermes_jack_in.guard:main",
    }
    assert wheel["packages"] == ["src/hermes_jack_in"]
    assert {"/.github", "/CLAUDE.md"} <= set(sdist["exclude"])
    assert (ROOT / "src/hermes_jack_in/__init__.py").is_file()
    assert not (ROOT / "src/hermes_claude_skills").exists()


def test_public_package_metadata_is_complete() -> None:
    project = project_metadata()["project"]

    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["authors"] == [{"name": "sushiHex"}]
    assert project["requires-python"] == ">=3.10"
    assert set(project["urls"]) == {
        "Changelog",
        "Documentation",
        "Issues",
        "Repository",
    }
    assert all("sushiHex/hermes-jack-in" in url for url in project["urls"].values())
    classifiers = set(project["classifiers"])
    assert "License :: OSI Approved :: MIT License" in classifiers
    assert "Programming Language :: Python :: 3 :: Only" in classifiers
    assert "Typing :: Typed" not in classifiers
    assert not (ROOT / "src/hermes_jack_in/py.typed").exists()


def test_public_governance_and_ci_files_exist() -> None:
    assert PUBLIC_GOVERNANCE_FILES <= {
        path.name for path in ROOT.iterdir() if path.is_file()
    }
    assert (ROOT / ".github/workflows/ci.yml").is_file()
    assert (ROOT / ".github/ISSUE_TEMPLATE/bug_report.yml").is_file()
    assert (ROOT / ".github/ISSUE_TEMPLATE/config.yml").is_file()
    assert (ROOT / ".github/pull_request_template.md").is_file()
    assert (ROOT / ".gitattributes").is_file()
    assert (ROOT / "overrides.example.yaml").is_file()
    assert (ROOT / "docs/VALIDATION.md").is_file()


def test_dependabot_covers_actions_uv_and_build_constraints() -> None:
    dependabot = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")

    assert "package-ecosystem: github-actions" in dependabot
    assert "package-ecosystem: uv" in dependabot
    assert "package-ecosystem: pip" in dependabot


def test_ci_uses_immutable_action_commits_and_qualifies_tags() -> None:
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    uses = re.findall(r"^\s*uses:\s*[^@\s]+@([^\s#]+)", workflow, flags=re.MULTILINE)

    assert uses
    assert all(re.fullmatch(r"[0-9a-f]{40}", revision) for revision in uses)
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262" in workflow
    assert "astral-sh/setup-uv@d0cc045d04ccac9d8b7881df0226f9e82c39688e" in workflow
    assert 'tags: ["v*"]' in workflow


def test_changelog_describes_the_candidate_as_unreleased() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "## [Unreleased]" in changelog
    assert "## [0.1.0]" not in changelog
    assert "/releases/tag/v0.1.0" not in changelog


def test_private_release_evidence_is_not_present() -> None:
    assert not [name for name in PRIVATE_ONLY_DIRECTORIES if (ROOT / name).exists()]
    assert not list(ROOT.glob(".audit-*.md"))


def test_candidate_has_no_configured_private_identity() -> None:
    forbidden = tuple(
        marker.strip().lower()
        for marker in os.environ.get(
            "HERMES_JACK_IN_FORBIDDEN_MARKERS",
            "",
        ).split(",")
        if marker.strip()
    )
    if not forbidden:
        pytest.skip("no private release markers configured")
    findings: list[str] = []
    for path in candidate_text_files():
        try:
            text = path.read_text(encoding="utf-8").lower()
        except (UnicodeDecodeError, OSError):
            continue
        if any(term in text for term in forbidden):
            findings.append(path.relative_to(ROOT).as_posix())

    assert findings == []


def test_readme_states_beta_scope_and_non_affiliation() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "# Hermes Jack-In" in readme
    assert "Beta" in readme
    assert "one-way projection only" in readme
    assert "does not capture Claude feedback" in readme
    assert "POSIX path checks do not retain directory authority" in readme
    assert "The repository is public" in readme
    assert "After the repository is public" not in readme
    assert "No package or repository has been published" not in readme
    normalized = " ".join(readme.split())
    assert (
        "independent third-party project and is not affiliated with or endorsed by "
        "Anthropic or Nous Research"
    ) in normalized
    assert "Windows" in readme
    assert "Linux" in readme
    assert "macOS" in readme
