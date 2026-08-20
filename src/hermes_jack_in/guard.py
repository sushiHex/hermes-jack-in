#!/usr/bin/env python
"""Claude PreToolUse guard for the authoritative Hermes skill tree."""

from __future__ import annotations

import argparse
import codecs
import glob
import json
import os
import re
import shlex
import stat
import string
import sys
from pathlib import Path
from typing import Any, Mapping

ANSI_C_QUOTE = re.compile(r"\$'((?:\\.|[^'\\])*)'")
# Locale translation happens before execution; reject its marker lexically rather
# than attempting to model whether quoting, escaping, or comments make it inert.
LOCALE_TRANSLATION_QUOTE = '$"'
EXTGLOB = re.compile(r"([@+?*!])\(([^()]*)\)")
MAX_LITERAL_VARIANTS = 64
MAX_COMMAND_LENGTH = 16_384
MAX_PATH_COMPONENTS = 256
MAX_GLOB_REGEX_LENGTH = 32_768
MAX_EXTGLOB_TEXT_LENGTH = 32
# Keep exemptions lexical: shell evaluation characters are absent by construction.
# This is intentionally not a shell parser.
LITERAL_READ_ONLY_COMMAND = re.compile(r"[A-Za-z0-9 \t_./:\\'\",=+%@-]+\Z")
ASCII_CHARS = frozenset(chr(value) for value in range(128))
POSIX_CLASS_CHARS = {
    "alnum": frozenset(string.ascii_letters + string.digits),
    "alpha": frozenset(string.ascii_letters),
    "blank": frozenset(" \t"),
    "cntrl": frozenset(chr(value) for value in (*range(32), 127)),
    "digit": frozenset(string.digits),
    "graph": frozenset(chr(value) for value in range(33, 127)),
    "lower": frozenset(string.ascii_lowercase),
    "print": frozenset(chr(value) for value in range(32, 127)),
    "punct": frozenset(string.punctuation),
    "space": frozenset(" \t\r\n\v\f"),
    "upper": frozenset(string.ascii_uppercase),
    "word": frozenset(string.ascii_letters + string.digits + "_"),
    "xdigit": frozenset(string.hexdigits),
}


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _host_path(raw: str) -> Path:
    normalized = raw.replace("\\", "/")
    if re.match(r"^/[A-Za-z]/", normalized):
        return Path(f"{normalized[1]}:/{normalized[3:]}")
    if normalized.startswith("~/"):
        return Path.home() / normalized[2:]
    named_home = re.match(r"^~([^/]+)/(.+)$", normalized)
    if named_home and named_home.group(1).lower() == Path.home().name.lower():
        return Path.home() / named_home.group(2)
    return Path(normalized)


def _windows_long_path(path: Path) -> Path:
    """Expand an existing Windows 8.3 alias without changing other platforms."""
    if os.name != "nt":
        return path
    try:
        import ctypes
        from ctypes import wintypes

        get_long_path_name = ctypes.WinDLL("kernel32", use_last_error=True).GetLongPathNameW
        get_long_path_name.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        )
        get_long_path_name.restype = wintypes.DWORD
        required = get_long_path_name(os.fspath(path), None, 0)
        if not required:
            return path
        buffer = ctypes.create_unicode_buffer(required)
        written = get_long_path_name(os.fspath(path), buffer, required)
        if not written or written >= required:
            return path
        return Path(buffer.value)
    except (AttributeError, OSError, ValueError):
        return path


def _path_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    return _windows_long_path(resolved).as_posix().lower().rstrip("/")


def _validated_guard_roots(roots: tuple[str | Path, ...]) -> tuple[Path, ...]:
    """Validate explicit physical directories before accepting hook traffic."""
    if not roots:
        raise ValueError("at least one protected root is required")

    validated: list[Path] = []
    identities: set[str] = set()
    for raw_root in roots:
        root = _host_path(os.fspath(raw_root)).expanduser()
        if not root.is_absolute():
            raise ValueError("protected roots must be absolute")
        unresolved = root.absolute()
        for candidate in (unresolved, *unresolved.parents):
            try:
                metadata = candidate.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(metadata.st_mode) or bool(
                int(getattr(metadata, "st_file_attributes", 0))
                & int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
            ):
                raise ValueError("protected roots and ancestors must be physical")
        try:
            metadata = unresolved.lstat()
        except OSError as exc:
            raise ValueError("protected root is unavailable") from exc
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("protected root must be a directory")
        resolved = _windows_long_path(unresolved.resolve(strict=True))
        identity = _path_key(resolved)
        if identity in identities:
            raise ValueError("protected roots must be unique")
        identities.add(identity)
        validated.append(resolved)
    return tuple(validated)


def _expand_braces(value: str) -> tuple[str, ...]:
    variants = [value]
    while True:
        expanded = False
        next_variants: list[str] = []
        for candidate in variants:
            match = re.search(r"\{([^{}]+)\}", candidate)
            if not match:
                next_variants.append(candidate)
                continue
            expanded = True
            expression = match.group(1)
            range_parts = expression.split("..")
            if len(range_parts) == 2 and all(len(part) == 1 for part in range_parts):
                start, stop = (ord(part) for part in range_parts)
                step = 1 if start <= stop else -1
                choices = tuple(chr(item) for item in range(start, stop + step, step))
            else:
                choices = tuple(expression.split(","))
            for choice in choices:
                next_variants.append(
                    candidate[: match.start()] + choice + candidate[match.end() :]
                )
                if len(next_variants) > MAX_LITERAL_VARIANTS:
                    raise ValueError("literal expansion exceeds analysis bound")
        variants = next_variants
        if not expanded:
            return tuple(variants)


def _literal_command_variants(command: str) -> tuple[str, ...]:
    """Expand bounded Bash literal syntax needed for protected-path checks."""
    command = re.sub(r"\\\r?\n", "", command)

    def decode_ansi(match: re.Match[str]) -> str:
        try:
            return codecs.decode(match.group(1), "unicode_escape")
        except (UnicodeDecodeError, ValueError):
            return match.group(0)

    command = ANSI_C_QUOTE.sub(decode_ansi, command)
    variants = list(_expand_braces(command))
    while True:
        expanded = False
        next_variants: list[str] = []
        for value in variants:
            match = EXTGLOB.search(value)
            if not match:
                next_variants.append(value)
                continue
            expanded = True
            operator = match.group(1)
            alternatives = tuple(match.group(2).split("|"))
            if operator == "!":
                choices = ("skills",)
            elif operator in "+*":
                if any(not alternative for alternative in alternatives):
                    raise ValueError("empty repeated extglob alternative")
                repeated = {""}
                frontier = {""}
                while frontier:
                    next_frontier: set[str] = set()
                    for prefix in frontier:
                        for alternative in alternatives:
                            candidate = prefix + alternative
                            if len(candidate) <= MAX_EXTGLOB_TEXT_LENGTH and candidate not in repeated:
                                repeated.add(candidate)
                                next_frontier.add(candidate)
                                if len(repeated) > MAX_LITERAL_VARIANTS:
                                    raise ValueError("literal expansion exceeds analysis bound")
                    frontier = next_frontier
                choices = tuple(sorted(repeated if operator == "*" else repeated - {""}))
            elif operator == "?":
                choices = ("", *alternatives)
            else:
                choices = alternatives
            for choice in choices:
                next_variants.append(value[: match.start()] + choice + value[match.end() :])
                if len(next_variants) > MAX_LITERAL_VARIANTS:
                    raise ValueError("literal expansion exceeds analysis bound")
        variants = next_variants
        if not expanded:
            break
    return tuple(variants)


def _parse_bracket_expression(
    pattern: str, start: int
) -> tuple[frozenset[str], int] | None:
    index = start + 1
    negated = index < len(pattern) and pattern[index] in "!^"
    if negated:
        index += 1
    characters: set[str] = set()
    if index < len(pattern) and pattern[index] == "]":
        characters.add("]")
        index += 1
    while index < len(pattern):
        if pattern[index] == "]":
            matched = ASCII_CHARS.difference(characters) if negated else characters
            return frozenset(matched), index + 1
        if pattern.startswith("[:", index):
            end = pattern.find(":]", index + 2)
            if end < 0:
                raise ValueError("malformed POSIX character class")
            name = pattern[index + 2 : end].lower()
            if name not in POSIX_CLASS_CHARS:
                raise ValueError(f"unsupported POSIX character class: {name}")
            characters.update(POSIX_CLASS_CHARS[name])
            index = end + 2
            continue
        matched_special = False
        for opener, closer in (("[=", "=]"), ("[.", ".]")):
            if pattern.startswith(opener, index):
                end = pattern.find(closer, index + 2)
                if end < 0 or end != index + 3:
                    raise ValueError("unsupported multi-character bracket element")
                characters.add(pattern[index + 2])
                index = end + 2
                matched_special = True
                break
        if matched_special:
            continue
        if index + 2 < len(pattern) and pattern[index + 1] == "-" and pattern[index + 2] != "]":
            start_value, end_value = ord(pattern[index]), ord(pattern[index + 2])
            if start_value > end_value:
                raise ValueError("descending glob range")
            if end_value >= 128:
                raise ValueError("non-ASCII glob range exceeds analysis scope")
            characters.update(chr(value) for value in range(start_value, end_value + 1))
            index += 3
        else:
            characters.add(pattern[index])
            index += 1
    return None


def _bash_component_matches(value: str, pattern: str) -> bool:
    pattern = re.sub(r"\*+", "*", pattern)
    regex: list[str] = ["^"]
    regex_length = 1
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "*":
            fragment = ".*"
            index += 1
        elif character == "?":
            fragment = "."
            index += 1
        elif character == "[":
            parsed = _parse_bracket_expression(pattern, index)
            if parsed is None:
                fragment = r"\["
                index += 1
            else:
                characters, index = parsed
                if not characters:
                    return False
                fragment = "[" + "".join(
                    re.escape(item) for item in sorted(characters)
                ) + "]"
        else:
            fragment = re.escape(character)
            index += 1
        regex.append(fragment)
        regex_length += len(fragment)
        if regex_length > MAX_GLOB_REGEX_LENGTH:
            raise ValueError("glob expression exceeds analysis bound")
    regex.append("$")
    return re.fullmatch("".join(regex), value) is not None


def _bash_path_matches(value: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if len(value) > MAX_PATH_COMPONENTS or len(pattern) > MAX_PATH_COMPONENTS:
        raise ValueError("path expression exceeds analysis bound")
    pending = [(0, 0)]
    visited: set[tuple[int, int]] = set()
    while pending:
        value_index, pattern_index = pending.pop()
        state = (value_index, pattern_index)
        if state in visited:
            continue
        visited.add(state)
        if pattern_index == len(pattern):
            if value_index == len(value):
                return True
            continue
        if pattern[pattern_index] == "**":
            pending.append((value_index, pattern_index + 1))
            if value_index < len(value):
                pending.append((value_index + 1, pattern_index))
        elif value_index < len(value) and _bash_component_matches(
            value[value_index], pattern[pattern_index]
        ):
            pending.append((value_index + 1, pattern_index + 1))
    return False


def _candidate_paths(
    raw: str,
    base: Path | None,
    protected_roots: tuple[str, ...],
) -> tuple[Path, ...]:
    if raw.startswith("-"):
        if "=" in raw:
            raw = raw.split("=", 1)[1]
        else:
            attached = re.search(r"(?:[A-Za-z]:[\\/]|/[A-Za-z]/|~[^/]*[\\/]|\.{1,2}[\\/])", raw)
            if not attached:
                return ()
            raw = raw[attached.start() :]
    raw = raw.rstrip(",:)")
    candidates: list[Path] = []
    for expanded in _expand_braces(raw):
        candidate = _host_path(expanded)
        if not candidate.is_absolute():
            if base is None:
                continue
            candidate = base / candidate
        glob_pattern = str(candidate).replace("[^", "[!")
        matches: tuple[Path, ...] = ()
        if glob.has_magic(glob_pattern):
            raw_pattern_parts = tuple(
                part for part in glob_pattern.replace("\\", "/").split("/") if part
            )
            if "**" in raw_pattern_parts and ".." in raw_pattern_parts:
                raise ValueError("globstar traversal exceeds analysis scope")
            normalized_pattern = os.path.normpath(glob_pattern).replace("\\", "/").lower()
            pattern_ancestors = [normalized_pattern]
            while "/" in pattern_ancestors[-1]:
                parent_pattern = pattern_ancestors[-1].rsplit("/", 1)[0]
                if not parent_pattern or parent_pattern == pattern_ancestors[-1]:
                    break
                pattern_ancestors.append(parent_pattern)
            protected_paths: list[Path] = []
            for root in protected_roots:
                protected = _host_path(root)
                for possible in (protected, *protected.parents):
                    possible_text = possible.as_posix().lower()
                    possible_parts = tuple(part for part in possible_text.strip("/").split("/") if part)
                    full_pattern_parts = tuple(
                        part for part in normalized_pattern.strip("/").split("/") if part
                    )
                    if _bash_path_matches(possible_parts, full_pattern_parts):
                        protected_paths.append(possible)
                protected_parts = tuple(
                    part for part in protected.as_posix().lower().strip("/").split("/") if part
                )
                if any(
                    _bash_path_matches(protected_parts, pattern_parts)
                    for pattern_parts in (
                        tuple(part for part in pattern.strip("/").split("/") if part)
                        for pattern in pattern_ancestors
                    )
                ):
                    protected_paths.append(protected)
            matches = tuple(protected_paths)
        candidates.extend(matches or (candidate,))
    return tuple(candidates)


def _resolved_command_paths(
    command: str,
    cwd: object,
    protected_roots: tuple[str, ...],
    depth: int = 0,
) -> tuple[str, ...]:
    base = _host_path(cwd) if isinstance(cwd, str) else None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = tuple(lexer)
    except ValueError:
        tokens = tuple(command.split())
    resolved: list[str] = []
    for raw in tokens:
        if not raw or all(char in ";&|()<>" for char in raw):
            continue
        if depth < 4 and any(character.isspace() for character in raw):
            resolved.extend(_resolved_command_paths(raw, cwd, protected_roots, depth + 1))
            continue
        for candidate in _candidate_paths(raw, base, protected_roots):
            resolved.append(_path_key(candidate))
    return tuple(resolved)


def _read_only_tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _is_read_only_invocation(tokens: list[str]) -> bool:
    if not tokens:
        return False
    if tokens[0] == "ls" or tokens[0] == "pwd":
        return True
    if tokens[:2] == ["git", "status"]:
        return True

    index = 0
    if tokens[:3] == ["env", "-u", "PYTHONPATH"]:
        index = 3
    if tokens[index : index + 2] == ["uv", "run"]:
        index += 2
    return (
        tokens[index : index + 1] == ["hermes-jack-in"]
        and tokens[index + 1 : index + 2] in (["scan"], ["plan"], ["check"])
    )


def _cwd_paths(cwd: object) -> tuple[str, ...]:
    if not isinstance(cwd, str):
        return ()
    path = _host_path(cwd)
    raw = path.absolute().as_posix().lower()
    resolved = _path_key(path)
    return tuple(dict.fromkeys((raw, resolved)))


def _protected_root_data(
    roots: tuple[str | Path, ...],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    canonical: list[str] = []
    spellings: set[str] = set()
    fragments: set[str] = set()
    for raw_root in roots:
        path = _host_path(str(raw_root)).expanduser()
        if not path.is_absolute():
            raise ValueError(f"protected root must be absolute: {raw_root}")
        key = _path_key(path)
        canonical.append(key)
        spellings.add(key)
        drive = re.match(r"^([a-z]):/(.+)$", key)
        if drive:
            spellings.add(f"/{drive.group(1)}/{drive.group(2)}")
        named_home = re.match(r"^[a-z]:/users/([^/]+)/(.+)$", key)
        if named_home:
            spellings.add(f"~{named_home.group(1)}/{named_home.group(2)}")
        parts = tuple(part for part in key.strip("/").split("/") if part)
        for index in range(max(0, len(parts) - 6), len(parts) - 1):
            fragments.add("/".join(parts[index:]))
    return tuple(canonical), tuple(sorted(spellings)), tuple(sorted(fragments))


def _command_may_mutate(command: str) -> bool:
    mutation_commands = (
        "bash",
        "cp",
        "find",
        "git",
        "install",
        "mv",
        "perl",
        "python",
        "rm",
        "sed",
        "sh",
    )
    return any(
        re.search(rf"(?:^|[;&|()\s]){name}(?:$|[;&|()\s])", command)
        for name in mutation_commands
    )


def evaluate(
    event: Mapping[str, Any],
    *,
    protected_roots: tuple[str | Path, ...] | None = None,
) -> dict[str, Any] | None:
    """Return a deny decision for unsafe Bash input, otherwise no opinion."""
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, Mapping):
        return _deny("Malformed Bash tool input; refusing execution.")
    command = tool_input.get("command")
    if not isinstance(command, str):
        return _deny("Malformed Bash command; refusing execution.")
    if len(command) > MAX_COMMAND_LENGTH:
        return _deny("Bash command exceeds the guard's analysis bound.")
    if LOCALE_TRANSLATION_QUOTE in command:
        return _deny(
            "Bash locale-translation quotes are outside the guard's literal analysis; "
            "refusing execution."
        )
    read_only_tokens = _read_only_tokens(command)
    if _is_read_only_invocation(read_only_tokens):
        if LITERAL_READ_ONLY_COMMAND.fullmatch(command) is None:
            return _deny(
                "Read-only Bash exemptions require a literal command without shell evaluation."
            )
        return None

    if re.search(r"\[[^]\r\n]*[^\x00-\x7f]", command):
        return _deny("Non-ASCII Bash bracket expressions are outside the analysis scope.")

    configured_roots = protected_roots or ()
    try:
        canonical_roots, root_spellings, lexical_fragments = _protected_root_data(
            tuple(configured_roots)
        )
        command_variants = _literal_command_variants(command)
        normalized_variants = tuple(
            re.sub(r"/+", "/", variant.replace("\\", "/"))
            .replace('"', "")
            .replace("'", "")
            .lower()
            for variant in command_variants
        )
        resolved_paths = tuple(
            path
            for variant in command_variants
            for path in _resolved_command_paths(
                variant,
                event.get("cwd"),
                canonical_roots,
            )
        )
    except ValueError:
        return _deny("Bash literal expansion or protected-root configuration is invalid.")
    cwd_paths = _cwd_paths(event.get("cwd"))
    if (
        any(root in normalized for normalized in normalized_variants for root in root_spellings)
        or (
            not isinstance(event.get("cwd"), str)
            and _command_may_mutate(command)
            and any(
                fragment in normalized
                for normalized in normalized_variants
                for fragment in lexical_fragments
            )
        )
        or any(
            path == root or path.startswith(f"{root}/")
            for path in cwd_paths
            for root in canonical_roots
        )
        or any(
            path == root or path.startswith(f"{root}/")
            for path in resolved_paths
            for root in canonical_roots
        )
        or any(
            root == path or root.startswith(f"{path}/")
            for path in resolved_paths
            for root in canonical_roots
        )
    ):
        return _deny(
            "The configured skill trees are protected from Claude Code Bash mutation. "
            "Make catalog changes through the authoritative workflow instead."
        )
    return None


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fail-closed Claude PreToolUse guard for explicit skill trees."
    )
    parser.add_argument(
        "--protected-root",
        action="append",
        type=Path,
        help="absolute physical skill root to protect; repeatable and required",
    )
    return parser.parse_args(argv)


def _load_event_from_stdin() -> Any:
    binary_stream = getattr(sys.stdin, "buffer", None)
    if binary_stream is None:
        return json.load(sys.stdin)
    payload = binary_stream.read()
    return json.loads(payload.decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    try:
        roots = _validated_guard_roots(tuple(args.protected_root or ()))
    except Exception:  # noqa: BLE001 - invalid hook configuration must deny Bash
        roots = ()
        configuration_valid = False
    else:
        configuration_valid = True

    try:
        event = _load_event_from_stdin()
    except Exception:  # noqa: BLE001 - malformed hook input must deny Bash
        decision = _deny("Malformed PreToolUse input; refusing Bash execution.")
    else:
        if not isinstance(event, Mapping):
            decision = _deny("Malformed hook event.")
        elif event.get("tool_name") == "Bash" and not configuration_valid:
            decision = _deny(
                "Guard configuration is invalid; refusing Bash execution. "
                "Configure unique, absolute, existing physical protected roots."
            )
        else:
            try:
                decision = evaluate(event, protected_roots=roots)
            except Exception:  # noqa: BLE001 - hook boundary must fail closed
                decision = _deny(
                    "Guard evaluation failed; refusing Bash execution."
                )
    if decision is not None:
        json.dump(decision, sys.stdout, separators=(",", ":"))
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
