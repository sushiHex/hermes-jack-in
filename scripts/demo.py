"""Run a disposable end-to-end Hermes Jack-In demonstration."""

from __future__ import annotations

import json
import os
import subprocess  # nosec B404 - fixed public CLI invocation only
import sys
import tempfile
from pathlib import Path
from typing import Any

PASS_LINE = "Hermes Jack-In demo: PASS"
MANIFEST_NAME = ".hermes-claude-skills-adapter.json"
SENTINEL = b"unmanaged owner content\n"


class DemoError(RuntimeError):
    """Raised when one public demo contract is not satisfied."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DemoError(message)


def _run_json(step: str, *arguments: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(  # nosec B603 - argv is fixed and shell-free
        [sys.executable, "-m", "hermes_jack_in", *arguments],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise DemoError(f"{step} exited with status {completed.returncode}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DemoError(f"{step} did not return valid JSON") from exc
    if not isinstance(payload, dict):
        raise DemoError(f"{step} returned a non-object JSON payload")
    return payload


def _expect_action(payload: dict[str, Any], operation: str) -> None:
    actions = payload.get("actions")
    if not isinstance(actions, list) or len(actions) != 1:
        raise DemoError(f"{operation} action count was not one")
    action = actions[0]
    if not isinstance(action, dict):
        raise DemoError(f"{operation} action was not an object")
    _require(action.get("operation") == operation, f"expected {operation} action")
    _require(action.get("name") == "demo", f"{operation} action did not target demo")
    _require(action.get("mode") == "copy-fallback", f"{operation} action did not use copy mode")


def _run_demo(root: Path) -> None:
    source = root / "source"
    skill = source / "productivity" / "demo"
    references = skill / "references"
    destination = root / "project" / ".claude" / "skills"
    unmanaged = destination / "oracle" / "owner.txt"

    references.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: demo\n"
        "description: Use when demonstrating a safe skill projection.\n"
        "---\n\n"
        "# Demo\n\n"
        "Read `references/guide.md`.\n",
        encoding="utf-8",
    )
    (references / "guide.md").write_text("Projected supporting content.\n", encoding="utf-8")
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_bytes(SENTINEL)

    common = ("--source", str(source), "--destination", str(destination))
    scan = _run_json("scan", "scan", "--source", str(source), "--json")
    _require(scan.get("issues") == [], "scan reported issues")
    _require(scan.get("summary") == {"directly-portable": 1}, "scan classification changed")
    print("PASS scan: one directly portable skill")

    preview = _run_json("copy-mode preview", "sync", *common, "--copy", "--dry-run", "--json")
    _expect_action(preview, "install")
    _require(not (destination / "demo").exists(), "dry-run mutated the destination")
    _require(unmanaged.read_bytes() == SENTINEL, "dry-run changed unmanaged content")
    print("PASS preview: one copy-mode install, no mutation")

    installed = _run_json("sync", "sync", *common, "--copy", "--json")
    _expect_action(installed, "install")
    _require((destination / "demo" / "SKILL.md").is_file(), "projected skill is missing")
    _require((destination / "demo" / "references" / "guide.md").is_file(), "supporting content is missing")
    _require((destination / MANIFEST_NAME).is_file(), "ownership manifest is missing")
    _require(unmanaged.read_bytes() == SENTINEL, "sync changed unmanaged content")
    print("PASS sync: managed skill installed; unmanaged sibling preserved")

    checked = _run_json("check", "check", *common, "--json")
    _require(checked.get("issues") == [], "check reported drift")
    unchanged = _run_json("unchanged sync", "sync", *common, "--copy", "--json")
    _require(unchanged.get("actions") == [], "unchanged sync was not a no-op")
    print("PASS check: no drift; unchanged sync is a no-op")

    removed = _run_json("remove", "remove", "--destination", str(destination), "--json")
    _expect_action(removed, "remove")
    _require(not (destination / "demo").exists(), "managed skill remained after removal")
    _require(not (destination / MANIFEST_NAME).exists(), "ownership manifest remained after removal")
    _require(unmanaged.read_bytes() == SENTINEL, "removal changed unmanaged content")
    print("PASS remove: managed output removed; unmanaged sibling preserved")


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="hermes-jack-in-demo-") as temporary:
            _run_demo(Path(temporary))
    except DemoError as exc:
        print(f"Hermes Jack-In demo: FAIL: {exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.SubprocessError):
        print("Hermes Jack-In demo: FAIL: temporary demo execution failed", file=sys.stderr)
        return 1
    print(PASS_LINE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
