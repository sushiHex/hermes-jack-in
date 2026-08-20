from __future__ import annotations

import errno
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from .core import (
    Classification,
    ScanResult,
    Skill,
    iter_skill_files,
    render_skill,
    scan_library,
    validate_skill_name,
)

LEGACY_PROTOCOL_ID = "hermes-claude-skills-adapter"
# The manifest and lock namespace are an ownership/concurrency protocol, not
# product branding. Old and new binaries must continue to share both names.
MANIFEST_NAME = f".{LEGACY_PROTOCOL_ID}.json"
MANIFEST_VERSION = 3
DESIRED_OUTPUT_IDENTITY_VERSION = 1
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
DESIRED_OUTPUT_IDENTITY_RE = re.compile(r"^v[1-9][0-9]*:[0-9a-f]{64}$")
LINUX_RENAMEAT2_SYSCALLS = {
    "aarch64": 276,
    "i686": 353,
    "ppc64le": 357,
    "s390x": 347,
    "x86_64": 316,
}


class AdapterError(RuntimeError):
    pass


class OwnershipError(AdapterError):
    pass


class _RenameOutcome(Enum):
    MOVED = "moved"
    NOT_MOVED = "not-moved"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Action:
    operation: str
    name: str
    mode: str
    source: str
    destination: str


@dataclass(frozen=True)
class SyncResult:
    actions: tuple[Action, ...]


@dataclass(frozen=True)
class CheckIssue:
    kind: str
    name: str
    detail: str


@dataclass(frozen=True)
class CheckResult:
    issues: tuple[CheckIssue, ...]


@dataclass(frozen=True)
class _LoadedManifest:
    data: dict[str, Any]
    version: int
    modern_hashes: bool
    identity: _FilesystemIdentity | None
    content: bytes | None

    @property
    def needs_upgrade(self) -> bool:
        return self.version != MANIFEST_VERSION


@dataclass(frozen=True)
class _FilesystemIdentity:
    device: int
    inode: int
    file_type: int
    reparse_attributes: int
    reparse_tag: int


@dataclass
class _ManifestState:
    identity: _FilesystemIdentity | None
    content: bytes | None


@dataclass(frozen=True)
class _StagedArtifact:
    path: Path
    identity: _FilesystemIdentity
    mode: str
    entry: dict[str, Any]


@dataclass(frozen=True)
class _QuarantinedArtifact:
    root: Path
    root_identity: _FilesystemIdentity
    path: Path
    identity: _FilesystemIdentity
    original_path: Path


def _tree_hash(path: Path) -> str:
    digest = hashlib.sha256()
    _update_hash_record(digest, b"V", b"tree-hash-v2")

    def add_entry(entry: Path, relative: str) -> None:
        encoded = relative.encode("utf-8")
        if entry.is_symlink():
            if relative != ".":
                raise OwnershipError(f"nested symlink is not allowed in owned artifact: {entry}")
            _update_hash_record(
                digest,
                b"L",
                encoded,
                os.readlink(entry).encode("utf-8"),
            )
            return
        if _is_windows_reparse_point(entry):
            if relative != ".":
                raise OwnershipError(
                    f"nested reparse point is not allowed in owned artifact: {entry}"
                )
            try:
                raw_target = os.readlink(entry).encode("utf-8")
            except (OSError, RuntimeError, ValueError) as exc:
                raise OwnershipError(f"unreadable owned reparse point: {entry}") from exc
            _update_hash_record(digest, b"R", encoded, raw_target)
            return
        if entry.is_dir():
            _update_hash_record(digest, b"D", encoded)
            for child in sorted(entry.iterdir(), key=lambda item: item.name):
                child_relative = child.relative_to(path).as_posix()
                add_entry(child, child_relative)
            return
        if entry.is_file():
            _update_hash_record(digest, b"F", encoded, entry.read_bytes())
            return
        _update_hash_record(digest, b"O", encoded)

    add_entry(path, ".")
    return digest.hexdigest()


def _legacy_tree_hash(path: Path) -> str:
    digest = hashlib.sha256()

    def add_entry(entry: Path, relative: str) -> None:
        encoded = relative.encode("utf-8")
        if entry.is_symlink():
            digest.update(b"L\0" + encoded + b"\0" + os.readlink(entry).encode("utf-8"))
            return
        if entry.is_dir():
            digest.update(b"D\0" + encoded + b"\0")
            for child in sorted(entry.iterdir(), key=lambda item: item.name):
                child_relative = child.relative_to(path).as_posix()
                add_entry(child, child_relative)
            return
        if entry.is_file():
            digest.update(b"F\0" + encoded + b"\0")
            digest.update(entry.read_bytes())
            return
        digest.update(b"O\0" + encoded + b"\0")

    add_entry(path, ".")
    return digest.hexdigest()


def _manifest_path(destination: Path) -> Path:
    return destination / MANIFEST_NAME


def _load_manifest(destination: Path) -> _LoadedManifest:
    path = _manifest_path(destination)
    try:
        identity = _identity_from_stat(path.lstat())
    except FileNotFoundError:
        return _LoadedManifest(
            {"version": MANIFEST_VERSION, "skills": {}},
            MANIFEST_VERSION,
            True,
            None,
            None,
        )
    try:
        content = path.read_bytes()
        data = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"invalid ownership manifest: {path}: {exc}") from exc
    _require_identity(
        path,
        identity,
        "ownership manifest changed while it was loaded",
    )
    if not isinstance(data, dict):
        raise OwnershipError(f"manifest root must be a mapping: {path}")
    version = data.get("version")
    if (
        type(version) is not int
        or version not in {1, 2, MANIFEST_VERSION}
        or not isinstance(data.get("skills"), dict)
    ):
        raise OwnershipError(f"unsupported ownership manifest: {path}")
    identity_presence = {
        "desired_output_identity" in entry
        for entry in data["skills"].values()
        if isinstance(entry, dict)
    }
    if version == 2 and len(identity_presence) > 1:
        raise OwnershipError(
            "mixed ownership manifest schema requires explicit operator reconciliation: "
            f"{path}"
        )
    modern_hashes = version == MANIFEST_VERSION or (
        version == 2 and identity_presence == {True}
    )
    for name, entry in data["skills"].items():
        try:
            validate_skill_name(name)
        except ValueError as exc:
            raise OwnershipError(f"invalid manifest skill name: {name!r}") from exc
        if not isinstance(entry, dict):
            raise OwnershipError(f"invalid manifest entry for {name}")
        required = {"source", "source_hash", "classification", "mode", "artifact_hash"}
        mode = entry.get("mode")
        live_modes = {"symlink"} if version == 1 else {"symlink", "junction"}
        if modern_hashes:
            required.add("desired_output_identity")
        optional_keys: set[str] = set()
        required_keys = required | ({"target"} if mode in live_modes else set())
        allowed_keys = required_keys | optional_keys
        entry_keys = set(entry)
        source_value = entry.get("source")
        source_path = PurePosixPath(source_value) if isinstance(source_value, str) else None
        desired_output_identity = entry.get("desired_output_identity")
        if (
            not required_keys <= entry_keys
            or not entry_keys <= allowed_keys
            or mode not in live_modes | {"copy-fallback", "materialized"}
            or source_path is None
            or "\\" in source_value
            or re.match(r"^[A-Za-z]:", source_value)
            or source_path.is_absolute()
            or not source_path.parts
            or any(part in {"", ".", ".."} for part in source_path.parts)
            or entry.get("classification") not in {classification.value for classification in Classification}
            or not isinstance(entry.get("source_hash"), str)
            or not HASH_RE.fullmatch(entry["source_hash"])
            or not isinstance(entry.get("artifact_hash"), str)
            or not HASH_RE.fullmatch(entry["artifact_hash"])
            or (
                modern_hashes
                and (
                    not isinstance(desired_output_identity, str)
                    or not DESIRED_OUTPUT_IDENTITY_RE.fullmatch(desired_output_identity)
                )
            )
        ):
            raise OwnershipError(f"invalid manifest entry for {name}")
        if mode in {"symlink", "junction"} and not isinstance(entry.get("target"), str):
            raise OwnershipError(f"invalid manifest entry for {name}: missing link target")
    if data["skills"] and not isinstance(data.get("source"), str):
        raise OwnershipError("ownership manifest with skills is missing a valid source")
    return _LoadedManifest(data, version, modern_hashes, identity, content)


def _manifest_state_from_loaded(loaded: _LoadedManifest) -> _ManifestState:
    return _ManifestState(loaded.identity, loaded.content)


def _manifest_quarantine_matches(
    quarantine: _QuarantinedArtifact,
    state: _ManifestState,
) -> bool:
    if state.identity is None or state.content is None:
        return False
    _require_quarantine_identity(quarantine)
    return (
        quarantine.identity == state.identity
        and quarantine.path.read_bytes() == state.content
    )


def _rollback_manifest_publication(
    path: Path,
    published_identity: _FilesystemIdentity,
    published_content: bytes,
    previous: _QuarantinedArtifact | None,
) -> None:
    replacement: _QuarantinedArtifact | None = None
    if _artifact_exists(path):
        replacement = _quarantine_artifact(path)
        try:
            replacement_matches = (
                replacement.identity == published_identity
                and replacement.path.read_bytes() == published_content
            )
        except BaseException:
            if _artifact_exists(replacement.path):
                _restore_quarantined_artifact(replacement)
            raise
        if not replacement_matches:
            _restore_unproven_quarantine(
                replacement,
                f"published ownership manifest changed during rollback: {path}",
            )
    if previous is not None:
        _restore_quarantined_artifact(previous)
    if replacement is not None:
        _remove_quarantined_artifact(replacement)


def _write_manifest(
    destination: Path,
    manifest: Mapping[str, Any],
    state: _ManifestState | None = None,
) -> None:
    destination.mkdir(exist_ok=True)
    path = _manifest_path(destination)
    if state is None:
        state = _manifest_state_from_loaded(_load_manifest(destination))
    if (state.identity is None) != (state.content is None):
        raise OwnershipError(f"invalid expected ownership manifest state: {path}")
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary: Path | None = None
    temporary_identity: _FilesystemIdentity | None = None
    descriptor_open = True
    previous: _QuarantinedArtifact | None = None
    published = False
    publication_ambiguous = False
    publication_verified = False
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination,
        prefix=f"{path.name}.",
        suffix=".tmp",
        text=True,
    )
    try:
        temporary = Path(temporary_name)
        temporary_identity = _identity_from_stat(os.fstat(descriptor))
        handle = os.fdopen(descriptor, "w", encoding="utf-8", newline="\n")
        descriptor_open = False
        with handle:
            handle.write(payload.decode("utf-8"))
        _require_identity(
            temporary,
            temporary_identity,
            "manifest scratch artifact was replaced before commit",
        )
        if temporary.read_bytes() != payload:
            raise OwnershipError(f"manifest scratch content changed before commit: {temporary}")

        if state.identity is not None:
            previous = _quarantine_artifact(path)
            if not _manifest_quarantine_matches(previous, state):
                raise OwnershipError(
                    f"ownership manifest changed before publication: {path}"
                )
        try:
            _rename_no_replace(temporary, path)
        except BaseException as exc:
            outcome = _reconcile_rename_after_exception(
                temporary,
                path,
                temporary_identity,
            )
            if outcome is _RenameOutcome.MOVED:
                published = True
            elif outcome is _RenameOutcome.AMBIGUOUS:
                publication_ambiguous = True
                raise OwnershipError(
                    f"manifest scratch artifact identity became ambiguous during publication: {temporary}"
                ) from exc
            if isinstance(exc, FileExistsError) and outcome is _RenameOutcome.NOT_MOVED:
                raise OwnershipError(
                    f"ownership manifest changed before publication: {path}"
                ) from exc
            raise
        published = True
        _require_identity(
            path,
            temporary_identity,
            "published ownership manifest identity changed",
        )
        if path.read_bytes() != payload:
            raise OwnershipError(
                f"published ownership manifest content changed: {path}"
            )
        publication_verified = True
        state.identity = temporary_identity
        state.content = payload
    except BaseException:
        if temporary_identity is None and descriptor_open:
            try:
                temporary_identity = _identity_from_stat(os.fstat(descriptor))
            except BaseException:
                pass
        if descriptor_open:
            os.close(descriptor)
            descriptor_open = False
        rollback_error: BaseException | None = None
        if not publication_ambiguous:
            try:
                if published and not publication_verified and temporary_identity is not None:
                    _rollback_manifest_publication(
                        path,
                        temporary_identity,
                        payload,
                        previous,
                    )
                elif previous is not None and _artifact_exists(previous.path):
                    _restore_quarantined_artifact(previous)
            except BaseException as exc:
                rollback_error = exc
            try:
                if temporary_identity is not None:
                    scratch_path = (
                        temporary if temporary is not None else Path(temporary_name)
                    )
                    _cleanup_owned_scratch(scratch_path, temporary_identity)
            except BaseException as cleanup_exc:
                raise OwnershipError(
                    f"refusing to clean replaced scratch artifact: {temporary_name}"
                ) from cleanup_exc
        if rollback_error is not None:
            raise OwnershipError(
                f"failed closed while rolling back ownership manifest publication: {path}"
            ) from rollback_error
        raise
    finally:
        if descriptor_open:
            os.close(descriptor)
    if temporary is None:
        raise OwnershipError(f"manifest scratch path is unavailable: {temporary_name}")
    if _artifact_exists(temporary):
        try:
            if temporary_identity is None:
                raise OwnershipError(
                    f"manifest scratch artifact identity is unavailable: {temporary}"
                )
            _cleanup_owned_scratch(temporary, temporary_identity)
        except BaseException as cleanup_exc:
            raise OwnershipError(
                f"refusing to clean replaced scratch artifact: {temporary}"
            ) from cleanup_exc
        raise OSError(f"manifest replace did not consume scratch artifact: {temporary}")
    if previous is not None:
        _remove_quarantined_artifact(previous)


def _selected(scan: ScanResult) -> dict[str, Skill]:
    if scan.issues:
        raise AdapterError("source scan failed:\n" + "\n".join(f"- {issue}" for issue in scan.issues))
    return {
        skill.name: skill
        for skill in scan.skills
        if skill.classification is not Classification.EXCLUDE and not skill.blocked
    }


def _update_hash_record(digest: Any, record_type: bytes, *fields: bytes) -> None:
    if len(record_type) != 1:
        raise ValueError("hash record types must be exactly one byte")
    digest.update(record_type)
    digest.update(struct.pack(">I", len(fields)))
    for field in fields:
        digest.update(struct.pack(">Q", len(field)))
        digest.update(field)


def _desired_output_identity(skill: Skill, mode: str) -> str:
    normalized_mode = "live-link" if mode in {"symlink", "junction"} else mode
    digest = hashlib.sha256()
    _update_hash_record(
        digest,
        b"V",
        str(DESIRED_OUTPUT_IDENTITY_VERSION).encode("ascii"),
    )
    _update_hash_record(digest, b"M", normalized_mode.encode("ascii"))
    _update_hash_record(digest, b"C", skill.classification.value.encode("utf-8"))
    _update_hash_record(digest, b"P", skill.relative_dir.as_posix().encode("utf-8"))
    if normalized_mode == "live-link":
        _update_hash_record(digest, b"S", skill.source_hash.encode("ascii"))
    else:
        skill_contents = (
            (skill.source_dir / "SKILL.md").read_bytes()
            if normalized_mode == "copy-fallback"
            else render_skill(skill).encode("utf-8")
        )
        _update_hash_record(digest, b"F", b"SKILL.md", skill_contents)
        for source_file in iter_skill_files(skill.source_dir):
            relative = source_file.relative_to(skill.source_dir)
            if source_file.name == "SKILL.md" or any(part.startswith(".") for part in relative.parts):
                continue
            _update_hash_record(
                digest,
                b"F",
                relative.as_posix().encode("utf-8"),
                source_file.read_bytes(),
            )
    return f"v{DESIRED_OUTPUT_IDENTITY_VERSION}:{digest.hexdigest()}"


def _is_windows_reparse_point(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _is_windows_reparse_directory(path: Path) -> bool:
    if not _is_windows_reparse_point(path):
        return False
    try:
        attributes = path.lstat().st_file_attributes
    except (AttributeError, FileNotFoundError):
        return False
    return bool(attributes & stat.FILE_ATTRIBUTE_DIRECTORY)


def _is_windows_junction(path: Path) -> bool:
    if not _is_windows_reparse_directory(path):
        return False
    try:
        reparse_tag = path.lstat().st_reparse_tag
    except (AttributeError, FileNotFoundError):
        return False
    return reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT


def _identity_from_stat(metadata: os.stat_result) -> _FilesystemIdentity:
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    reparse_attributes = attributes & (
        getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        | getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0)
    )
    return _FilesystemIdentity(
        device=int(metadata.st_dev),
        inode=int(metadata.st_ino),
        file_type=stat.S_IFMT(metadata.st_mode),
        reparse_attributes=reparse_attributes,
        reparse_tag=int(getattr(metadata, "st_reparse_tag", 0)),
    )


def _artifact_identity(path: Path) -> _FilesystemIdentity:
    try:
        return _identity_from_stat(path.lstat())
    except (FileNotFoundError, OSError) as exc:
        raise OwnershipError(f"artifact identity is unavailable: {path}") from exc


def _identity_matches(path: Path, expected: _FilesystemIdentity) -> bool:
    try:
        return _identity_from_stat(path.lstat()) == expected
    except (FileNotFoundError, OSError):
        return False


def _reconcile_rename_after_exception(
    source: Path,
    destination: Path,
    expected: _FilesystemIdentity,
) -> _RenameOutcome:
    unavailable = object()

    def observed_identity(path: Path) -> _FilesystemIdentity | None | object:
        try:
            return _identity_from_stat(path.lstat())
        except FileNotFoundError:
            return None
        except BaseException:
            return unavailable

    source_identity = observed_identity(source)
    destination_identity = observed_identity(destination)
    if source_identity is unavailable or destination_identity is unavailable:
        return _RenameOutcome.AMBIGUOUS
    if source_identity is None and destination_identity == expected:
        return _RenameOutcome.MOVED
    if source_identity == expected and destination_identity != expected:
        return _RenameOutcome.NOT_MOVED
    return _RenameOutcome.AMBIGUOUS


def _require_identity(
    path: Path,
    expected: _FilesystemIdentity,
    message: str,
) -> None:
    if not _identity_matches(path, expected):
        raise OwnershipError(f"{message}: {path}")


def _physical_link_mode(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    if _is_windows_junction(path):
        return "junction"
    return None


def _without_windows_namespace(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\??\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    if path.startswith("\\??\\"):
        return path[4:]
    return path


def _path_identity(path: str | Path, *, relative_to: Path | None = None) -> str:
    text = os.fspath(path)
    if os.name == "nt":
        text = _without_windows_namespace(text)
    candidate = Path(text)
    if not candidate.is_absolute() and relative_to is not None:
        candidate = relative_to / candidate
    return os.path.normcase(os.path.abspath(os.path.normpath(candidate)))


def _link_points_to(
    path: Path,
    expected: str | Path,
    *,
    relative_to: Path | None = None,
) -> bool:
    try:
        raw_target = os.readlink(path)
    except (OSError, RuntimeError, ValueError):
        return False
    return _path_identity(
        raw_target,
        relative_to=path.parent if relative_to is None else relative_to,
    ) == _path_identity(expected)


def _is_live_link(path: Path) -> bool:
    return path.is_symlink() or _is_windows_reparse_directory(path)


def _artifact_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _remove_artifact(
    path: Path,
    *,
    directory_tree_authorized: bool = False,
) -> None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    attributes = int(getattr(metadata, "st_file_attributes", 0))
    is_reparse_directory = bool(
        os.name == "nt"
        and attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        and attributes & getattr(stat, "FILE_ATTRIBUTE_DIRECTORY", 0)
    )
    if stat.S_ISLNK(metadata.st_mode):
        path.unlink()
    elif is_reparse_directory:
        os.rmdir(path)
    elif stat.S_ISDIR(metadata.st_mode):
        if not directory_tree_authorized:
            _tree_hash(path)
        shutil.rmtree(path)
    else:
        path.unlink()
    if _artifact_exists(path):
        raise OSError(f"artifact removal did not remove pathname: {path}")


def _cleanup_owned_scratch(
    path: Path,
    identity: _FilesystemIdentity | None,
) -> None:
    if not _artifact_exists(path):
        return
    if identity is None or not _identity_matches(path, identity):
        raise OwnershipError(f"scratch artifact identity changed: {path}")
    _remove_artifact(path)


def _cleanup_allocated_directory(
    path: Path,
    identity: _FilesystemIdentity | None,
) -> None:
    if identity is not None:
        _cleanup_owned_scratch(path, identity)
        return
    try:
        path.rmdir()
    except FileNotFoundError:
        return
    if _artifact_exists(path):
        raise OSError(f"scratch directory removal did not remove pathname: {path}")


def _cleanup_empty_allocated_directory(
    path: Path,
    identity: _FilesystemIdentity | None,
) -> None:
    if not _artifact_exists(path):
        return
    if identity is not None:
        _require_identity(
            path,
            identity,
            "quarantine scratch directory was replaced",
        )
    path.rmdir()
    if _artifact_exists(path):
        raise OSError(f"scratch directory removal did not remove pathname: {path}")


def _create_junction(link: Path, target: Path) -> None:
    if os.name != "nt":
        raise OSError("directory junctions are only available on Windows")

    import ctypes
    from ctypes import wintypes

    target = target.resolve(strict=True)
    if not target.is_dir():
        raise NotADirectoryError(target)

    target_text = str(target)
    if target_text.startswith("\\\\"):
        substitute = "\\??\\UNC\\" + target_text[2:]
    else:
        substitute = "\\??\\" + target_text
    substitute_bytes = substitute.encode("utf-16-le")
    print_bytes = target_text.encode("utf-16-le")
    path_buffer = substitute_bytes + b"\0\0" + print_bytes + b"\0\0"
    reparse_data_length = 8 + len(path_buffer)
    reparse_buffer = struct.pack(
        "<LHHHHHH",
        0xA0000003,  # IO_REPARSE_TAG_MOUNT_POINT
        reparse_data_length,
        0,
        0,
        len(substitute_bytes),
        len(substitute_bytes) + 2,
        len(print_bytes),
    ) + path_buffer

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.DeviceIoControl.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    )
    kernel32.DeviceIoControl.restype = wintypes.BOOL

    os.mkdir(link)
    handle = kernel32.CreateFileW(
        str(link),
        0x40000000,  # GENERIC_WRITE
        0x00000001 | 0x00000002 | 0x00000004,  # share read/write/delete
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x02000000,  # OPEN_REPARSE_POINT | BACKUP_SEMANTICS
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        error = ctypes.WinError(ctypes.get_last_error())
        os.rmdir(link)
        raise error

    try:
        returned = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(reparse_buffer)
        ok = kernel32.DeviceIoControl(
            handle,
            0x000900A4,  # FSCTL_SET_REPARSE_POINT
            buffer,
            len(reparse_buffer),
            None,
            0,
            ctypes.byref(returned),
            None,
        )
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException:
        kernel32.CloseHandle(handle)
        os.rmdir(link)
        raise
    else:
        kernel32.CloseHandle(handle)

    try:
        target_matches = link.resolve(strict=True) == target
    except (OSError, RuntimeError):
        target_matches = False
    if not _is_windows_junction(link) or not target_matches:
        os.rmdir(link)
        raise OSError("created junction did not resolve to the requested target")


def _owned_artifact_is_unchanged(
    path: Path,
    entry: Mapping[str, Any],
    *,
    link_parent: Path | None = None,
) -> bool:
    mode = entry.get("mode")
    if mode in {"symlink", "junction"}:
        if _physical_link_mode(path) != mode:
            return False
        try:
            target_is_available = Path(entry["target"]).resolve(strict=True).is_dir()
        except (OSError, RuntimeError):
            target_is_available = False
        return target_is_available and _link_points_to(
            path,
            str(entry["target"]),
            relative_to=link_parent,
        )
    if _is_live_link(path) or not path.is_dir():
        return False
    try:
        return _tree_hash(path) == entry.get("artifact_hash")
    except (OSError, OwnershipError):
        return False


def _legacy_source_hash(skill: Skill) -> str:
    digest = hashlib.sha256()
    for path in iter_skill_files(skill.source_dir):
        digest.update(path.relative_to(skill.source_dir).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _layout_directories(files: Mapping[str, bytes]) -> set[str]:
    directories = {"."}
    for relative in files:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            directories.add(parent.as_posix())
            parent = parent.parent
    return directories


def _expected_artifact_layouts(
    skill: Skill,
    mode: str,
    recorded_source_hash: str,
) -> tuple[tuple[set[str], dict[str, bytes]], ...]:
    supporting = {
        path.relative_to(skill.source_dir).as_posix(): path.read_bytes()
        for path in iter_skill_files(skill.source_dir)
        if path.name != "SKILL.md"
        and not any(
            part.startswith(".")
            for part in path.relative_to(skill.source_dir).parts
        )
    }
    if mode == "copy-fallback":
        files = {
            "SKILL.md": (skill.source_dir / "SKILL.md").read_bytes(),
            **supporting,
        }
        return ((_layout_directories(files), files),)
    if mode != "materialized":
        return ()

    rendered = render_skill(skill)
    current_marker = f"source-sha256: {skill.source_hash}"
    if rendered.count(current_marker) != 1:
        return ()
    rendered = rendered.replace(
        current_marker,
        f"source-sha256: {recorded_source_hash}",
    )
    rendered_bytes = {rendered.encode("utf-8")}
    if os.linesep != "\n":
        rendered_bytes.add(rendered.replace("\n", os.linesep).encode("utf-8"))
    layouts = []
    for skill_bytes in rendered_bytes:
        files = {"SKILL.md": skill_bytes, **supporting}
        layouts.append((_layout_directories(files), files))
    return tuple(layouts)


def _artifact_layout(path: Path) -> tuple[set[str], dict[str, bytes]] | None:
    if not path.is_dir() or _is_live_link(path):
        return None
    directories = {"."}
    files: dict[str, bytes] = {}

    def visit(directory: Path) -> bool:
        for child in sorted(directory.iterdir(), key=lambda item: item.name):
            if child.is_symlink() or _is_windows_reparse_point(child):
                return False
            relative = child.relative_to(path).as_posix()
            if child.is_dir():
                directories.add(relative)
                if not visit(child):
                    return False
            elif child.is_file():
                files[relative] = child.read_bytes()
            else:
                return False
        return True

    return (directories, files) if visit(path) else None


def _legacy_entry_matches_current_artifact(
    path: Path,
    entry: Mapping[str, Any],
    skill: Skill,
    *,
    version: int,
) -> bool:
    if (
        entry.get("source") != skill.relative_dir.as_posix()
        or entry.get("classification") != skill.classification.value
    ):
        return False
    mode = str(entry.get("mode"))
    if mode in {"symlink", "junction"}:
        return (
            skill.classification is Classification.DIRECT
            and _physical_link_mode(path) == mode
            and _link_points_to(path, str(entry["target"]))
            and _path_identity(str(entry["target"]))
            == _path_identity(skill.source_dir)
        )

    legacy_source_hash = _legacy_source_hash(skill)
    accepted_source_hashes = {legacy_source_hash}
    if version == 2:
        accepted_source_hashes.add(skill.source_hash)
    recorded_source_hash = str(entry.get("source_hash"))
    if recorded_source_hash not in accepted_source_hashes:
        return False

    actual_layout = _artifact_layout(path)
    if (
        actual_layout is None
        or _legacy_tree_hash(path) != entry.get("artifact_hash")
    ):
        return False
    return actual_layout in _expected_artifact_layouts(
        skill,
        mode,
        recorded_source_hash,
    )


def _migrate_legacy_entries(
    manifest: Mapping[str, Any],
    desired: Mapping[str, Skill],
    destination: Path,
    *,
    version: int,
) -> dict[str, Any]:
    migrated: dict[str, Any] = {}
    for name, entry in sorted(manifest["skills"].items()):
        skill = desired.get(name)
        target = destination / name
        if (
            skill is None
            or not _artifact_exists(target)
            or not _legacy_entry_matches_current_artifact(
                target,
                entry,
                skill,
                version=version,
            )
        ):
            raise OwnershipError(
                "legacy manifest artifact identity is unverified for "
                f"{name}; explicit operator reconciliation is required"
            )
        mode = str(entry["mode"])
        new_entry: dict[str, Any] = {
            "source": skill.relative_dir.as_posix(),
            "source_hash": skill.source_hash,
            "classification": skill.classification.value,
            "mode": mode,
            "artifact_hash": _tree_hash(target),
            "desired_output_identity": _desired_output_identity(skill, mode),
        }
        if mode in {"symlink", "junction"}:
            new_entry["target"] = str(skill.source_dir.resolve(strict=True))
        migrated[name] = new_entry
    return migrated


def _copy_supporting_files(skill: Skill, target: Path) -> None:
    for source_file in iter_skill_files(skill.source_dir):
        if source_file.name == "SKILL.md":
            continue
        relative = source_file.relative_to(skill.source_dir)
        if any(part.startswith(".") for part in relative.parts):
            continue
        destination_file = target / relative
        destination_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination_file)


def _materialize(
    skill: Skill,
    target: Path,
    direct_copy: bool = False,
    *,
    target_precreated: bool = False,
) -> None:
    if not target_precreated:
        target.mkdir()
    if direct_copy:
        shutil.copy2(skill.source_dir / "SKILL.md", target / "SKILL.md")
    else:
        (target / "SKILL.md").write_text(render_skill(skill), encoding="utf-8", newline="\n")
    _copy_supporting_files(skill, target)


def _install(
    skill: Skill,
    destination: Path,
    prefer_symlinks: bool,
) -> _StagedArtifact:
    staging_identity: _FilesystemIdentity | None = None
    staging = destination
    staging_bound = False
    completed = False
    staging_name = tempfile.mkdtemp(
        dir=destination,
        prefix=f".{skill.name}.adapter-tmp-",
    )
    try:
        staging = Path(staging_name)
        staging_bound = True
        staging_identity = _artifact_identity(staging)
        mode = "materialized"
        target_path: str | None = None
        if skill.classification is Classification.DIRECT and prefer_symlinks:
            _require_identity(
                staging,
                staging_identity,
                "install scratch artifact was replaced before link creation",
            )
            staging.rmdir()
            staging_identity = None
            try:
                staging.symlink_to(skill.source_dir, target_is_directory=True)
                mode = "symlink"
                target_path = str(skill.source_dir.resolve())
            except OSError as exc:
                privilege_denied = getattr(exc, "winerror", None) == 1314 or exc.errno == 1314
                if os.name != "nt" or not privilege_denied:
                    raise
                _create_junction(staging, skill.source_dir)
                mode = "junction"
                target_path = str(skill.source_dir.resolve())
            staging_identity = _artifact_identity(staging)
        elif skill.classification is Classification.DIRECT:
            _materialize(skill, staging, direct_copy=True, target_precreated=True)
            mode = "copy-fallback"
        else:
            _materialize(skill, staging, target_precreated=True)

        if staging_identity is None:
            raise OwnershipError(f"install scratch artifact identity is unavailable: {staging}")
        _require_identity(
            staging,
            staging_identity,
            "install scratch artifact was replaced during staging",
        )
        entry: dict[str, Any] = {
            "source": skill.relative_dir.as_posix(),
            "source_hash": skill.source_hash,
            "classification": skill.classification.value,
            "mode": mode,
            "artifact_hash": _tree_hash(staging),
            "desired_output_identity": _desired_output_identity(skill, mode),
        }
        if target_path is not None:
            entry["target"] = target_path
        result = _StagedArtifact(staging, staging_identity, mode, entry)
        completed = True
        return result
    finally:
        if not completed:
            try:
                if staging_bound:
                    _cleanup_allocated_directory(staging, staging_identity)
                else:
                    os.rmdir(staging_name)
            except BaseException as cleanup_exc:
                raise OwnershipError(
                    f"refusing to clean replaced scratch artifact: {staging_name}"
                ) from cleanup_exc


def _cleanup_staged_artifact(staged: _StagedArtifact) -> None:
    _cleanup_owned_scratch(staged.path, staged.identity)


def _rename_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        os.rename(source, destination)
        return

    import ctypes

    at_fdcwd = -100
    rename_noreplace = 1
    if sys.platform.startswith("linux"):
        libc = ctypes.CDLL(None, use_errno=True)
        machine = platform.machine().lower()
        syscall_number = LINUX_RENAMEAT2_SYSCALLS.get(machine)
        if syscall_number is None:
            raise OSError(
                errno.ENOTSUP,
                f"renameat2 is unsupported on Linux architecture {machine!r}",
                destination,
            )
        try:
            renameat2 = libc.renameat2
        except AttributeError:
            try:
                syscall = libc.syscall
            except AttributeError as exc:
                raise OSError(
                    errno.ENOTSUP,
                    "renameat2(RENAME_NOREPLACE) and libc syscall are unavailable",
                    destination,
                ) from exc
            syscall.restype = ctypes.c_long
            result = syscall(
                ctypes.c_long(syscall_number),
                ctypes.c_int(at_fdcwd),
                ctypes.c_char_p(os.fsencode(source)),
                ctypes.c_int(at_fdcwd),
                ctypes.c_char_p(os.fsencode(destination)),
                ctypes.c_uint(rename_noreplace),
            )
        else:
            renameat2.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            renameat2.restype = ctypes.c_int
            result = renameat2(
                at_fdcwd,
                os.fsencode(source),
                at_fdcwd,
                os.fsencode(destination),
                rename_noreplace,
            )
    elif sys.platform == "darwin":
        libc = ctypes.CDLL(None, use_errno=True)
        try:
            renamex_np = libc.renamex_np
        except AttributeError as exc:
            raise OSError(
                errno.ENOTSUP,
                "renamex_np(RENAME_EXCL) is unavailable",
                destination,
            ) from exc
        renamex_np.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        renamex_np.restype = ctypes.c_int
        result = renamex_np(
            os.fsencode(source),
            os.fsencode(destination),
            0x00000004,
        )
    else:
        raise OSError(
            errno.ENOTSUP,
            "atomic no-replace rename is unavailable on this POSIX platform",
            destination,
        )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination)


def _cleanup_quarantine_root(quarantine: _QuarantinedArtifact) -> None:
    _cleanup_empty_allocated_directory(
        quarantine.root,
        quarantine.root_identity,
    )


def _quarantine_artifact(target: Path) -> _QuarantinedArtifact:
    root_identity: _FilesystemIdentity | None = None
    result: _QuarantinedArtifact | None = None
    moved = False
    rename_ambiguous = False
    root = target.parent
    root_bound = False
    completed = False
    root_name = tempfile.mkdtemp(
        dir=target.parent,
        prefix=f".{target.name}.adapter-quarantine-",
    )
    try:
        root = Path(root_name)
        root_bound = True
        quarantined = root / target.name
        root_identity = _artifact_identity(root)
        before_identity = _artifact_identity(target)
        result = _QuarantinedArtifact(
            root,
            root_identity,
            quarantined,
            before_identity,
            target,
        )
        try:
            _rename_no_replace(target, quarantined)
        except BaseException:
            outcome = _reconcile_rename_after_exception(
                target,
                quarantined,
                before_identity,
            )
            if outcome is _RenameOutcome.MOVED:
                moved = True
            elif outcome is _RenameOutcome.AMBIGUOUS:
                rename_ambiguous = True
            raise
        moved = True
        _require_identity(
            quarantined,
            before_identity,
            "artifact changed while entering quarantine",
        )
        completed = True
        return result
    finally:
        if not completed:
            if moved and result is not None:
                try:
                    _restore_quarantined_artifact(result)
                except BaseException as restore_exc:
                    raise OwnershipError(
                        f"artifact changed while entering quarantine: {target}"
                    ) from restore_exc
            elif not rename_ambiguous:
                try:
                    if not root_bound:
                        os.rmdir(root_name)
                    else:
                        _cleanup_empty_allocated_directory(root, root_identity)
                except BaseException as cleanup_exc:
                    raise OwnershipError(
                        f"refusing to clean quarantine scratch directory: {root_name}"
                    ) from cleanup_exc


def _require_quarantine_identity(quarantine: _QuarantinedArtifact) -> None:
    _require_identity(
        quarantine.root,
        quarantine.root_identity,
        "quarantine scratch directory was replaced",
    )
    _require_identity(
        quarantine.path,
        quarantine.identity,
        "quarantined artifact was replaced",
    )


def _restore_quarantined_artifact(quarantine: _QuarantinedArtifact) -> None:
    _require_quarantine_identity(quarantine)
    if _artifact_exists(quarantine.original_path):
        raise OwnershipError(
            "cannot restore quarantined artifact because the destination is occupied: "
            f"{quarantine.original_path}"
        )
    rename_error: BaseException | None = None
    try:
        _rename_no_replace(quarantine.path, quarantine.original_path)
    except BaseException as exc:
        outcome = _reconcile_rename_after_exception(
            quarantine.path,
            quarantine.original_path,
            quarantine.identity,
        )
        if outcome is _RenameOutcome.MOVED:
            rename_error = exc
        elif isinstance(exc, FileExistsError) and outcome is _RenameOutcome.NOT_MOVED:
            raise OwnershipError(
                "cannot restore quarantined artifact because the destination is occupied: "
                f"{quarantine.original_path}"
            ) from exc
        elif outcome is _RenameOutcome.AMBIGUOUS:
            raise OwnershipError(
                f"cannot reconcile quarantined artifact restore: {quarantine.original_path}"
            ) from exc
        else:
            raise
    _require_identity(
        quarantine.original_path,
        quarantine.identity,
        "restored artifact identity changed",
    )
    _cleanup_quarantine_root(quarantine)
    if rename_error is not None:
        raise rename_error


def _remove_quarantined_artifact(quarantine: _QuarantinedArtifact) -> None:
    _require_quarantine_identity(quarantine)
    _remove_artifact(quarantine.path, directory_tree_authorized=True)
    _cleanup_quarantine_root(quarantine)


def _desired_mode(skill: Skill, prefer_symlinks: bool) -> str:
    if skill.classification is Classification.DIRECT:
        return "symlink" if prefer_symlinks else "copy-fallback"
    return "materialized"


def _mode_satisfies(current: str, skill: Skill, prefer_symlinks: bool) -> bool:
    if skill.classification is not Classification.DIRECT:
        return current == "materialized"
    if prefer_symlinks:
        return current in {"symlink", "junction"}
    return current == "copy-fallback"


def _reject_overlapping_roots(source: Path, destination: Path) -> None:
    if source == destination or source in destination.parents or destination in source.parents:
        raise AdapterError(f"source and destination must not overlap: {source} <> {destination}")


def _resolve_destination(destination: Path) -> Path:
    unresolved = Path(destination).absolute()
    for candidate in (unresolved, *unresolved.parents):
        if candidate.is_symlink():
            relation = "root symlink" if candidate == unresolved else "root symlink ancestor"
            raise AdapterError(f"destination {relation} is not allowed: {candidate}")
        if _is_windows_reparse_point(candidate):
            relation = "root reparse point" if candidate == unresolved else "root reparse ancestor"
            raise AdapterError(f"destination {relation} is not allowed: {candidate}")
    return unresolved.resolve()


def _windows_namespaced_path(path: Path) -> str:
    text = str(path.absolute())
    if text.startswith("\\\\?\\"):
        return text
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


class _PhysicalDirectoryMissing(AdapterError):
    pass


@dataclass
class _PinnedWindowsDirectory:
    path: Path
    handle: int
    parent_handle: int
    volume_serial: int
    file_index: bytes
    created: bool


_WINDOWS_FILE_TRAVERSE = 0x00000020
_WINDOWS_FILE_READ_ATTRIBUTES = 0x00000080
_WINDOWS_DELETE = 0x00010000
_WINDOWS_SYNCHRONIZE = 0x00100000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_FILE_SHARE_DELETE = 0x00000004
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_WINDOWS_FILE_CREATE = 2
_WINDOWS_FILE_OPEN = 1
_WINDOWS_FILE_CREATED = 2
_WINDOWS_FILE_OPENED = 1
_WINDOWS_FILE_DIRECTORY_FILE = 0x00000001
_WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
_WINDOWS_FILE_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_OBJ_CASE_INSENSITIVE = 0x00000040
_WINDOWS_STATUS_OBJECT_NAME_COLLISION = 0xC0000035
_WINDOWS_STATUS_SHARING_VIOLATION = 0xC0000043
_WINDOWS_STATUS_OBJECT_NAME_NOT_FOUND = 0xC0000034
_WINDOWS_STATUS_OBJECT_PATH_NOT_FOUND = 0xC000003A
_WINDOWS_FILE_DISPOSITION_INFO = 4
_WINDOWS_ERROR_DIR_NOT_EMPTY = 145
_WINDOWS_AUTHORITY_CONTENTION_TIMEOUT_SECONDS = 2.0
_WINDOWS_AUTHORITY_CONTENTION_RETRY_SECONDS = 0.005
_WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _windows_directory_identity(handle: int, path: Path) -> tuple[int, bytes, int]:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class FileId128(ctypes.Structure):
        _fields_ = [("Identifier", ctypes.c_ubyte * 16)]

    class FileIdInfo(ctypes.Structure):
        _fields_ = [
            ("VolumeSerialNumber", ctypes.c_ulonglong),
            ("FileId", FileId128),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    result = get_information(wintypes.HANDLE(handle), ctypes.byref(information))
    if type(result) is not int or result != 1:
        error = ctypes.WinError(ctypes.get_last_error())
        raise AdapterError(f"destination directory identity is unavailable: {path}") from error
    attributes = int(information.dwFileAttributes)
    if attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise AdapterError(f"destination directory reparse point is not allowed: {path}")
    if not attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
        raise AdapterError(f"destination path is not a directory: {path}")

    get_information_ex = kernel32.GetFileInformationByHandleEx
    get_information_ex.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    get_information_ex.restype = wintypes.BOOL
    file_id_information = FileIdInfo()
    file_id_info_class = 18
    result = get_information_ex(
        wintypes.HANDLE(handle),
        file_id_info_class,
        ctypes.byref(file_id_information),
        ctypes.sizeof(file_id_information),
    )
    if type(result) is not int or result != 1:
        error = ctypes.WinError(ctypes.get_last_error())
        raise AdapterError(f"destination directory identity is unavailable: {path}") from error
    volume_serial = int(file_id_information.VolumeSerialNumber)
    file_index = bytes(file_id_information.FileId.Identifier)
    if volume_serial == 0 or not any(file_index):
        raise AdapterError(f"destination directory identity is indeterminate: {path}")
    return volume_serial, file_index, attributes


def _open_windows_directory_anchor(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        _windows_namespaced_path(path),
        _WINDOWS_FILE_TRAVERSE
        | _WINDOWS_FILE_READ_ATTRIBUTES
        | _WINDOWS_SYNCHRONIZE,
        _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
        None,
        _WINDOWS_OPEN_EXISTING,
        _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT | _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if type(handle) is not int or handle in {0, invalid_handle}:
        error = ctypes.WinError(ctypes.get_last_error())
        raise AdapterError(f"destination anchor could not be pinned: {path}") from error
    return handle


def _ntstatus_error(status: int, path: Path, operation: str) -> AdapterError:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll")
    convert = ntdll.RtlNtStatusToDosError
    convert.argtypes = (wintypes.LONG,)
    convert.restype = wintypes.ULONG
    code = int(convert(status))
    return AdapterError(
        f"destination directory {operation} failed: {path} "
        f"(NTSTATUS 0x{status & 0xFFFFFFFF:08x}, WinError {code})"
    )


def _nt_open_relative_directory(
    parent_handle: int,
    name: str,
    path: Path,
    *,
    create_missing: bool,
    delete_access: bool = False,
    share_delete: bool = False,
) -> tuple[int, bool]:
    import ctypes
    from ctypes import wintypes

    if (
        not name
        or name in {".", ".."}
        or "\\" in name
        or "/" in name
        or "\x00" in name
        or ":" in name
        or name.endswith((" ", "."))
        or name.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES
    ):
        raise AdapterError(f"destination path component is not allowed: {path}")

    class UnicodeString(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.USHORT),
            ("MaximumLength", wintypes.USHORT),
            ("Buffer", wintypes.LPWSTR),
        ]

    class ObjectAttributes(ctypes.Structure):
        _fields_ = [
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        ]

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    ntdll = ctypes.WinDLL("ntdll")
    nt_create_file = ntdll.NtCreateFile
    nt_create_file.argtypes = (
        ctypes.POINTER(wintypes.HANDLE),
        wintypes.ULONG,
        ctypes.POINTER(ObjectAttributes),
        ctypes.POINTER(IoStatusBlock),
        ctypes.c_void_p,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        wintypes.ULONG,
        ctypes.c_void_p,
        wintypes.ULONG,
    )
    nt_create_file.restype = wintypes.LONG

    name_buffer = ctypes.create_unicode_buffer(name)
    byte_length = len(name.encode("utf-16-le"))
    unicode_name = UnicodeString(
        byte_length,
        byte_length + ctypes.sizeof(ctypes.c_wchar),
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    object_attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        wintypes.HANDLE(parent_handle),
        ctypes.pointer(unicode_name),
        _WINDOWS_OBJ_CASE_INSENSITIVE,
        None,
        None,
    )

    def attempt(
        disposition: int,
        desired_access: int,
        *,
        allow_delete_share: bool,
    ) -> tuple[int, int, int]:
        output_handle = wintypes.HANDLE()
        io_status = IoStatusBlock()
        status = nt_create_file(
            ctypes.byref(output_handle),
            desired_access,
            ctypes.byref(object_attributes),
            ctypes.byref(io_status),
            None,
            stat.FILE_ATTRIBUTE_NORMAL,
            _WINDOWS_FILE_SHARE_READ
            | _WINDOWS_FILE_SHARE_WRITE
            | (_WINDOWS_FILE_SHARE_DELETE if allow_delete_share else 0),
            disposition,
            _WINDOWS_FILE_DIRECTORY_FILE
            | _WINDOWS_FILE_SYNCHRONOUS_IO_NONALERT
            | _WINDOWS_FILE_OPEN_REPARSE_POINT,
            None,
            0,
        )
        handle_value = output_handle.value

        def reject_malformed(message: str) -> None:
            malformed_error = AdapterError(f"{message}: {path}")
            if type(handle_value) is int and handle_value != 0:
                try:
                    _close_windows_directory_handle(handle_value, path)
                except BaseException as cleanup_error:
                    raise malformed_error from cleanup_error
            raise malformed_error

        if type(status) is not int:
            reject_malformed("destination native status is malformed")
        information = io_status.Information
        if type(information) is not int:
            reject_malformed("destination native result is malformed")
        if handle_value is not None and type(handle_value) is not int:
            reject_malformed("destination native handle is malformed")
        return status, int(handle_value or 0), information

    base_access = (
        _WINDOWS_FILE_TRAVERSE
        | _WINDOWS_FILE_READ_ATTRIBUTES
        | _WINDOWS_SYNCHRONIZE
    )
    open_access = base_access | (_WINDOWS_DELETE if delete_access else 0)
    creation_contended = False
    if create_missing:
        status, handle, information = attempt(
            _WINDOWS_FILE_CREATE,
            base_access | _WINDOWS_DELETE,
            allow_delete_share=True,
        )
        if status == 0:
            if handle == 0 or information != _WINDOWS_FILE_CREATED:
                if handle:
                    _close_windows_directory_handle(handle, path)
                raise AdapterError(f"destination create result is ambiguous: {path}")
            return handle, True
        if status & 0xFFFFFFFF not in {
            _WINDOWS_STATUS_OBJECT_NAME_COLLISION,
            _WINDOWS_STATUS_SHARING_VIOLATION,
        }:
            if handle:
                _close_windows_directory_handle(handle, path)
            raise _ntstatus_error(status, path, "creation")
        if handle:
            _close_windows_directory_handle(handle, path)
        creation_contended = True

    status, handle, information = attempt(
        _WINDOWS_FILE_OPEN,
        open_access,
        allow_delete_share=share_delete,
    )
    if creation_contended:
        deadline = time.monotonic() + _WINDOWS_AUTHORITY_CONTENTION_TIMEOUT_SECONDS
        while status & 0xFFFFFFFF == _WINDOWS_STATUS_SHARING_VIOLATION:
            if handle:
                _close_windows_directory_handle(handle, path)
                handle = 0
            if time.monotonic() >= deadline:
                break
            time.sleep(_WINDOWS_AUTHORITY_CONTENTION_RETRY_SECONDS)
            status, handle, information = attempt(
                _WINDOWS_FILE_OPEN,
                open_access,
                allow_delete_share=share_delete,
            )
    unsigned_status = status & 0xFFFFFFFF
    if unsigned_status in {
        _WINDOWS_STATUS_OBJECT_NAME_NOT_FOUND,
        _WINDOWS_STATUS_OBJECT_PATH_NOT_FOUND,
    }:
        if handle:
            _close_windows_directory_handle(handle, path)
        raise _PhysicalDirectoryMissing(f"destination directory is absent: {path}")
    if status != 0:
        if handle:
            _close_windows_directory_handle(handle, path)
        raise _ntstatus_error(status, path, "open")
    if handle == 0 or information != _WINDOWS_FILE_OPENED:
        if handle:
            _close_windows_directory_handle(handle, path)
        raise AdapterError(f"destination open result is ambiguous: {path}")
    return handle, False


def _close_windows_directory_handle(handle: int, path: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    result = close_handle(wintypes.HANDLE(handle))
    if type(result) is not int or result == 0:
        error = ctypes.WinError(ctypes.get_last_error())
        raise AdapterError(f"destination directory handle close failed: {path}") from error


def _set_windows_directory_delete_disposition(handle: int, path: Path) -> bool:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", ctypes.c_ubyte)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    disposition = FileDispositionInfo(1)
    result = set_information(
        wintypes.HANDLE(handle),
        _WINDOWS_FILE_DISPOSITION_INFO,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    )
    if type(result) is not int or result == 0:
        code = ctypes.get_last_error()
        if code == _WINDOWS_ERROR_DIR_NOT_EMPTY:
            return False
        error = ctypes.WinError(code)
        raise AdapterError(f"created destination directory rollback failed: {path}") from error
    return True


def _mark_windows_directory_for_deletion(entry: _PinnedWindowsDirectory) -> bool:
    volume_serial, file_index, _ = _windows_directory_identity(entry.handle, entry.path)
    if (volume_serial, file_index) != (entry.volume_serial, entry.file_index):
        raise AdapterError(f"created destination directory identity changed: {entry.path}")
    return _set_windows_directory_delete_disposition(entry.handle, entry.path)


def _stabilize_created_windows_directory(
    entry: _PinnedWindowsDirectory,
) -> None:
    temporary_handle = 0
    try:
        shared_handle, shared_created = _nt_open_relative_directory(
            entry.parent_handle,
            entry.path.name,
            entry.path,
            create_missing=False,
            share_delete=True,
        )
        temporary_handle = shared_handle
        if shared_created:
            raise AdapterError(f"created destination directory reopen is ambiguous: {entry.path}")
        shared_volume, shared_index, _ = _windows_directory_identity(
            shared_handle,
            entry.path,
        )
        if (shared_volume, shared_index) != (entry.volume_serial, entry.file_index):
            raise AdapterError(f"created destination directory identity changed: {entry.path}")

        initial_handle = entry.handle
        entry.handle = shared_handle
        temporary_handle = 0
        _close_windows_directory_handle(initial_handle, entry.path)

        retained_handle, retained_created = _nt_open_relative_directory(
            entry.parent_handle,
            entry.path.name,
            entry.path,
            create_missing=False,
        )
        temporary_handle = retained_handle
        if retained_created:
            raise AdapterError(f"created destination directory reopen is ambiguous: {entry.path}")
        retained_volume, retained_index, _ = _windows_directory_identity(
            retained_handle,
            entry.path,
        )
        if (retained_volume, retained_index) != (entry.volume_serial, entry.file_index):
            raise AdapterError(f"created destination directory identity changed: {entry.path}")

        shared_handle = entry.handle
        entry.handle = retained_handle
        temporary_handle = 0
        _close_windows_directory_handle(shared_handle, entry.path)
    except BaseException as stabilization_error:
        if temporary_handle:
            try:
                _close_windows_directory_handle(temporary_handle, entry.path)
            except BaseException as cleanup_error:
                raise stabilization_error.with_traceback(
                    stabilization_error.__traceback__
                ) from cleanup_error
        raise


def _windows_directory_chain(directory: Path) -> tuple[Path, tuple[str, ...]]:
    absolute = directory.absolute()
    if not absolute.is_absolute() or not absolute.anchor:
        raise AdapterError(f"destination directory must be absolute: {directory}")
    anchor = Path(absolute.anchor)
    try:
        components = absolute.relative_to(anchor).parts
    except ValueError as exc:
        raise AdapterError(f"destination directory anchor is invalid: {directory}") from exc
    return anchor, components


def _release_windows_directory_chain(
    chain: list[_PinnedWindowsDirectory],
    *,
    rollback: bool,
) -> None:
    failures: list[BaseException] = []
    for entry in reversed(chain):
        if rollback and entry.created:
            try:
                owned_handle = entry.handle
                entry.handle = 0
                _close_windows_directory_handle(owned_handle, entry.path)
                rollback_handle, rollback_created = _nt_open_relative_directory(
                    entry.parent_handle,
                    entry.path.name,
                    entry.path,
                    create_missing=False,
                    delete_access=True,
                )
                if rollback_created:
                    raise AdapterError(
                        f"created destination rollback reopened a new directory: {entry.path}"
                    )
                rollback_entry = _PinnedWindowsDirectory(
                    entry.path,
                    rollback_handle,
                    entry.parent_handle,
                    entry.volume_serial,
                    entry.file_index,
                    False,
                )
                try:
                    _mark_windows_directory_for_deletion(rollback_entry)
                finally:
                    _close_windows_directory_handle(rollback_handle, entry.path)
            except BaseException as exc:
                failures.append(exc)
        if entry.handle:
            try:
                _close_windows_directory_handle(entry.handle, entry.path)
            except BaseException as exc:
                failures.append(exc)
    if failures:
        raise AdapterError("destination directory authority cleanup was incomplete") from failures[0]


@contextmanager
def _pinned_physical_directory(
    directory: Path,
    *,
    create_missing: bool = True,
    parent_authority: list[_PinnedWindowsDirectory] | None = None,
) -> Iterator[list[_PinnedWindowsDirectory] | None]:
    directory = directory.absolute()
    if os.name != "nt":
        if create_missing:
            if parent_authority is None:
                directory.mkdir(parents=True, exist_ok=True)
            else:
                directory.mkdir(exist_ok=True)
        elif not directory.is_dir():
            raise _PhysicalDirectoryMissing(f"destination directory is absent: {directory}")
        yield None
        return

    chain: list[_PinnedWindowsDirectory] = []
    try:
        if parent_authority is None:
            anchor, components = _windows_directory_chain(directory)
            anchor_handle = _open_windows_directory_anchor(anchor)
            try:
                volume_serial, file_index, _ = _windows_directory_identity(
                    anchor_handle,
                    anchor,
                )
            except BaseException as identity_error:
                try:
                    _close_windows_directory_handle(anchor_handle, anchor)
                except BaseException as cleanup_error:
                    raise identity_error.with_traceback(
                        identity_error.__traceback__
                    ) from cleanup_error
                raise
            chain.append(
                _PinnedWindowsDirectory(
                    anchor,
                    anchor_handle,
                    0,
                    volume_serial,
                    file_index,
                    False,
                )
            )
            current = anchor
            parent_handle = anchor_handle
        else:
            if not parent_authority or directory.parent != parent_authority[-1].path:
                raise AdapterError(
                    f"destination parent authority does not match child: {directory}"
                )
            components = (directory.name,)
            volume_serial = parent_authority[-1].volume_serial
            current = parent_authority[-1].path
            parent_handle = parent_authority[-1].handle
        for component in components:
            current = current / component
            handle, created = _nt_open_relative_directory(
                parent_handle,
                component,
                current,
                create_missing=create_missing,
            )
            try:
                child_volume, child_index, _ = _windows_directory_identity(handle, current)
            except BaseException as identity_error:
                cleanup_failures: list[BaseException] = []
                if created:
                    try:
                        removed = _set_windows_directory_delete_disposition(handle, current)
                        if not removed:
                            cleanup_failures.append(
                                AdapterError(
                                    f"created destination directory is not empty: {current}"
                                )
                            )
                    except BaseException as cleanup_error:
                        cleanup_failures.append(cleanup_error)
                try:
                    _close_windows_directory_handle(handle, current)
                except BaseException as cleanup_error:
                    cleanup_failures.append(cleanup_error)
                if cleanup_failures:
                    cleanup_error = AdapterError(
                        "created destination directory cleanup was incomplete"
                    )
                    cleanup_error.__cause__ = cleanup_failures[0]
                    raise identity_error.with_traceback(
                        identity_error.__traceback__
                    ) from cleanup_error
                raise
            entry = _PinnedWindowsDirectory(
                current,
                handle,
                parent_handle,
                child_volume,
                child_index,
                created,
            )
            chain.append(entry)
            if child_volume != volume_serial:
                raise AdapterError(
                    f"destination directory crosses a volume boundary: {current}"
                )
            if created:
                _stabilize_created_windows_directory(entry)
            parent_handle = entry.handle
    except BaseException as establishment_error:
        try:
            _release_windows_directory_chain(chain, rollback=True)
        except BaseException as cleanup_error:
            raise establishment_error.with_traceback(
                establishment_error.__traceback__
            ) from cleanup_error
        raise

    try:
        yield chain
    except BaseException as body_error:
        try:
            _release_windows_directory_chain(chain, rollback=True)
        except BaseException as cleanup_error:
            raise body_error.with_traceback(body_error.__traceback__) from cleanup_error
        raise

    _release_windows_directory_chain(chain, rollback=False)


def _destination_lock_path(destination: Path) -> Path:
    lock_parent = destination.parent
    metadata = lock_parent.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise AdapterError(
            f"destination lock parent is not a physical directory: {lock_parent}"
        )
    if os.name == "nt" and int(getattr(metadata, "st_file_attributes", 0)) & getattr(
        stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        0,
    ):
        raise AdapterError(
            f"destination lock parent is a reparse point: {lock_parent}"
        )
    destination_key = hashlib.sha256(
        _path_identity(destination).encode("utf-8")
    ).hexdigest()[:24]
    return lock_parent / f".{LEGACY_PROTOCOL_ID}-{destination_key}.lock"


def _acquire_windows_file_lock(descriptor: int):
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_void_p),
            ("InternalHigh", ctypes.c_void_p),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    lock_file_ex = kernel32.LockFileEx
    lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(Overlapped),
    ]
    lock_file_ex.restype = wintypes.BOOL
    handle = wintypes.HANDLE(msvcrt.get_osfhandle(descriptor))
    overlapped = Overlapped()
    if not lock_file_ex(
        handle,
        0x00000002,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, handle, overlapped


def _release_windows_file_lock(kernel32, handle, overlapped) -> None:
    import ctypes
    from ctypes import wintypes

    unlock_file_ex = kernel32.UnlockFileEx
    unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
    ]
    unlock_file_ex.restype = wintypes.BOOL
    if not unlock_file_ex(handle, 0, 1, 0, ctypes.byref(overlapped)):
        raise ctypes.WinError(ctypes.get_last_error())


@contextmanager
def _destination_transaction_lock(destination: Path) -> Iterator[None]:
    lock_path = _destination_lock_path(destination)
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    windows_lock = None
    try:
        if os.name == "nt":
            windows_lock = _acquire_windows_file_lock(descriptor)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt" and windows_lock is not None:
                _release_windows_file_lock(*windows_lock)
            elif os.name != "nt":
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _validate_manifest_source(manifest: Mapping[str, Any], source: Path) -> None:
    recorded = manifest.get("source")
    if manifest.get("skills") and not isinstance(recorded, str):
        raise OwnershipError("ownership manifest with skills is missing a valid source")
    if manifest.get("skills") and Path(recorded).resolve() != source:
        raise OwnershipError(f"destination belongs to a different source: {recorded}")
    for name, entry in manifest.get("skills", {}).items():
        if entry.get("mode") in {"symlink", "junction"}:
            expected = source / PurePosixPath(entry["source"])
            actual = str(entry["target"])
            if _path_identity(actual) != _path_identity(expected):
                raise OwnershipError(f"invalid manifest symlink target for {name}: {actual}")


def _live_target_matches_desired(entry: Mapping[str, Any], skill: Skill) -> bool:
    return (
        entry.get("mode") in {"symlink", "junction"}
        and isinstance(entry.get("target"), str)
        and _path_identity(str(entry["target"]))
        == _path_identity(skill.source_dir)
    )


def _owned_live_link_can_be_relocated(
    path: Path,
    entry: Mapping[str, Any],
    skill: Skill,
    *,
    link_parent: Path | None = None,
) -> bool:
    mode = entry.get("mode")
    return (
        skill.classification is Classification.DIRECT
        and mode in {"symlink", "junction"}
        and entry.get("source") != skill.relative_dir.as_posix()
        and _physical_link_mode(path) == mode
        and _link_points_to(
            path,
            str(entry["target"]),
            relative_to=link_parent,
        )
        and not _live_target_matches_desired(entry, skill)
    )


def _manifest_state_matches(
    state: _ManifestState,
    expected: Mapping[str, Any],
) -> bool:
    expected_content = (
        json.dumps(expected, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    return state.identity is not None and state.content == expected_content


def _modern_quarantine_matches(
    quarantine: _QuarantinedArtifact,
    entry: Mapping[str, Any],
    *,
    skill: Skill | None = None,
    allow_relocation: bool = False,
) -> bool:
    _require_quarantine_identity(quarantine)
    unchanged = _owned_artifact_is_unchanged(
        quarantine.path,
        entry,
        link_parent=quarantine.original_path.parent,
    )
    return unchanged or (
        allow_relocation
        and skill is not None
        and _owned_live_link_can_be_relocated(
            quarantine.path,
            entry,
            skill,
            link_parent=quarantine.original_path.parent,
        )
    )


def _legacy_live_quarantine_matches(
    quarantine: _QuarantinedArtifact,
    entry: Mapping[str, Any],
) -> bool:
    _require_quarantine_identity(quarantine)
    mode = entry.get("mode")
    return (
        mode in {"symlink", "junction"}
        and _physical_link_mode(quarantine.path) == mode
        and _link_points_to(
            quarantine.path,
            str(entry["target"]),
            relative_to=quarantine.original_path.parent,
        )
        and Path(entry["target"]).is_dir()
    )


def _restore_unproven_quarantine(
    quarantine: _QuarantinedArtifact,
    message: str,
) -> None:
    try:
        _restore_quarantined_artifact(quarantine)
    except BaseException as restore_exc:
        raise OwnershipError(message) from restore_exc
    raise OwnershipError(message)


def _validate_staged_artifact(staged: _StagedArtifact, target: Path) -> None:
    _require_identity(
        staged.path,
        staged.identity,
        "staged artifact was replaced before commit",
    )
    if not _owned_artifact_is_unchanged(staged.path, staged.entry):
        raise OwnershipError(f"staged artifact changed before commit: {staged.path}")
    if _artifact_exists(target):
        raise OwnershipError(f"refusing to overwrite unmanaged Claude skill: {target}")


def _verify_installed_artifact(staged: _StagedArtifact, target: Path) -> None:
    _require_identity(
        target,
        staged.identity,
        "installed artifact identity changed during commit",
    )
    if not _owned_artifact_is_unchanged(target, staged.entry):
        raise OwnershipError(f"installed artifact changed during commit: {target}")


def _rollback_install(
    target: Path,
    staged: _StagedArtifact,
    previous: _QuarantinedArtifact | None,
) -> None:
    replacement: _QuarantinedArtifact | None = None
    if _artifact_exists(target):
        replacement = _quarantine_artifact(target)
        if (
            replacement.identity != staged.identity
            or not _modern_quarantine_matches(replacement, staged.entry)
        ):
            _restore_unproven_quarantine(
                replacement,
                f"installed artifact changed while rolling back: {target}",
            )
    if previous is not None:
        _restore_quarantined_artifact(previous)
    if replacement is not None:
        if not _modern_quarantine_matches(replacement, staged.entry):
            raise OwnershipError(
                f"installed artifact changed before rollback cleanup: {target}"
            )
        _remove_quarantined_artifact(replacement)


def _install_and_commit(
    skill: Skill,
    destination: Path,
    prefer_symlinks: bool,
    entry: Mapping[str, Any] | None,
    owned: dict[str, Any],
    manifest_base: Mapping[str, Any],
    manifest_state: _ManifestState,
) -> str:
    target = destination / skill.name
    staged = _install(skill, destination, prefer_symlinks)
    previous: _QuarantinedArtifact | None = None
    target_committed = False
    target_publication_ambiguous = False
    manifest_committed = False
    delayed_write_error: BaseException | None = None

    try:
        if _artifact_exists(target):
            if entry is None:
                raise OwnershipError(f"refusing to overwrite unmanaged Claude skill: {target}")
            candidate = _quarantine_artifact(target)
            previous = candidate
            if not _modern_quarantine_matches(
                candidate,
                entry,
                skill=skill,
                allow_relocation=True,
            ):
                _restore_unproven_quarantine(
                    candidate,
                    f"modified owned artifact: {target}",
                )

        _validate_staged_artifact(staged, target)
        try:
            _rename_no_replace(staged.path, target)
        except BaseException as exc:
            outcome = _reconcile_rename_after_exception(
                staged.path,
                target,
                staged.identity,
            )
            if outcome is _RenameOutcome.MOVED:
                target_committed = True
            elif outcome is _RenameOutcome.AMBIGUOUS:
                target_publication_ambiguous = True
            if isinstance(exc, FileExistsError) and outcome is _RenameOutcome.NOT_MOVED:
                raise OwnershipError(
                    f"refusing to overwrite unmanaged Claude skill: {target}"
                ) from exc
            raise
        target_committed = True
        _verify_installed_artifact(staged, target)
        new_owned = dict(owned)
        new_owned[skill.name] = staged.entry
        next_manifest = {**manifest_base, "skills": new_owned}
        try:
            _write_manifest(destination, next_manifest, manifest_state)
        except BaseException as exc:
            if not _manifest_state_matches(manifest_state, next_manifest):
                raise
            delayed_write_error = exc
        manifest_committed = True
        owned[skill.name] = staged.entry
    except BaseException:
        if not manifest_committed and not target_publication_ambiguous:
            try:
                if target_committed:
                    _rollback_install(target, staged, previous)
                elif previous is not None and _artifact_exists(previous.path):
                    _restore_quarantined_artifact(previous)
            except BaseException as rollback_exc:
                raise OwnershipError(
                    f"failed closed while rolling back destination replacement: {target}"
                ) from rollback_exc
        if not target_publication_ambiguous:
            try:
                _cleanup_staged_artifact(staged)
            except BaseException as cleanup_exc:
                raise OwnershipError(
                    f"refusing to clean replaced scratch artifact: {staged.path}"
                ) from cleanup_exc
        raise

    try:
        _cleanup_staged_artifact(staged)
    except BaseException as cleanup_exc:
        raise OwnershipError(
            f"refusing to clean replaced scratch artifact: {staged.path}"
        ) from cleanup_exc

    if previous is not None:
        if not _modern_quarantine_matches(
            previous,
            entry,
            skill=skill,
            allow_relocation=True,
        ):
            raise OwnershipError(
                f"quarantined owned artifact changed before removal: {target}"
            )
        _remove_quarantined_artifact(previous)

    if delayed_write_error is not None:
        raise delayed_write_error
    return staged.mode


def _remove_and_commit(
    name: str,
    target: Path,
    entry: Mapping[str, Any],
    owned: dict[str, Any],
    destination: Path,
    manifest_base: Mapping[str, Any],
    manifest_state: _ManifestState,
    *,
    modern_hashes: bool,
) -> None:
    old_owned = dict(owned)
    quarantine: _QuarantinedArtifact | None = None
    if _artifact_exists(target):
        candidate = _quarantine_artifact(target)
        quarantine = candidate
        try:
            matches = (
                _modern_quarantine_matches(candidate, entry)
                if modern_hashes
                else _legacy_live_quarantine_matches(candidate, entry)
            )
            if not matches:
                _restore_unproven_quarantine(
                    candidate,
                    f"modified owned artifact: {target}",
                )
        except BaseException:
            if _artifact_exists(candidate.path):
                try:
                    _restore_quarantined_artifact(candidate)
                except BaseException as restore_exc:
                    raise OwnershipError(
                        f"failed closed while restoring removed artifact: {target}"
                    ) from restore_exc
            raise

    new_owned = dict(owned)
    del new_owned[name]
    next_manifest = {**manifest_base, "skills": new_owned}
    delayed_write_error: BaseException | None = None
    try:
        _write_manifest(destination, next_manifest, manifest_state)
    except BaseException as exc:
        if not _manifest_state_matches(manifest_state, next_manifest):
            if quarantine is not None:
                try:
                    _restore_quarantined_artifact(quarantine)
                except BaseException as restore_exc:
                    raise OwnershipError(
                        f"failed closed while restoring removed artifact: {target}"
                    ) from restore_exc
            raise
        delayed_write_error = exc
    if quarantine is not None:
        try:
            matches = (
                _modern_quarantine_matches(quarantine, entry)
                if modern_hashes
                else _legacy_live_quarantine_matches(quarantine, entry)
            )
            if not matches:
                raise OwnershipError(
                    f"quarantined owned artifact changed before removal: {target}"
                )
            _remove_quarantined_artifact(quarantine)
        except BaseException as removal_exc:
            try:
                restorable = _artifact_exists(quarantine.path) and (
                    _modern_quarantine_matches(quarantine, entry)
                    if modern_hashes
                    else _legacy_live_quarantine_matches(quarantine, entry)
                )
            except BaseException:
                restorable = False
            if restorable:
                try:
                    _restore_quarantined_artifact(quarantine)
                    old_manifest = {**manifest_base, "skills": old_owned}
                    try:
                        _write_manifest(destination, old_manifest, manifest_state)
                    except BaseException:
                        if not _manifest_state_matches(manifest_state, old_manifest):
                            raise
                except BaseException as rollback_exc:
                    del owned[name]
                    raise OwnershipError(
                        f"failed closed while rolling back artifact removal: {target}"
                    ) from rollback_exc
                raise removal_exc
            del owned[name]
            raise

    del owned[name]

    if delayed_write_error is not None:
        raise delayed_write_error


def _remove_manifest_file(
    destination: Path,
    expected: Mapping[str, Any],
    state: _ManifestState,
) -> None:
    path = _manifest_path(destination)
    if state.identity is None or state.content is None:
        if _artifact_exists(path):
            raise OwnershipError(
                f"ownership manifest appeared during removal: {path}"
            )
        return
    quarantine: _QuarantinedArtifact | None = None
    removed = False
    try:
        quarantine = _quarantine_artifact(path)
        if not _manifest_quarantine_matches(quarantine, state):
            raise OwnershipError(f"ownership manifest changed during removal: {path}")
        try:
            actual = json.loads(quarantine.path.read_bytes().decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OwnershipError(
                f"ownership manifest changed during removal: {path}"
            ) from exc
        if actual != expected:
            raise OwnershipError(f"ownership manifest changed during removal: {path}")
        if _artifact_exists(path):
            raise OwnershipError(
                f"ownership manifest path was replaced during removal: {path}"
            )
        _require_quarantine_identity(quarantine)
        _remove_artifact(quarantine.path)
        removed = True
        state.identity = None
        state.content = None
        _cleanup_quarantine_root(quarantine)
    except BaseException:
        if quarantine is not None and not removed and _artifact_exists(quarantine.path):
            try:
                _restore_quarantined_artifact(quarantine)
            except BaseException as restore_exc:
                raise OwnershipError(
                    f"failed closed while restoring ownership manifest: {path}"
                ) from restore_exc
        raise


def sync_library(
    source: Path,
    destination: Path,
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
    dry_run: bool = False,
    prefer_symlinks: bool = True,
    allow_empty: bool = False,
) -> SyncResult:
    destination = _resolve_destination(destination)
    scan = scan_library(Path(source), overrides=overrides)
    source = scan.source
    _reject_overlapping_roots(source, destination)
    desired = _selected(scan)
    if dry_run:
        return _sync_library_transaction(
            source,
            destination,
            scan,
            desired,
            dry_run=True,
            prefer_symlinks=prefer_symlinks,
            allow_empty=allow_empty,
        )
    with _pinned_physical_directory(destination.parent) as parent_authority:
        if _resolve_destination(destination) != destination:
            raise AdapterError(
                f"destination resolved away from its pinned physical path: {destination}"
            )
        with _destination_transaction_lock(destination):
            if _resolve_destination(destination) != destination:
                raise AdapterError(
                    f"destination resolved away from its pinned physical path: {destination}"
                )
            with _pinned_physical_directory(
                destination,
                parent_authority=parent_authority,
            ):
                return _sync_library_transaction(
                    source,
                    destination,
                    scan,
                    desired,
                    dry_run=False,
                    prefer_symlinks=prefer_symlinks,
                    allow_empty=allow_empty,
                )


def _sync_library_transaction(
    source: Path,
    destination: Path,
    scan: ScanResult,
    desired: Mapping[str, Skill],
    *,
    dry_run: bool,
    prefer_symlinks: bool,
    allow_empty: bool,
) -> SyncResult:
    loaded = _load_manifest(destination)
    manifest_state = _manifest_state_from_loaded(loaded)
    manifest = loaded.data
    _validate_manifest_source(manifest, source)
    if not dry_run and manifest["skills"] and not desired and not allow_empty:
        raise AdapterError(
            "refusing to empty a managed destination implicitly; rerun sync with "
            "--allow-empty or use remove after reviewing the plan"
        )
    if loaded.modern_hashes:
        owned: dict[str, Any] = dict(manifest["skills"])
    else:
        owned = _migrate_legacy_entries(
            manifest,
            desired,
            destination,
            version=loaded.version,
        )
    manifest_base: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "source": str(source),
    }
    actions: list[Action] = []
    desired_identities = {
        name: _desired_output_identity(
            skill,
            _desired_mode(skill, prefer_symlinks),
        )
        for name, skill in desired.items()
    }

    # Finish every deterministic ownership preflight before the first write.
    for name in sorted(desired):
        target = destination / name
        entry = owned.get(name)
        if _artifact_exists(target) and entry is None:
            raise OwnershipError(f"refusing to overwrite unmanaged Claude skill: {target}")
        if entry is not None and _artifact_exists(target):
            unchanged = _owned_artifact_is_unchanged(target, entry)
            safely_relocatable = _owned_live_link_can_be_relocated(
                target,
                entry,
                desired[name],
            )
            if not unchanged and not safely_relocatable:
                raise OwnershipError(f"modified owned artifact: {target}")
    for name in sorted(set(owned) - set(desired)):
        target = destination / name
        if _artifact_exists(target) and not _owned_artifact_is_unchanged(target, owned[name]):
            raise OwnershipError(f"modified owned artifact: {target}")
    if not dry_run and loaded.needs_upgrade:
        _write_manifest(
            destination,
            {**manifest_base, "skills": owned},
            manifest_state,
        )

    for name, skill in sorted(desired.items()):
        target = destination / name
        entry = owned.get(name)
        source_changed = entry is not None and entry.get("source_hash") != skill.source_hash
        desired_output_identity = desired_identities[name]
        desired_output_changed = (
            entry is not None
            and entry.get("desired_output_identity") != desired_output_identity
        )
        live_checkpoint = (
            (source_changed or desired_output_changed)
            and _artifact_exists(target)
            and entry is not None
            and entry.get("mode") in {"symlink", "junction"}
            and skill.classification is Classification.DIRECT
            and _mode_satisfies(str(entry.get("mode")), skill, prefer_symlinks)
            and _live_target_matches_desired(entry, skill)
        )
        if live_checkpoint:
            actual_mode = str(entry["mode"])
            if not dry_run:
                if not _owned_artifact_is_unchanged(target, entry):
                    raise OwnershipError(f"modified owned artifact: {target}")
                new_entry = dict(entry)
                new_entry["source"] = skill.relative_dir.as_posix()
                new_entry["source_hash"] = skill.source_hash
                new_entry["classification"] = skill.classification.value
                new_entry["artifact_hash"] = _tree_hash(target)
                new_entry["desired_output_identity"] = desired_output_identity
                owned[name] = new_entry
                _write_manifest(
                    destination,
                    {**manifest_base, "skills": owned},
                    manifest_state,
                )
            actions.append(
                Action(
                    "checkpoint-live-source",
                    name,
                    actual_mode,
                    skill.relative_dir.as_posix(),
                    str(target),
                )
            )
            continue
        changed = (
            entry is None
            or not _artifact_exists(target)
            or source_changed
            or desired_output_changed
            or (
                entry.get("mode") in {"symlink", "junction"}
                and not _live_target_matches_desired(entry, skill)
            )
        )
        mode_changed = entry is not None and not _mode_satisfies(str(entry.get("mode")), skill, prefer_symlinks)
        if changed or mode_changed:
            operation = "install" if entry is None else "update"
            planned_mode = _desired_mode(skill, prefer_symlinks)
            if not dry_run:
                actual_mode = _install_and_commit(
                    skill,
                    destination,
                    prefer_symlinks,
                    entry,
                    owned,
                    manifest_base,
                    manifest_state,
                )
            else:
                actual_mode = planned_mode
            actions.append(Action(operation, name, actual_mode, skill.relative_dir.as_posix(), str(target)))

    for name in sorted(set(owned) - set(desired)):
        target = destination / name
        entry = owned[name]
        actions.append(Action("remove-stale", name, str(entry.get("mode", "unknown")), str(entry.get("source", "")), str(target)))
        if not dry_run:
            _remove_and_commit(
                name,
                target,
                entry,
                owned,
                destination,
                manifest_base,
                manifest_state,
                modern_hashes=True,
            )

    if not dry_run and not _artifact_exists(_manifest_path(destination)):
        _write_manifest(
            destination,
            {**manifest_base, "skills": owned},
            manifest_state,
        )
    return SyncResult(actions=tuple(actions))


def check_library(
    source: Path,
    destination: Path,
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> CheckResult:
    destination = _resolve_destination(destination)
    scan = scan_library(Path(source), overrides=overrides)
    source = scan.source
    _reject_overlapping_roots(source, destination)
    desired = _selected(scan)
    loaded = _load_manifest(destination)
    manifest = loaded.data
    _validate_manifest_source(manifest, source)
    owned = (
        dict(manifest["skills"])
        if loaded.modern_hashes
        else _migrate_legacy_entries(
            manifest,
            desired,
            destination,
            version=loaded.version,
        )
    )
    issues: list[CheckIssue] = []
    for name, skill in sorted(desired.items()):
        entry = owned.get(name)
        if entry is None:
            target = destination / name
            kind = "unmanaged-collision" if _artifact_exists(target) else "missing-output"
            issues.append(CheckIssue(kind, name, str(destination / name)))
            continue
        if entry.get("source_hash") != skill.source_hash:
            issues.append(CheckIssue("source-changed", name, skill.relative_dir.as_posix()))
        elif entry.get("desired_output_identity") != _desired_output_identity(
            skill,
            str(entry.get("mode")),
        ):
            issues.append(
                CheckIssue("desired-output-changed", name, skill.relative_dir.as_posix())
            )
        if not _artifact_exists(destination / name):
            issues.append(CheckIssue("missing-output", name, str(destination / name)))
        elif not _owned_artifact_is_unchanged(
            destination / name,
            entry,
        ) and not _owned_live_link_can_be_relocated(
            destination / name,
            entry,
            skill,
        ):
            issues.append(CheckIssue("output-modified", name, str(destination / name)))
    for name in sorted(set(owned) - set(desired)):
        issues.append(CheckIssue("stale-output", name, str(destination / name)))
    return CheckResult(tuple(issues))


def remove_library(destination: Path, *, dry_run: bool = False) -> SyncResult:
    destination = _resolve_destination(destination)
    if dry_run:
        return _remove_library_transaction(destination, dry_run=True)
    with ExitStack() as authority_stack:
        try:
            parent_authority = authority_stack.enter_context(
                _pinned_physical_directory(
                    destination.parent,
                    create_missing=False,
                )
            )
        except _PhysicalDirectoryMissing:
            return SyncResult(())
        if _resolve_destination(destination) != destination:
            raise AdapterError(
                f"destination resolved away from its pinned physical path: {destination}"
            )
        with _destination_transaction_lock(destination):
            if _resolve_destination(destination) != destination:
                raise AdapterError(
                    f"destination resolved away from its pinned physical path: {destination}"
                )
            if not _artifact_exists(destination):
                return SyncResult(())
            try:
                authority_stack.enter_context(
                    _pinned_physical_directory(
                        destination,
                        create_missing=False,
                        parent_authority=parent_authority,
                    )
                )
            except _PhysicalDirectoryMissing:
                return SyncResult(())
            return _remove_library_transaction(destination, dry_run=False)


def _remove_library_transaction(destination: Path, *, dry_run: bool) -> SyncResult:
    loaded = _load_manifest(destination)
    manifest_state = _manifest_state_from_loaded(loaded)
    manifest = loaded.data
    if manifest.get("skills"):
        _validate_manifest_source(manifest, Path(manifest["source"]).resolve())
    actions: list[Action] = []
    for name, entry in sorted(manifest["skills"].items()):
        target = destination / name
        if _artifact_exists(target):
            if loaded.modern_hashes:
                unchanged = _owned_artifact_is_unchanged(target, entry)
            else:
                mode = entry.get("mode")
                unchanged = (
                    mode in {"symlink", "junction"}
                    and _physical_link_mode(target) == mode
                    and _link_points_to(target, str(entry["target"]))
                    and Path(entry["target"]).is_dir()
                )
                if mode not in {"symlink", "junction"}:
                    raise OwnershipError(
                        "legacy manifest artifact identity is unverified for "
                        f"{name}; explicit operator reconciliation is required"
                    )
            if not unchanged:
                raise OwnershipError(f"modified owned artifact: {target}")
        actions.append(Action("remove", name, str(entry.get("mode", "unknown")), str(entry.get("source", "")), str(target)))
    if not dry_run:
        if loaded.needs_upgrade and loaded.modern_hashes:
            manifest["version"] = MANIFEST_VERSION
            _write_manifest(destination, manifest, manifest_state)
        manifest_base = {key: value for key, value in manifest.items() if key != "skills"}
        for action in actions:
            target = Path(action.destination)
            entry = manifest["skills"][action.name]
            _remove_and_commit(
                action.name,
                target,
                entry,
                manifest["skills"],
                destination,
                manifest_base,
                manifest_state,
                modern_hashes=loaded.modern_hashes,
            )
        _remove_manifest_file(destination, manifest, manifest_state)
    return SyncResult(tuple(actions))
