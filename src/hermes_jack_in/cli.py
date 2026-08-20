from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any, Sequence

import yaml

from .core import Classification, scan_library
from .sync import AdapterError, check_library, remove_library, sync_library


class _UniqueKeyLoader(yaml.SafeLoader):
    def construct_mapping(
        self, node: yaml.nodes.MappingNode, deep: bool = False
    ) -> dict[Any, Any]:
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as exc:
                raise yaml.constructor.ConstructorError(
                    "while constructing an override mapping",
                    node.start_mark,
                    "found an unhashable mapping key",
                    key_node.start_mark,
                ) from exc
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing an override mapping",
                    node.start_mark,
                    f"duplicate YAML mapping key: {key!r}",
                    key_node.start_mark,
                )
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_overrides(path: str | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    # _UniqueKeyLoader subclasses SafeLoader; yaml.load is required to retain
    # duplicate-key rejection while preserving SafeLoader's constructor set.
    data = yaml.load(  # nosec B506
        Path(path).read_text(encoding="utf-8"),
        Loader=_UniqueKeyLoader,
    )
    if (
        not isinstance(data, dict)
        or set(data) != {"skills"}
        or not isinstance(data["skills"], dict)
    ):
        raise AdapterError("override file must contain exactly one top-level 'skills' mapping")
    if not all(isinstance(name, str) for name in data["skills"]):
        raise AdapterError("override skill names must be strings")
    return data["skills"]


def _common_source(parser: argparse.ArgumentParser, *, destination: bool = False) -> None:
    parser.add_argument("--source", required=True, type=Path)
    if destination:
        parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--overrides")
    parser.add_argument("--json", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes-jack-in")
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('hermes-jack-in')}")
    commands = parser.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="inventory and classify Hermes skills")
    _common_source(scan)
    plan = commands.add_parser("plan", help="show proposed mappings and exclusions")
    _common_source(plan, destination=True)
    sync = commands.add_parser("sync", help="install or update owned Claude skills")
    _common_source(sync, destination=True)
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--copy", action="store_true", help="materialize instead of using symlinks")
    sync.add_argument(
        "--allow-empty",
        action="store_true",
        help="allow an empty desired inventory to remove every managed skill",
    )
    check = commands.add_parser("check", help="detect drift and invalid state")
    _common_source(check, destination=True)
    remove = commands.add_parser("remove", help="remove only adapter-owned artifacts")
    remove.add_argument("--destination", required=True, type=Path)
    remove.add_argument("--dry-run", action="store_true")
    remove.add_argument("--json", action="store_true")
    return parser


def _scan_payload(source: Path, overrides: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scan = scan_library(source, overrides=overrides)
    counts = Counter(skill.classification.value for skill in scan.skills)
    return {
        "source": str(scan.source),
        "summary": dict(sorted(counts.items())),
        "issues": list(scan.issues),
        "skills": [
            {
                "name": skill.name,
                "source": skill.relative_dir.as_posix(),
                "classification": skill.classification.value,
                "reasons": list(skill.reasons),
                "blocked": skill.blocked,
            }
            for skill in scan.skills
        ],
    }


def _emit(payload: dict[str, Any], use_json: bool) -> None:
    if use_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if "summary" in payload:
        print("Compatibility summary:")
        for key, value in payload["summary"].items():
            print(f"  {key}: {value}")
    for action in payload.get("actions", []):
        print(f"{action['operation']}: {action['name']} ({action['mode']})")
    for issue in payload.get("issues", []):
        if isinstance(issue, str):
            print(f"issue: {issue}")
        else:
            print(f"{issue['kind']}: {issue['name']}: {issue['detail']}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "remove":
            result = remove_library(args.destination, dry_run=args.dry_run)
            payload = {"actions": [asdict(action) for action in result.actions]}
            _emit(payload, args.json)
            return 0

        overrides = load_overrides(args.overrides)
        if args.command == "scan":
            payload = _scan_payload(args.source, overrides)
            _emit(payload, args.json)
            return 1 if payload["issues"] else 0

        if args.command == "plan":
            inventory = _scan_payload(args.source, overrides)
            result = sync_library(args.source, args.destination, overrides=overrides, dry_run=True)
            payload = {
                "actions": [asdict(action) for action in result.actions],
                "excluded": [
                    skill for skill in inventory["skills"]
                    if skill["classification"] == Classification.EXCLUDE.value
                ],
                "issues": inventory["issues"],
            }
            _emit(payload, args.json)
            return 0

        if args.command == "sync":
            result = sync_library(
                args.source,
                args.destination,
                overrides=overrides,
                dry_run=args.dry_run,
                prefer_symlinks=not args.copy,
                allow_empty=args.allow_empty,
            )
            payload = {"actions": [asdict(action) for action in result.actions]}
            _emit(payload, args.json)
            return 0

        if args.command == "check":
            result = check_library(args.source, args.destination, overrides=overrides)
            payload = {"issues": [asdict(issue) for issue in result.issues]}
            _emit(payload, args.json)
            return 1 if result.issues else 0
    except (AdapterError, OSError, ValueError, yaml.YAMLError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
