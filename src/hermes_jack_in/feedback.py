from __future__ import annotations

import json
import os
import stat
import unicodedata
from pathlib import Path
from typing import Any, Mapping

from .core import validate_skill_name
from .sync import (
    AdapterError,
    ProjectionIdentity,
    _identity_from_stat,
    _identity_matches,
    _pinned_physical_directory,
    _resolve_destination,
    verified_projection_identity,
)

FEEDBACK_VERSION = "feedback-v1"
PROPOSAL_VERSION = "proposal-v1"
MAX_FEEDBACK_BYTES = 32 * 1024
FEEDBACK_FIELDS = frozenset(
    {
        "version",
        "skill",
        "claimed_provenance",
        "summary",
        "observation",
        "recommendation",
    }
)
TEXT_LIMITS = {
    "claimed_provenance": 1024,
    "summary": 1024,
    "observation": 8192,
    "recommendation": 8192,
}


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _read_bounded_file(path: Path) -> bytes:
    path = Path(path).absolute()
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AdapterError(f"feedback input is not a readable regular file: {path}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    if path.is_symlink() or bool(reparse_flag and attributes & reparse_flag):
        raise AdapterError(f"feedback input is not a regular file: {path}")
    expected_identity = _identity_from_stat(metadata)
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise AdapterError(f"feedback input is not a readable regular file: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if _identity_from_stat(metadata) != expected_identity:
            raise AdapterError(f"feedback input changed before it could be read: {path}")
        if not stat.S_ISREG(metadata.st_mode):
            raise AdapterError(f"feedback input is not a regular file: {path}")
        if metadata.st_size > MAX_FEEDBACK_BYTES:
            raise AdapterError(f"feedback input exceeds {MAX_FEEDBACK_BYTES} bytes")
        chunks: list[bytes] = []
        remaining = MAX_FEEDBACK_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(8192, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        contents = b"".join(chunks)
        if len(contents) > MAX_FEEDBACK_BYTES:
            raise AdapterError(f"feedback input exceeds {MAX_FEEDBACK_BYTES} bytes")
        return contents
    finally:
        os.close(descriptor)


def _strict_text(value: Any, field: str, limit: int) -> str:
    if type(value) is not str or not value.strip():
        raise AdapterError(f"feedback field must be a non-empty string: {field}")
    if len(value) > limit:
        raise AdapterError(f"feedback field exceeds {limit} characters: {field}")
    if any(unicodedata.category(character).startswith("C") for character in value):
        raise AdapterError(f"feedback field contains a control character: {field}")
    return value


def load_feedback(path: Path) -> dict[str, str]:
    contents = _read_bounded_file(path)
    try:
        text = contents.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AdapterError("feedback input must be valid UTF-8") from exc
    try:
        payload = json.loads(text, object_pairs_hook=_unique_object)
    except AdapterError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AdapterError("feedback input must be valid JSON") from exc
    if type(payload) is not dict or set(payload) != FEEDBACK_FIELDS:
        expected = ", ".join(sorted(FEEDBACK_FIELDS))
        raise AdapterError(f"feedback input must contain exactly these fields: {expected}")
    if payload["version"] != FEEDBACK_VERSION:
        raise AdapterError(f"unsupported feedback version: {payload['version']!r}")
    try:
        validate_skill_name(payload["skill"])
    except ValueError as exc:
        raise AdapterError(f"invalid feedback skill: {payload['skill']!r}") from exc
    feedback = {
        "version": FEEDBACK_VERSION,
        "skill": payload["skill"],
    }
    feedback.update(
        {
            field: _strict_text(payload[field], field, limit)
            for field, limit in TEXT_LIMITS.items()
        }
    )
    return feedback


def _paths_overlap(first: Path, second: Path) -> bool:
    return first == second or first in second.parents or second in first.parents


def _resolve_output_path(output: Path, source: Path, destination: Path) -> Path:
    output = _resolve_destination(Path(output))
    parent = output.parent
    if not parent.is_dir():
        raise AdapterError(f"proposal output parent must already exist: {parent}")
    if _paths_overlap(output, source) or _paths_overlap(output, destination):
        raise AdapterError("proposal output must not overlap source or destination")
    return output


def _canonical_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _write_new_file(path: Path, contents: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOINHERIT", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise AdapterError(f"refusing to overwrite proposal output: {path}") from exc
    except OSError as exc:
        raise AdapterError(f"unable to create proposal output: {path}") from exc
    identity = _identity_from_stat(os.fstat(descriptor))
    try:
        if not _identity_matches(path, identity):
            raise OSError("proposal output identity changed before write")
        view = memoryview(contents)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("proposal output write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        if not _identity_matches(path, identity):
            raise OSError("proposal output identity changed during write")
    except BaseException as exc:
        try:
            os.close(descriptor)
            if _identity_matches(path, identity):
                path.unlink()
        except BaseException as cleanup_error:
            raise exc.with_traceback(exc.__traceback__) from cleanup_error
        if not isinstance(exc, Exception):
            raise
        raise AdapterError(f"proposal output write failed: {path}") from exc
    else:
        os.close(descriptor)


def _projection_payload(identity: ProjectionIdentity) -> dict[str, str]:
    return {
        "classification": identity.classification,
        "desired_output_identity": identity.desired_output_identity,
        "mode": identity.mode,
        "skill": identity.skill,
        "source": identity.source,
        "source_hash": identity.source_hash,
    }


def propose_feedback(
    source: Path,
    destination: Path,
    input_path: Path,
    output_path: Path,
    *,
    overrides: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Create one canonical review-only proposal for a current owned projection."""
    feedback = load_feedback(input_path)
    identity = verified_projection_identity(
        source,
        destination,
        feedback["skill"],
        overrides=overrides,
    )
    source_root = Path(source).resolve()
    destination_root = _resolve_destination(Path(destination))
    output = _resolve_output_path(output_path, source_root, destination_root)

    proposal: dict[str, Any] = {
        "feedback": feedback,
        "projection": _projection_payload(identity),
        "review_status": "required",
        "version": PROPOSAL_VERSION,
    }
    with _pinned_physical_directory(output.parent, create_missing=False):
        if _resolve_output_path(output, source_root, destination_root) != output:
            raise AdapterError("proposal output changed before write")
        confirmed = verified_projection_identity(
            source,
            destination,
            feedback["skill"],
            overrides=overrides,
        )
        if confirmed != identity:
            raise AdapterError("projection identity changed during feedback verification")
        _write_new_file(output, _canonical_bytes(proposal))
    return proposal
