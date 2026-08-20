from __future__ import annotations

import hashlib
import html
import os
import re
import stat
import struct
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import unquote, urlsplit

import yaml


LEGACY_PROVENANCE_ID = "hermes-claude-skills-adapter"
# This hidden generated marker predates the public product name. Retaining it
# avoids changing the desired bytes of already proved materialized artifacts.
DIRECT_FRONTMATTER = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "allowed-tools",
}
CLAUDE_FRONTMATTER = DIRECT_FRONTMATTER | {
    "when_to_use",
    "argument-hint",
    "arguments",
    "disable-model-invocation",
    "user-invocable",
    "disallowed-tools",
    "model",
    "effort",
    "paths",
}
EXECUTION_BEARING_FRONTMATTER = {"agent", "background", "context", "hooks", "shell"}
INTERPRETED_FRONTMATTER = CLAUDE_FRONTMATTER | EXECUTION_BEARING_FRONTMATTER
HERMES_TOOLS = {
    "execute_code",
    "terminal",
    "read_file",
    "write_file",
    "patch",
    "search_files",
    "delegate_task",
    "clarify",
    "cronjob",
    "skill_view",
    "skill_manage",
    "memory",
    "session_search",
    "web_search",
    "web_extract",
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_snapshot",
    "browser_vision",
}
HERMES_ONLY_PATTERNS = {
    "Hermes runtime": re.compile(
        r"(?i)(?:\bHERMES_(?:HOME|SKILL_DIR|SESSION_ID)\b|(?<![\w.-])\.hermes(?=$|[/\\]))"
    ),
    "Hermes services": re.compile(r"(?i)\b(?:Hermes Agent|Hermes gateway|skill_manage|skill_view|curator)\b"),
}
PORTABLE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}
ALLOWED_EXTERNAL_SCHEMES = {"data", "http", "https", "mailto"}
SINGLE_URL_HTML_ATTRIBUTES = {
    "action",
    "background",
    "cite",
    "classid",
    "codebase",
    "data",
    "formaction",
    "href",
    "icon",
    "longdesc",
    "manifest",
    "poster",
    "profile",
    "src",
    "usemap",
    "xlink:href",
}
AMBIGUOUS_URL_HTML_ATTRIBUTES = {
    "archive",
    "imagesrcset",
    "ping",
    "srcdoc",
    "srcset",
}


def _html_attribute_pattern(names: set[str]) -> re.Pattern[str]:
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(
        rf'''(?is)(?<![\w:-])(?P<name>{alternatives})\s*=\s*'''
        r'''(?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<unquoted>[^\s"'=<>`]*))'''
    )


SINGLE_URL_HTML_ATTRIBUTE = _html_attribute_pattern(SINGLE_URL_HTML_ATTRIBUTES)
AMBIGUOUS_URL_HTML_ATTRIBUTE = _html_attribute_pattern(AMBIGUOUS_URL_HTML_ATTRIBUTES)
COMMONMARK_URI_AUTOLINK = re.compile(
    r"(?i)<([A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\x00-\x20]*)>"
)
HTML_CHARACTER_REFERENCE = re.compile(
    r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]*);?"
)
RAW_HTML_TAG_START = re.compile(
    r"<(?:/?[A-Za-z][A-Za-z0-9-]*)(?=[\s/>])"
)


def validate_skill_name(name: object) -> str:
    if (
        not isinstance(name, str)
        or not PORTABLE_NAME.fullmatch(name)
        or name.endswith("-")
        or "--" in name
        or name in WINDOWS_RESERVED_NAMES
    ):
        raise ValueError(f"invalid portable skill name: {name!r}")
    return name


def _portable_metadata(value: object) -> bool:
    return isinstance(value, Mapping) and all(
        isinstance(key, str) and isinstance(item, str)
        for key, item in value.items()
    )


def _invalid_optional_frontmatter(frontmatter: Mapping[str, object]) -> tuple[str, ...]:
    invalid: list[str] = []
    if "license" in frontmatter and not isinstance(frontmatter["license"], str):
        invalid.append("license")
    compatibility = frontmatter.get("compatibility")
    if "compatibility" in frontmatter and (
        not isinstance(compatibility, str) or len(compatibility) > 500
    ):
        invalid.append("compatibility")
    if "allowed-tools" in frontmatter and not isinstance(frontmatter["allowed-tools"], str):
        invalid.append("allowed-tools")
    return tuple(sorted(invalid))


def _is_windows_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError, OSError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _raise_walk_error(error: OSError) -> None:
    raise error


def iter_skill_files(root: Path) -> tuple[Path, ...]:
    files: list[Path] = []
    for current, directories, filenames in os.walk(
        root, followlinks=False, onerror=_raise_walk_error
    ):
        current_path = Path(current)
        for directory in directories:
            candidate = current_path / directory
            if candidate.is_symlink():
                raise ValueError(f"source symlink is not allowed: {candidate}")
            if _is_windows_reparse_point(candidate):
                raise ValueError(f"source reparse point is not allowed: {candidate}")
        for filename in filenames:
            candidate = current_path / filename
            if candidate.is_symlink():
                raise ValueError(f"source symlink is not allowed: {candidate}")
            if _is_windows_reparse_point(candidate):
                raise ValueError(f"source reparse point is not allowed: {candidate}")
            files.append(candidate)
    return tuple(sorted(files))


def hidden_entries(root: Path) -> tuple[Path, ...]:
    entries: list[Path] = []
    for current, directories, filenames in os.walk(
        root, followlinks=False, onerror=_raise_walk_error
    ):
        current_path = Path(current)
        hidden_directories = [name for name in directories if name.startswith(".")]
        entries.extend(current_path / name for name in hidden_directories)
        entries.extend(current_path / name for name in filenames if name.startswith("."))
        directories[:] = [name for name in directories if not name.startswith(".")]
    return tuple(entries)


class Classification(str, Enum):
    DIRECT = "directly-portable"
    CONVERT = "metadata-path-conversion"
    ADAPT = "semantic-adaptation"
    EXCLUDE = "hermes-only"


class OverrideError(ValueError):
    """An explicit adaptation rule no longer matches its source skill."""


@dataclass(frozen=True)
class Skill:
    source_dir: Path
    relative_dir: Path
    name: str
    description: str
    frontmatter: dict[str, object]
    body: str
    classification: Classification
    reasons: tuple[str, ...]
    blocked: bool = False

    @property
    def source_hash(self) -> str:
        digest = hashlib.sha256()

        def add_record(record_type: bytes, payload: bytes) -> None:
            digest.update(record_type)
            digest.update(struct.pack(">Q", len(payload)))
            digest.update(payload)

        add_record(b"V", b"hermes-skill-source-v2")
        for path in iter_skill_files(self.source_dir):
            add_record(b"P", path.relative_to(self.source_dir).as_posix().encode("utf-8"))
            add_record(b"F", path.read_bytes())
        return digest.hexdigest()


@dataclass(frozen=True)
class ScanResult:
    source: Path
    skills: tuple[Skill, ...]
    issues: tuple[str, ...] = ()


def _parse_skill(path: Path) -> tuple[dict[str, object], str]:
    text = path.read_text(encoding="utf-8-sig")
    if not text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    match = re.match(r"^---\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not match:
        raise ValueError("malformed YAML frontmatter")
    try:
        frontmatter = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError(f"malformed YAML: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ValueError("frontmatter must be a mapping")
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    validate_skill_name(name)
    if not isinstance(description, str) or not description:
        raise ValueError("missing skill description")
    if len(description) > 1024:
        raise ValueError("description exceeds 1024 characters")
    return frontmatter, match.group(2)


def _inline_markdown_targets(body: str) -> tuple[str, ...]:
    targets: list[str] = []
    for opener in re.finditer(r"\]\(", body):
        start = opener.end()
        depth = 1
        escaped = False
        for index in range(start, len(body)):
            character = body[index]
            if escaped:
                escaped = False
                continue
            if character == "\\":
                escaped = True
                continue
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    targets.append(body[start:index])
                    break
        else:
            raise ValueError("unsafe local Markdown link with ambiguous parenthesized target")
    return tuple(targets)


def _raw_link_target(raw_target: str) -> str:
    target = raw_target.strip()
    if target.startswith("<"):
        closing = target.find(">", 1)
        if closing < 0:
            raise ValueError("unsafe local Markdown link with malformed angle-bracket target")
        target = target[1:closing].strip()
    else:
        target = target.split(maxsplit=1)[0] if target else ""
    if not target:
        raise ValueError("unsafe local Markdown link with empty target")
    return target


def _decode_local_path(raw_path: str) -> str:
    decoded = raw_path
    for _ in range(8):
        next_value = unquote(html.unescape(decoded))
        if next_value == decoded:
            return decoded
        decoded = next_value
    raise ValueError("unsafe local Markdown link with excessively encoded target")


def _mask_html_character_references(target: str) -> str:
    return HTML_CHARACTER_REFERENCE.sub(lambda match: "x" * len(match.group()), target)


def _html_attribute_value(match: re.Match[str]) -> str:
    return next(
        group
        for group in (
            match.group("double"),
            match.group("single"),
            match.group("unquoted"),
        )
        if group is not None
    )


def _raw_html_tags(body: str) -> tuple[str, ...]:
    tags: list[str] = []
    position = 0
    while opener := RAW_HTML_TAG_START.search(body, position):
        quote = ""
        for index in range(opener.end(), len(body)):
            character = body[index]
            if quote:
                if character == quote:
                    quote = ""
            elif character in {'"', "'"}:
                quote = character
            elif character == ">":
                tags.append(body[opener.start() : index + 1])
                position = index + 1
                break
        else:
            position = opener.end()
    return tuple(tags)


def _validate_local_markdown_links(body: str) -> None:
    targets = [(target, True) for target in _inline_markdown_targets(body)]
    targets.extend(
        (match.group(1), False)
        for match in re.finditer(r"(?m)^\s*\[[^]]+\]:\s*(\S+)", body)
    )
    targets.extend(
        (match.group(1), False)
        for match in COMMONMARK_URI_AUTOLINK.finditer(body)
    )
    for html_tag in _raw_html_tags(body):
        targets.extend(
            (_html_attribute_value(match), False)
            for match in SINGLE_URL_HTML_ATTRIBUTE.finditer(html_tag)
        )
        ambiguous_attribute = AMBIGUOUS_URL_HTML_ATTRIBUTE.search(html_tag)
        if ambiguous_attribute:
            raise ValueError(
                "unsafe local Markdown link with ambiguous HTML URL attribute: "
                f"{ambiguous_attribute.group('name').lower()}"
            )

    for raw_target, parenthesized in targets:
        target = _raw_link_target(raw_target)
        structural_target = _mask_html_character_references(target)
        try:
            parsed = urlsplit(structural_target)
        except ValueError as exc:
            raise ValueError(f"unsafe local Markdown link with malformed target: {target}") from exc
        if target.startswith("#"):
            continue
        if parsed.scheme:
            if parsed.scheme.lower() in ALLOWED_EXTERNAL_SCHEMES:
                continue
            raise ValueError(f"unsafe local Markdown link outside skill root: {target}")
        if parsed.netloc:
            raise ValueError(f"unsafe local Markdown link outside skill root: {target}")
        if parenthesized and any(character in target for character in "()"):
            raise ValueError(
                f"unsafe local Markdown link with ambiguous parenthesized target: {target}"
            )
        raw_path = target[: len(parsed.path)]
        decoded_path = _decode_local_path(raw_path)
        if raw_path and not decoded_path.strip():
            raise ValueError("unsafe local Markdown link with empty target")
        try:
            decoded_structure = urlsplit(decoded_path)
        except ValueError as exc:
            raise ValueError(f"unsafe local Markdown link with malformed target: {target}") from exc
        if (
            decoded_structure.scheme
            or decoded_structure.netloc
            or decoded_structure.query
            or decoded_structure.fragment
        ):
            raise ValueError(
                f"unsafe local Markdown link with encoded URL delimiter: {target}"
            )
        normalized = decoded_path.replace("\\", "/")
        normalized = normalized.removeprefix("${CLAUDE_SKILL_DIR}/")
        candidate = PurePosixPath(normalized)
        if (
            normalized.startswith(("/", "//"))
            or re.match(r"^[A-Za-z]:", normalized)
            or ".." in candidate.parts
        ):
            raise ValueError(f"unsafe local Markdown link outside skill root: {target}")


def _hermes_reasons(body: str, frontmatter: Mapping[str, object] | None = None) -> list[str]:
    tool_config = ""
    allowed_config = ""
    if frontmatter:
        tool_config = yaml.safe_dump(
            {
                key: value
                for key, value in frontmatter.items()
                if key in INTERPRETED_FRONTMATTER and key not in {"name", "metadata"}
            }
        )
        allowed_config = yaml.safe_dump(
            {key: frontmatter[key] for key in ("allowed-tools", "disallowed-tools") if key in frontmatter}
        )
    semantic_text = body + "\n" + tool_config
    tools = sorted(
        tool
        for tool in HERMES_TOOLS
        if re.search(
            rf"`{re.escape(tool)}`|\b{re.escape(tool)}\s*\(|\b(?:use|call|invoke|via|with)\s+(?:the\s+)?`?{re.escape(tool)}`?(?:\s+tool)?\b",
            semantic_text,
            re.IGNORECASE,
        )
        or re.search(rf"\b{re.escape(tool)}\b", allowed_config)
    )
    executable_fields = sorted(set(frontmatter or {}) & EXECUTION_BEARING_FRONTMATTER)
    reasons = (
        [f"execution-bearing frontmatter: {', '.join(executable_fields)}"]
        if executable_fields
        else []
    )
    if tools:
        reasons.append(f"Hermes tools: {', '.join(tools)}")
    reasons.extend(label for label, pattern in HERMES_ONLY_PATTERNS.items() if pattern.search(semantic_text))
    return reasons


def _apply_override(
    name: str,
    body: str,
    frontmatter: Mapping[str, object],
    override: Mapping[str, Any],
) -> tuple[str, Classification, tuple[str, ...]]:
    requested = override.get("classification")
    if requested == Classification.EXCLUDE.value:
        return body, Classification.EXCLUDE, (str(override.get("reason", "explicit exclusion override")),)
    if requested != Classification.ADAPT.value:
        raise OverrideError(f"{name}: unsupported override classification: {requested}")
    adapted = body
    replacements = override.get("replacements", [])
    if not isinstance(replacements, list):
        raise OverrideError(f"{name}: replacements must be a list")
    for replacement in replacements:
        if not isinstance(replacement, Mapping):
            raise OverrideError(f"{name}: each replacement must be a mapping")
        old = replacement.get("from")
        new = replacement.get("to")
        if not isinstance(old, str) or not isinstance(new, str):
            raise OverrideError(f"{name}: replacements require string from/to")
        if not old:
            raise OverrideError(f"{name}: replacement 'from' must not be empty")
        if old not in adapted:
            raise OverrideError(f"{name}: replacement source not found: {old!r}")
        adapted = adapted.replace(old, new)
    remaining = _hermes_reasons(adapted, frontmatter)
    if remaining:
        raise OverrideError(f"{name}: semantic override leaves incompatible constructs: {'; '.join(remaining)}")
    try:
        _validate_local_markdown_links(adapted)
    except ValueError as exc:
        raise OverrideError(f"{name}: {exc}") from exc
    return adapted, Classification.ADAPT, ("explicit semantic override",)


def scan_library(source: Path, overrides: Mapping[str, Mapping[str, Any]] | None = None) -> ScanResult:
    unresolved_source = Path(source).absolute()
    for candidate in (unresolved_source, *unresolved_source.parents):
        if candidate.is_symlink():
            relation = "root" if candidate == unresolved_source else "root symlink ancestor"
            return ScanResult(
                source=unresolved_source,
                skills=(),
                issues=(f"source {relation} is not allowed: {candidate}",),
            )
        if _is_windows_reparse_point(candidate):
            relation = "root reparse point" if candidate == unresolved_source else "root reparse ancestor"
            return ScanResult(
                source=unresolved_source,
                skills=(),
                issues=(f"source {relation} is not allowed: {candidate}",),
            )
    try:
        source_stat = unresolved_source.stat()
    except FileNotFoundError:
        return ScanResult(
            source=unresolved_source,
            skills=(),
            issues=(f"source root does not exist: {unresolved_source}",),
        )
    except OSError as exc:
        return ScanResult(
            source=unresolved_source,
            skills=(),
            issues=(f"source root is unreadable: {unresolved_source}: {exc}",),
        )
    if not stat.S_ISDIR(source_stat.st_mode):
        return ScanResult(
            source=unresolved_source,
            skills=(),
            issues=(f"source root is not a directory: {unresolved_source}",),
        )
    try:
        source = unresolved_source.resolve(strict=True)
    except OSError as exc:
        return ScanResult(
            source=unresolved_source,
            skills=(),
            issues=(f"source root cannot be resolved: {unresolved_source}: {exc}",),
        )
    overrides = overrides or {}
    for override_name in overrides:
        try:
            validate_skill_name(override_name)
        except ValueError as exc:
            raise OverrideError(f"invalid override skill name: {override_name!r}") from exc
    used_overrides: set[str] = set()
    skills: list[Skill] = []
    issues: list[str] = []
    try:
        source_files = iter_skill_files(source)
        skill_files = [
            path for path in source_files
            if path.name == "SKILL.md"
            and not any(part.startswith(".") for part in path.relative_to(source).parts)
        ]
        root_skill = source / "SKILL.md"
        if root_skill in skill_files:
            skill_files.remove(root_skill)
            issues.append("SKILL.md: root-level SKILL.md is not allowed in a categorized library")
        skill_dirs = {path.parent for path in skill_files}
        hidden_paths = hidden_entries(source)

        for directory in sorted((p for p in source.rglob("*") if p.is_dir()), key=str):
            relative = directory.relative_to(source)
            if any(part.startswith(".") for part in relative.parts):
                continue
            has_files = any(child.is_file() for child in directory.iterdir())
            under_skill = any(parent in skill_dirs for parent in directory.parents)
            contains_skill = any(directory in skill_dir.parents for skill_dir in skill_dirs)
            if has_files and directory not in skill_dirs and not under_skill and not contains_skill:
                issues.append(f"{relative.as_posix()}: missing SKILL.md")
    except (OSError, ValueError) as exc:
        return ScanResult(
            source=source,
            skills=(),
            issues=(f"source traversal failed: {exc}",),
        )

    for skill_file in skill_files:
        relative_dir = skill_file.parent.relative_to(source)
        try:
            frontmatter, body = _parse_skill(skill_file)
            _validate_local_markdown_links(body)
            name = str(frontmatter["name"])
            if name in overrides:
                used_overrides.add(name)
                override = overrides[name]
                if not isinstance(override, Mapping):
                    raise OverrideError(f"{name}: override must be a mapping")
                body, classification, reasons = _apply_override(name, body, frontmatter, override)
            else:
                incompatible = _hermes_reasons(body, frontmatter)
                unsupported = sorted(set(frontmatter) - INTERPRETED_FRONTMATTER)
                claude_specific = sorted(
                    (set(frontmatter) & CLAUDE_FRONTMATTER) - DIRECT_FRONTMATTER
                )
                unsupported_metadata = "metadata" in frontmatter and not _portable_metadata(frontmatter["metadata"])
                invalid_optional = _invalid_optional_frontmatter(frontmatter)
                has_windows_path = re.search(r"\]\([^)]*\\[^)]*\)", body) or "\\" in str(frontmatter.get("paths", ""))
                hidden_source = any(skill_file.parent in path.parents for path in hidden_paths)
                if incompatible:
                    classification = Classification.EXCLUDE
                    reasons = tuple(incompatible)
                elif (
                    unsupported
                    or claude_specific
                    or unsupported_metadata
                    or invalid_optional
                    or has_windows_path
                    or hidden_source
                ):
                    classification = Classification.CONVERT
                    reasons_list = []
                    if unsupported:
                        reasons_list.append(f"unsupported frontmatter: {', '.join(unsupported)}")
                    if claude_specific:
                        reasons_list.append(
                            f"Claude-specific frontmatter: {', '.join(claude_specific)}"
                        )
                    if unsupported_metadata:
                        reasons_list.append("unsupported metadata shape")
                    if invalid_optional:
                        reasons_list.append(
                            f"invalid optional frontmatter: {', '.join(invalid_optional)}"
                        )
                    if has_windows_path:
                        reasons_list.append("Windows path separators")
                    if hidden_source:
                        reasons_list.append("hidden source artifacts")
                    reasons = tuple(reasons_list)
                else:
                    classification = Classification.DIRECT
                    reasons = ()
            skills.append(
                Skill(
                    source_dir=skill_file.parent,
                    relative_dir=relative_dir,
                    name=name,
                    description=str(frontmatter["description"]),
                    frontmatter=frontmatter,
                    body=body,
                    classification=classification,
                    reasons=reasons,
                )
            )
        except OverrideError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            issues.append(f"{relative_dir.as_posix()}/SKILL.md: {exc}")

    unused_overrides = sorted(set(overrides) - used_overrides)
    if unused_overrides:
        raise OverrideError(f"unused override skill names: {', '.join(unused_overrides)}")

    by_name: dict[str, list[int]] = {}
    for index, skill in enumerate(skills):
        by_name.setdefault(skill.name, []).append(index)
    for name, indexes in by_name.items():
        if len(indexes) > 1:
            locations = ", ".join(skills[i].relative_dir.as_posix() for i in indexes)
            issues.append(f"name collision: {name}: {locations}")
            for index in indexes:
                old = skills[index]
                skills[index] = Skill(**{**old.__dict__, "blocked": True})

    return ScanResult(source=source, skills=tuple(skills), issues=tuple(issues))


def render_skill(skill: Skill) -> str:
    frontmatter = {key: value for key, value in skill.frontmatter.items() if key in CLAUDE_FRONTMATTER}
    if "metadata" in frontmatter and not _portable_metadata(frontmatter["metadata"]):
        del frontmatter["metadata"]
    for key in _invalid_optional_frontmatter(frontmatter):
        del frontmatter[key]
    if "paths" in frontmatter:
        def normalize_paths(value: object) -> object:
            if isinstance(value, str):
                return value.replace("\\", "/")
            if isinstance(value, list):
                return [normalize_paths(item) for item in value]
            if isinstance(value, dict):
                return {key: normalize_paths(item) for key, item in value.items()}
            return value
        frontmatter["paths"] = normalize_paths(frontmatter["paths"])
    frontmatter["name"] = skill.name
    frontmatter["description"] = skill.description
    raw_frontmatter = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).rstrip()
    body = re.sub(
        r"(\]\()([^)]+)(\))",
        lambda match: match.group(1) + match.group(2).replace("\\", "/") + match.group(3),
        skill.body,
    )
    marker = (
        f"<!-- {LEGACY_PROVENANCE_ID}\n"
        f"source: {skill.relative_dir.as_posix()}\n"
        f"source-sha256: {skill.source_hash}\n"
        "-->"
    )
    return f"---\n{raw_frontmatter}\n---\n\n{marker}\n\n{body}"
