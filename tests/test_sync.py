import ctypes
import errno
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
from pathlib import Path

import pytest

from test_core import write_skill


def _file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "manifests"
LEGACY_SKILL_BYTES = (
    b"---\nname: alpha\ndescription: Alpha.\n---\n\nUse this procedure.\n"
)
SUPPORTED_LINUX_RENAMEAT2_MACHINES = {
    "aarch64",
    "i686",
    "ppc64le",
    "s390x",
    "x86_64",
}
NATIVE_MUTATING_PLATFORM = (
    os.name == "nt"
    or sys.platform == "darwin"
    or (
        sys.platform.startswith("linux")
        and platform.machine().lower() in SUPPORTED_LINUX_RENAMEAT2_MACHINES
    )
)


def _legacy_source_hash(skill_dir: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(item for item in skill_dir.rglob("*") if item.is_file()):
        digest.update(path.relative_to(skill_dir).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _stage_legacy_copy_fixture(
    tmp_path: Path,
    fixture_name: str,
) -> tuple[Path, Path, Path]:
    from hermes_jack_in.sync import MANIFEST_NAME

    source = tmp_path / "skills"
    source_skill = source / "one" / "alpha"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_bytes(LEGACY_SKILL_BYTES)
    destination = tmp_path / "claude"
    destination_skill = destination / "alpha"
    destination_skill.mkdir(parents=True)
    (destination_skill / "SKILL.md").write_bytes(LEGACY_SKILL_BYTES)
    manifest_path = destination / MANIFEST_NAME
    payload = json.loads((FIXTURE_ROOT / fixture_name).read_text(encoding="utf-8"))
    assert payload["source"] == "<SOURCE>"
    payload["source"] = str(source.resolve())
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return source, destination, manifest_path


def _destination_bytes(destination: Path) -> dict[str, bytes]:
    return {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    }


def _directory_symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        if os.name == "nt" and getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege unavailable")
        raise


def _junction_or_skip(sync_module, link: Path, target: Path) -> None:
    try:
        sync_module._create_junction(link, target)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows junction privilege unavailable")
        raise


def _stage_modern_nested_link_case(
    tmp_path: Path,
    mode: str,
) -> tuple[Path, Path, Path, Path, bytes]:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    frontmatter = "name: alpha\ndescription: Alpha."
    if mode == "materialized":
        frontmatter += "\nversion: 1"
    source_skill = write_skill(source, "one/alpha", frontmatter)
    supporting = source_skill / "references"
    supporting.mkdir()
    (supporting / "owned.txt").write_text("owned", encoding="utf-8")
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["skills"]["alpha"]["mode"] == mode

    target = destination / "alpha"
    nested = target / "references"
    shutil.rmtree(nested)
    external = tmp_path / "external"
    external.mkdir()
    (external / "sentinel.txt").write_text("preserve external", encoding="utf-8")
    return source, destination, nested, external, manifest_path.read_bytes()


def test_dry_run_lists_exact_changes_without_writing(tmp_path: Path) -> None:
    from hermes_jack_in.sync import sync_library

    source = tmp_path / "Hermes Skills"
    destination = tmp_path / "Claude Skills"
    write_skill(source, "research/plain", "name: plain\ndescription: Plain.")

    result = sync_library(source, destination, dry_run=True)

    assert [(a.operation, a.name, a.mode) for a in result.actions] == [("install", "plain", "symlink")]
    assert not destination.exists()


def test_materialized_sync_preserves_supporting_files_and_is_idempotent(tmp_path: Path) -> None:
    from hermes_jack_in.sync import sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude" / "skills"
    skill_dir = write_skill(
        source,
        "docs/linked",
        "name: linked\ndescription: Linked.\nversion: 1",
        "See [guide](references/guide.md).\n",
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references" / "guide.md").write_text("supporting content", encoding="utf-8")

    first = sync_library(source, destination, prefer_symlinks=False)
    second = sync_library(source, destination, prefer_symlinks=False)

    assert [(a.operation, a.name) for a in first.actions] == [("install", "linked")]
    assert second.actions == ()
    assert (destination / "linked" / "references" / "guide.md").read_text(encoding="utf-8") == "supporting content"
    rendered = (destination / "linked" / "SKILL.md").read_text(encoding="utf-8")
    assert "hermes-claude-skills-adapter" in rendered
    assert "version:" not in rendered


def test_missing_source_cannot_authorize_stale_removal(tmp_path: Path) -> None:
    from hermes_jack_in.sync import AdapterError, sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude" / "skills"
    write_skill(source, "docs/linked", "name: linked\ndescription: Linked.\nversion: 1")
    sync_library(source, destination, prefer_symlinks=False)
    source.rename(tmp_path / "moved-skills")

    with pytest.raises(AdapterError, match="source root does not exist"):
        sync_library(source, destination, prefer_symlinks=False)

    assert (destination / "linked" / "SKILL.md").is_file()


@pytest.mark.parametrize("operation_name", ["plan", "sync", "check"])
def test_partial_walk_error_aborts_without_changing_managed_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_name: str,
) -> None:
    from hermes_jack_in import core
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    skill = write_skill(
        source,
        "docs/linked",
        "name: linked\ndescription: Linked.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    sentinel = destination / "external-sentinel.bin"
    sentinel.write_bytes(b"external\x00sentinel")
    before = _file_bytes(destination)
    callback_errors: list[OSError] = []

    def partial_walk(
        top: os.PathLike[str] | str,
        *,
        followlinks: bool,
        onerror,
    ):
        assert Path(top) == source.resolve()
        assert followlinks is False
        assert onerror is not None
        yield str(source), ["docs"], []
        yield str(source / "docs"), ["linked"], []
        yield str(skill), [], ["SKILL.md"]
        error = PermissionError("simulated unreadable subtree")
        callback_errors.append(error)
        onerror(error)

    monkeypatch.setattr(core.os, "walk", partial_walk)

    with pytest.raises(sync_module.AdapterError, match="source traversal failed"):
        if operation_name == "plan":
            sync_module.sync_library(
                source,
                destination,
                dry_run=True,
                prefer_symlinks=False,
            )
        elif operation_name == "sync":
            sync_module.sync_library(source, destination, prefer_symlinks=False)
        else:
            sync_module.check_library(source, destination)

    assert [str(error) for error in callback_errors] == ["simulated unreadable subtree"]
    assert _file_bytes(destination) == before


def test_sync_preserves_unowned_deterministic_scratch_sentinels(tmp_path: Path) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(
        source,
        "docs/linked",
        "name: linked\ndescription: Linked.\nversion: 1",
    )
    install_sentinel = destination / ".linked.adapter-tmp"
    install_sentinel.mkdir(parents=True)
    (install_sentinel / "sentinel.txt").write_text("external install sentinel", encoding="utf-8")
    manifest_sentinel = destination / ".hermes-claude-skills-adapter.tmp"
    manifest_sentinel.write_text("external manifest sentinel", encoding="utf-8")

    sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert (install_sentinel / "sentinel.txt").read_text(encoding="utf-8") == "external install sentinel"
    assert manifest_sentinel.read_text(encoding="utf-8") == "external manifest sentinel"


@pytest.mark.parametrize("mode", ["copy-fallback", "materialized"])
@pytest.mark.parametrize("operation", ["hash", "check", "sync", "remove"])
def test_modern_copies_reject_nested_symlinks_without_touching_external_state(
    tmp_path: Path,
    mode: str,
    operation: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source, destination, nested, external, manifest_before = (
        _stage_modern_nested_link_case(tmp_path, mode)
    )
    _directory_symlink_or_skip(nested, external)

    try:
        if operation == "hash":
            with pytest.raises(sync_module.OwnershipError, match="nested symlink"):
                sync_module._tree_hash(destination / "alpha")
        elif operation == "check":
            result = sync_module.check_library(source, destination)
            assert [(issue.kind, issue.name) for issue in result.issues] == [
                ("output-modified", "alpha")
            ]
        else:
            with pytest.raises(sync_module.OwnershipError, match="modified owned artifact"):
                if operation == "sync":
                    sync_module.sync_library(
                        source,
                        destination,
                        prefer_symlinks=False,
                    )
                else:
                    sync_module.remove_library(destination)

        assert (external / "sentinel.txt").read_text(encoding="utf-8") == (
            "preserve external"
        )
        assert nested.is_symlink()
        assert (destination / sync_module.MANIFEST_NAME).read_bytes() == manifest_before
    finally:
        if nested.is_symlink():
            nested.unlink()


@pytest.mark.skipif(os.name != "nt", reason="nested junction validation is Windows-only")
@pytest.mark.parametrize("mode", ["copy-fallback", "materialized"])
@pytest.mark.parametrize("operation", ["hash", "check", "sync", "remove"])
def test_modern_copies_reject_nested_junctions_without_touching_external_state(
    tmp_path: Path,
    mode: str,
    operation: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source, destination, nested, external, manifest_before = (
        _stage_modern_nested_link_case(tmp_path, mode)
    )
    _junction_or_skip(sync_module, nested, external)

    try:
        if operation == "hash":
            with pytest.raises(sync_module.OwnershipError, match="nested reparse point"):
                sync_module._tree_hash(destination / "alpha")
        elif operation == "check":
            result = sync_module.check_library(source, destination)
            assert [(issue.kind, issue.name) for issue in result.issues] == [
                ("output-modified", "alpha")
            ]
        else:
            with pytest.raises(sync_module.OwnershipError, match="modified owned artifact"):
                if operation == "sync":
                    sync_module.sync_library(
                        source,
                        destination,
                        prefer_symlinks=False,
                    )
                else:
                    sync_module.remove_library(destination)

        assert (external / "sentinel.txt").read_text(encoding="utf-8") == (
            "preserve external"
        )
        assert (destination / sync_module.MANIFEST_NAME).read_bytes() == manifest_before
    finally:
        if os.path.lexists(nested):
            os.rmdir(nested)


@pytest.mark.parametrize("operation", ["update", "stale", "remove"])
def test_candidate_swap_after_preflight_never_deletes_the_raced_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    write_skill(
        source,
        "two/beta",
        "name: beta\ndescription: Beta.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    target = destination / "alpha"
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()

    if operation == "update":
        (alpha / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Changed.\nversion: 2\n---\n\nChanged.\n",
            encoding="utf-8",
        )
    elif operation == "stale":
        shutil.rmtree(alpha)

    displaced = tmp_path / f"displaced-owned-{operation}"
    original_proof = sync_module._owned_artifact_is_unchanged
    raced = False

    def race_after_proof(path, entry, *args, **kwargs):
        nonlocal raced
        unchanged = original_proof(path, entry, *args, **kwargs)
        if path == target and unchanged and not raced:
            os.replace(target, displaced)
            target.mkdir()
            (target / "UNMANAGED.txt").write_text(
                f"preserve raced {operation}",
                encoding="utf-8",
            )
            raced = True
        return unchanged

    monkeypatch.setattr(
        sync_module,
        "_owned_artifact_is_unchanged",
        race_after_proof,
    )

    with pytest.raises(sync_module.OwnershipError):
        if operation == "remove":
            sync_module.remove_library(destination)
        else:
            sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert raced
    assert (target / "UNMANAGED.txt").read_text(encoding="utf-8") == (
        f"preserve raced {operation}"
    )
    assert (displaced / "SKILL.md").is_file()
    assert manifest_path.read_bytes() == manifest_before


def test_new_unmanaged_target_appearing_during_staging_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    destination.mkdir()
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    target = destination / "alpha"
    original_mkdtemp = sync_module.tempfile.mkdtemp
    raced = False

    def racing_mkdtemp(*args, **kwargs):
        nonlocal raced
        created = original_mkdtemp(*args, **kwargs)
        if str(kwargs.get("prefix", "")).startswith(".alpha.adapter-tmp-"):
            target.mkdir()
            (target / "UNMANAGED.txt").write_text(
                "preserve late arrival",
                encoding="utf-8",
            )
            raced = True
        return created

    monkeypatch.setattr(sync_module.tempfile, "mkdtemp", racing_mkdtemp)

    with pytest.raises(sync_module.OwnershipError, match="unmanaged Claude skill"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert raced
    assert (target / "UNMANAGED.txt").read_text(encoding="utf-8") == (
        "preserve late arrival"
    )
    manifest_path = destination / sync_module.MANIFEST_NAME
    assert not manifest_path.exists()


def test_staging_cleanup_preserves_a_replacement_at_the_scratch_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    original_materialize = sync_module._materialize
    replacement_path: Path | None = None

    def replace_staging(skill, target, *args, **kwargs):
        nonlocal replacement_path
        original_materialize(skill, target, *args, **kwargs)
        os.replace(target, tmp_path / "displaced-created-staging")
        target.mkdir()
        (target / "UNMANAGED.txt").write_text(
            "preserve scratch replacement",
            encoding="utf-8",
        )
        replacement_path = target
        raise OSError("simulated staging failure")

    monkeypatch.setattr(sync_module, "_materialize", replace_staging)

    with pytest.raises(sync_module.OwnershipError, match="scratch artifact"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert replacement_path is not None
    assert (replacement_path / "UNMANAGED.txt").read_text(encoding="utf-8") == (
        "preserve scratch replacement"
    )
    assert not (destination / sync_module.MANIFEST_NAME).exists()


def test_manifest_temp_cleanup_preserves_a_replacement_at_the_scratch_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    destination = tmp_path / "claude"
    destination.mkdir()
    manifest_path = destination / sync_module.MANIFEST_NAME
    displaced = tmp_path / "displaced-created-manifest-temp"
    replacement_path: Path | None = None
    original_rename_no_replace = sync_module._rename_no_replace

    def replace_manifest_temp(source, target):
        nonlocal replacement_path
        source_path = Path(source)
        if Path(target) == manifest_path:
            os.replace(source_path, displaced)
            source_path.write_text("preserve manifest scratch replacement", encoding="utf-8")
            replacement_path = source_path
            raise OSError("simulated manifest replace failure")
        return original_rename_no_replace(source, target)

    monkeypatch.setattr(sync_module, "_rename_no_replace", replace_manifest_temp)

    with pytest.raises(sync_module.OwnershipError, match="scratch artifact"):
        sync_module._write_manifest(
            destination,
            {"version": sync_module.MANIFEST_VERSION, "skills": {}},
        )

    assert replacement_path is not None
    assert replacement_path.read_text(encoding="utf-8") == (
        "preserve manifest scratch replacement"
    )
    assert displaced.is_file()
    assert not manifest_path.exists()


@pytest.mark.parametrize("operation", ["update", "stale", "remove"])
def test_manifest_write_failure_restores_the_previous_owned_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    write_skill(
        source,
        "two/beta",
        "name: beta\ndescription: Beta.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    target = destination / "alpha"
    target_before = _destination_bytes(target)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()

    if operation == "update":
        (alpha / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Changed.\nversion: 2\n---\n\nChanged.\n",
            encoding="utf-8",
        )
    elif operation == "stale":
        shutil.rmtree(alpha)

    def fail_manifest_write(*args, **kwargs):
        raise OSError("simulated manifest write failure")

    monkeypatch.setattr(sync_module, "_write_manifest", fail_manifest_write)

    with pytest.raises(OSError, match="simulated manifest write failure"):
        if operation == "remove":
            sync_module.remove_library(destination)
        else:
            sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert _destination_bytes(target) == target_before
    assert manifest_path.read_bytes() == manifest_before


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
@pytest.mark.parametrize("operation", ["stale-sync", "remove"])
def test_artifact_lstat_errors_abort_without_removing_artifact_or_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    error_number: int,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    write_skill(
        source,
        "two/beta",
        "name: beta\ndescription: Beta.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    target = destination / "alpha"
    target_before = _destination_bytes(target)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    if operation == "stale-sync":
        shutil.rmtree(alpha)

    original_lstat = Path.lstat

    def fail_target_lstat(path: Path, *args: object, **kwargs: object):
        if path == target:
            raise OSError(error_number, "injected artifact lstat failure", path)
        return original_lstat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", fail_target_lstat)
        with pytest.raises(OSError, match="injected artifact lstat failure"):
            if operation == "stale-sync":
                sync_module.sync_library(source, destination, prefer_symlinks=False)
            else:
                sync_module.remove_library(destination)

    assert _destination_bytes(target) == target_before
    assert manifest_path.read_bytes() == manifest_before


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EIO])
def test_manifest_lstat_errors_abort_remove_without_removing_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    target = destination / "alpha"
    target_before = _destination_bytes(target)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    original_lstat = Path.lstat

    def fail_manifest_lstat(path: Path, *args: object, **kwargs: object):
        if path == manifest_path:
            raise OSError(error_number, "injected manifest lstat failure", path)
        return original_lstat(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "lstat", fail_manifest_lstat)
        with pytest.raises(OSError, match="injected manifest lstat failure"):
            sync_module.remove_library(destination)

    assert _destination_bytes(target) == target_before
    assert manifest_path.read_bytes() == manifest_before


class _InjectedAbort(BaseException):
    pass


@pytest.mark.parametrize(
    ("operation", "error_kind"),
    [
        ("fresh", "oserror"),
        ("update", "ownership"),
        ("stale", "keyboard-interrupt"),
        ("remove", "base-exception"),
    ],
)
def test_post_rename_verification_errors_roll_back_every_mutation_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    error_kind: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    write_skill(
        source,
        "two/beta",
        "name: beta\ndescription: Beta.\nversion: 1",
    )
    target = destination / "alpha"
    manifest_path = destination / sync_module.MANIFEST_NAME
    target_before: dict[str, bytes] | None = None
    manifest_before: bytes | None = None
    if operation != "fresh":
        sync_module.sync_library(source, destination, prefer_symlinks=False)
        target_before = _destination_bytes(target)
        manifest_before = manifest_path.read_bytes()
    if operation == "update":
        (alpha / "SKILL.md").write_text(
            "---\nname: alpha\ndescription: Changed.\nversion: 2\n---\n\nChanged.\n",
            encoding="utf-8",
        )
    elif operation == "stale":
        shutil.rmtree(alpha)

    if error_kind == "oserror":
        injected_error: BaseException = OSError("injected post-rename verification failure")
        expected_error: type[BaseException] = OSError
    elif error_kind == "ownership":
        injected_error = sync_module.OwnershipError(
            "injected post-rename verification failure"
        )
        expected_error = sync_module.OwnershipError
    elif error_kind == "keyboard-interrupt":
        injected_error = KeyboardInterrupt("injected post-rename verification failure")
        expected_error = KeyboardInterrupt
    else:
        injected_error = _InjectedAbort("injected post-rename verification failure")
        expected_error = _InjectedAbort

    original_require_identity = sync_module._require_identity
    injected = False

    def fail_first_post_rename_verification(path, expected, message):
        nonlocal injected
        is_publication_check = (
            operation in {"fresh", "update"}
            and path == target
            and message == "installed artifact identity changed during commit"
        )
        is_quarantine_check = (
            operation in {"stale", "remove"}
            and path.name == target.name
            and path.parent.name.startswith(f".{target.name}.adapter-quarantine-")
            and message == "artifact changed while entering quarantine"
        )
        if not injected and (is_publication_check or is_quarantine_check):
            injected = True
            raise injected_error
        return original_require_identity(path, expected, message)

    monkeypatch.setattr(
        sync_module,
        "_require_identity",
        fail_first_post_rename_verification,
    )

    with pytest.raises(expected_error, match="injected post-rename verification failure"):
        if operation == "remove":
            sync_module.remove_library(destination)
        else:
            sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert injected
    if operation == "fresh":
        assert not target.exists()
        assert not manifest_path.exists()
    else:
        assert target_before is not None
        assert manifest_before is not None
        assert _destination_bytes(target) == target_before
        assert manifest_path.read_bytes() == manifest_before
    assert not list(destination.glob(".*.adapter-quarantine-*"))
    assert not list(destination.glob(".*.adapter-tmp-*"))


def test_artifact_quarantine_reconciles_a_rename_that_moves_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    target = tmp_path / "alpha"
    target.mkdir()
    sentinel = b"preserve quarantined artifact\n"
    (target / "SKILL.md").write_bytes(sentinel)
    original_rename_no_replace = sync_module._rename_no_replace
    injected = False

    def move_then_raise(source: Path, destination: Path) -> None:
        nonlocal injected
        if not injected and Path(source) == target:
            original_rename_no_replace(source, destination)
            injected = True
            raise OSError("injected post-quarantine-rename failure")
        original_rename_no_replace(source, destination)

    monkeypatch.setattr(sync_module, "_rename_no_replace", move_then_raise)

    with pytest.raises(OSError, match="injected post-quarantine-rename failure"):
        sync_module._quarantine_artifact(target)

    assert injected
    assert (target / "SKILL.md").read_bytes() == sentinel
    assert not list(tmp_path.glob(".alpha.adapter-quarantine-*"))


def test_target_publication_reconciles_a_rename_that_moves_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    target = destination / "alpha"
    target_before = _destination_bytes(target)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    (alpha / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Changed.\nversion: 2\n---\n\nChanged.\n",
        encoding="utf-8",
    )
    original_rename_no_replace = sync_module._rename_no_replace
    injected = False

    def move_then_raise(staged: Path, published: Path) -> None:
        nonlocal injected
        staged_path = Path(staged)
        if (
            not injected
            and Path(published) == target
            and staged_path.parent == destination
            and staged_path.name.startswith(".alpha.adapter-tmp-")
        ):
            original_rename_no_replace(staged, published)
            injected = True
            raise OSError("injected post-target-publication-rename failure")
        original_rename_no_replace(staged, published)

    monkeypatch.setattr(sync_module, "_rename_no_replace", move_then_raise)

    with pytest.raises(
        OSError,
        match="injected post-target-publication-rename failure",
    ):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert injected
    assert _destination_bytes(target) == target_before
    assert manifest_path.read_bytes() == manifest_before
    assert not list(destination.glob(".*.adapter-quarantine-*"))
    assert not list(destination.glob(".*.adapter-tmp-*"))


def test_manifest_publication_reconciles_a_rename_that_moves_then_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    destination = tmp_path / "claude"
    initial = {"version": sync_module.MANIFEST_VERSION, "skills": {}}
    sync_module._write_manifest(destination, initial)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    loaded = sync_module._load_manifest(destination)
    state = sync_module._manifest_state_from_loaded(loaded)
    replacement = {
        "version": sync_module.MANIFEST_VERSION,
        "source": str(tmp_path / "source"),
        "skills": {},
    }
    original_rename_no_replace = sync_module._rename_no_replace
    injected = False

    def move_then_raise(staged: Path, published: Path) -> None:
        nonlocal injected
        staged_path = Path(staged)
        if (
            not injected
            and Path(published) == manifest_path
            and staged_path.name.endswith(".tmp")
        ):
            original_rename_no_replace(staged, published)
            injected = True
            raise OSError("injected post-manifest-publication-rename failure")
        original_rename_no_replace(staged, published)

    monkeypatch.setattr(sync_module, "_rename_no_replace", move_then_raise)

    with pytest.raises(
        OSError,
        match="injected post-manifest-publication-rename failure",
    ):
        sync_module._write_manifest(destination, replacement, state)

    assert injected
    assert manifest_path.read_bytes() == manifest_before
    assert state.identity == loaded.identity
    assert state.content == loaded.content
    assert not list(destination.glob(".*.adapter-quarantine-*"))
    assert not list(destination.glob(f"{sync_module.MANIFEST_NAME}.*.tmp"))


def test_manifest_mkstemp_fstat_failure_closes_and_removes_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    destination = tmp_path / "claude"
    destination.mkdir()
    original_mkstemp = sync_module.tempfile.mkstemp
    original_fstat = sync_module.os.fstat
    allocated: dict[str, object] = {}

    def recording_mkstemp(*args, **kwargs):
        descriptor, name = original_mkstemp(*args, **kwargs)
        allocated.update(descriptor=descriptor, path=Path(name))
        return descriptor, name

    def fail_first_fstat(descriptor: int):
        if (
            descriptor == allocated.get("descriptor")
            and not allocated.get("fstat_attempted")
        ):
            allocated["fstat_attempted"] = True
            raise OSError("injected first manifest fstat failure")
        return original_fstat(descriptor)

    monkeypatch.setattr(sync_module.tempfile, "mkstemp", recording_mkstemp)
    monkeypatch.setattr(sync_module.os, "fstat", fail_first_fstat)

    with pytest.raises(OSError, match="injected first manifest fstat failure"):
        sync_module._write_manifest(
            destination,
            {"version": sync_module.MANIFEST_VERSION, "skills": {}},
        )

    descriptor = int(allocated["descriptor"])
    scratch = Path(allocated["path"])
    try:
        original_fstat(descriptor)
    except OSError as exc:
        descriptor_closed = exc.errno == errno.EBADF
    else:
        descriptor_closed = False
    scratch_leaked = scratch.exists()
    if not descriptor_closed:
        os.close(descriptor)
    if scratch_leaked:
        scratch.unlink()

    assert allocated.get("fstat_attempted") is True
    assert descriptor_closed
    assert not scratch_leaked


def test_install_mkdtemp_first_lstat_failure_removes_empty_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    original_mkdtemp = sync_module.tempfile.mkdtemp
    original_identity = sync_module._artifact_identity
    allocated: Path | None = None
    failed = False

    def recording_mkdtemp(*args, **kwargs):
        nonlocal allocated
        created = Path(original_mkdtemp(*args, **kwargs))
        if str(kwargs.get("prefix", "")).startswith(".alpha.adapter-tmp-"):
            allocated = created
        return str(created)

    def fail_first_staging_identity(path: Path):
        nonlocal failed
        if path == allocated and not failed:
            failed = True
            raise OSError("injected first staging lstat failure")
        return original_identity(path)

    monkeypatch.setattr(sync_module.tempfile, "mkdtemp", recording_mkdtemp)
    monkeypatch.setattr(sync_module, "_artifact_identity", fail_first_staging_identity)

    with pytest.raises(OSError, match="injected first staging lstat failure"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert allocated is not None
    assert failed
    scratch_leaked = allocated.exists()
    if scratch_leaked:
        allocated.rmdir()
    assert not scratch_leaked
    assert not (destination / sync_module.MANIFEST_NAME).exists()


@pytest.mark.parametrize("failure_point", ["root", "target"])
def test_quarantine_mkdtemp_initial_lstat_failure_removes_empty_scratch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    write_skill(
        source,
        "two/beta",
        "name: beta\ndescription: Beta.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    target = destination / "alpha"
    target_before = _destination_bytes(target)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    shutil.rmtree(alpha)
    original_mkdtemp = sync_module.tempfile.mkdtemp
    original_identity = sync_module._artifact_identity
    allocated: Path | None = None
    failed = False

    def recording_mkdtemp(*args, **kwargs):
        nonlocal allocated
        created = Path(original_mkdtemp(*args, **kwargs))
        if str(kwargs.get("prefix", "")).startswith(".alpha.adapter-quarantine-"):
            allocated = created
        return str(created)

    def fail_initial_quarantine_identity(path: Path):
        nonlocal failed
        should_fail = (
            not failed
            and (
                (failure_point == "root" and path == allocated)
                or (failure_point == "target" and path == target)
            )
        )
        if should_fail:
            failed = True
            raise OSError(f"injected quarantine {failure_point} lstat failure")
        return original_identity(path)

    monkeypatch.setattr(sync_module.tempfile, "mkdtemp", recording_mkdtemp)
    monkeypatch.setattr(
        sync_module,
        "_artifact_identity",
        fail_initial_quarantine_identity,
    )

    with pytest.raises(OSError, match=f"injected quarantine {failure_point} lstat failure"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert failed
    assert allocated is not None
    scratch_leaked = allocated.exists()
    if scratch_leaked:
        allocated.rmdir()
    assert not scratch_leaked
    assert _destination_bytes(target) == target_before
    assert manifest_path.read_bytes() == manifest_before


def test_manifest_publication_preserves_a_raced_sentinel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    target = destination / "alpha"
    target_before = _destination_bytes(target)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    displaced_manifest = tmp_path / "displaced-loaded-manifest"
    sentinel = b"preserve raced manifest sentinel\n"
    (alpha / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Changed.\nversion: 2\n---\n\nChanged.\n",
        encoding="utf-8",
    )
    original_require_identity = sync_module._require_identity
    raced = False

    def race_manifest_before_publication(path, expected, message):
        nonlocal raced
        if (
            not raced
            and message == "manifest scratch artifact was replaced before commit"
        ):
            os.replace(manifest_path, displaced_manifest)
            manifest_path.write_bytes(sentinel)
            raced = True
        return original_require_identity(path, expected, message)

    monkeypatch.setattr(
        sync_module,
        "_require_identity",
        race_manifest_before_publication,
    )

    with pytest.raises(sync_module.OwnershipError, match="manifest changed"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert raced
    assert manifest_path.read_bytes() == sentinel
    assert displaced_manifest.read_bytes() == manifest_before
    assert _destination_bytes(target) == target_before


def test_manifest_post_rename_verification_interrupt_restores_loaded_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    destination = tmp_path / "claude"
    initial = {"version": sync_module.MANIFEST_VERSION, "skills": {}}
    sync_module._write_manifest(destination, initial)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_before = manifest_path.read_bytes()
    loaded = sync_module._load_manifest(destination)
    state = sync_module._manifest_state_from_loaded(loaded)
    replacement = {
        "version": sync_module.MANIFEST_VERSION,
        "source": str(tmp_path / "source"),
        "skills": {},
    }
    original_require_identity = sync_module._require_identity
    injected = False

    def interrupt_published_manifest_verification(path, expected, message):
        nonlocal injected
        if (
            not injected
            and path == manifest_path
            and message == "published ownership manifest identity changed"
        ):
            injected = True
            raise KeyboardInterrupt("injected manifest post-rename verification interrupt")
        return original_require_identity(path, expected, message)

    monkeypatch.setattr(
        sync_module,
        "_require_identity",
        interrupt_published_manifest_verification,
    )

    with pytest.raises(
        KeyboardInterrupt,
        match="injected manifest post-rename verification interrupt",
    ):
        sync_module._write_manifest(destination, replacement, state)

    assert injected
    assert manifest_path.read_bytes() == manifest_before
    assert state.identity == loaded.identity
    assert state.content == loaded.content
    assert not list(destination.glob(".*.adapter-quarantine-*"))
    assert not list(destination.glob(f"{sync_module.MANIFEST_NAME}.*.tmp"))


def test_existing_transaction_lock_path_is_not_treated_as_a_stale_lock(
    tmp_path: Path,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    lock_path = sync_module._destination_lock_path(destination)
    sentinel = b"persistent advisory lock inode\n"
    lock_path.write_bytes(sentinel)

    sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert lock_path.read_bytes() == sentinel
    assert (destination / "alpha" / "SKILL.md").is_file()


def test_mutating_sync_and_remove_serialize_the_destination_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    (alpha / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Changed.\nversion: 2\n---\n\nChanged.\n",
        encoding="utf-8",
    )

    original_load_manifest = sync_module._load_manifest
    first_loaded = threading.Event()
    release_first = threading.Event()
    call_guard = threading.Lock()
    load_calls = 0
    errors: list[BaseException] = []

    def blocking_load_manifest(path: Path):
        nonlocal load_calls
        loaded = original_load_manifest(path)
        with call_guard:
            load_calls += 1
            call_number = load_calls
        if call_number == 1:
            first_loaded.set()
            if not release_first.wait(5):
                raise TimeoutError("test did not release first manifest load")
        return loaded

    def run(operation) -> None:
        try:
            operation()
        except BaseException as exc:
            errors.append(exc)

    monkeypatch.setattr(sync_module, "_load_manifest", blocking_load_manifest)
    first = threading.Thread(
        target=run,
        args=(
            lambda: sync_module.sync_library(
                source,
                destination,
                prefer_symlinks=False,
            ),
        ),
    )
    second = threading.Thread(
        target=run,
        args=(lambda: sync_module.remove_library(destination),),
    )
    first.start()
    assert first_loaded.wait(5)
    second.start()
    time.sleep(0.2)
    with call_guard:
        serialized_before_release = load_calls == 1
    release_first.set()
    first.join(10)
    second.join(10)

    assert serialized_before_release
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert load_calls == 2
    assert not (destination / "alpha").exists()
    assert not (destination / sync_module.MANIFEST_NAME).exists()


def test_destination_transaction_lock_serializes_separate_processes(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "claude"
    ready = tmp_path / "holder-ready"
    release = tmp_path / "release-holder"
    attempted = tmp_path / "contender-attempted"
    acquired = tmp_path / "contender-acquired"
    script = """
import sys
import time
from pathlib import Path
from hermes_jack_in.sync import _destination_transaction_lock

role = sys.argv[1]
destination, ready, release, attempted, acquired = map(Path, sys.argv[2:])
if role == "holder":
    with _destination_transaction_lock(destination):
        ready.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("holder release was not signaled")
            time.sleep(0.01)
else:
    attempted.write_text("attempted", encoding="utf-8")
    with _destination_transaction_lock(destination):
        acquired.write_text("acquired", encoding="utf-8")
"""
    common = [
        str(destination),
        str(ready),
        str(release),
        str(attempted),
        str(acquired),
    ]

    def wait_for(path: Path) -> None:
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"subprocess marker was not created: {path.name}")
            time.sleep(0.01)

    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", script, "holder", *common],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender = None
    try:
        wait_for(ready)
        contender = subprocess.Popen(
            [sys.executable, "-I", "-c", script, "contender", *common],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for(attempted)
        time.sleep(0.25)
        assert not acquired.exists()
        release.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        contender_stdout, contender_stderr = contender.communicate(timeout=10)
    finally:
        release.touch(exist_ok=True)
        for process in (holder, contender):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    assert holder.returncode == 0, (holder_stdout, holder_stderr)
    assert contender.returncode == 0, (contender_stdout, contender_stderr)
    assert acquired.read_text(encoding="utf-8") == "acquired"


@pytest.mark.skipif(os.name != "nt", reason="Windows public transaction lock proof")
def test_public_sync_and_remove_serialize_across_separate_processes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skills"
    destination = tmp_path / "missing-parent" / "claude"
    ready = tmp_path / "holder-ready"
    release = tmp_path / "release-holder"
    attempted = tmp_path / "contender-attempted"
    completed = tmp_path / "contender-completed"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    script = """
import sys
import time
from pathlib import Path
from hermes_jack_in import sync as sync_module

role = sys.argv[1]
source, destination, ready, release, attempted, completed = map(Path, sys.argv[2:])
if role == "holder":
    original = sync_module._sync_library_transaction
    def block_after_public_authority(*args, **kwargs):
        ready.write_text("ready", encoding="utf-8")
        deadline = time.monotonic() + 10
        while not release.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("holder release was not signaled")
            time.sleep(0.01)
        return original(*args, **kwargs)
    sync_module._sync_library_transaction = block_after_public_authority
    sync_module.sync_library(source, destination, prefer_symlinks=False)
else:
    attempted.write_text("attempted", encoding="utf-8")
    sync_module.remove_library(destination)
    completed.write_text("completed", encoding="utf-8")
"""
    common = [
        str(source),
        str(destination),
        str(ready),
        str(release),
        str(attempted),
        str(completed),
    ]

    def wait_for(path: Path) -> None:
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"subprocess marker was not created: {path.name}")
            time.sleep(0.01)

    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", script, "holder", *common],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender = None
    try:
        wait_for(ready)
        contender = subprocess.Popen(
            [sys.executable, "-I", "-c", script, "contender", *common],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for(attempted)
        time.sleep(0.25)
        assert not completed.exists()
        release.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        contender_stdout, contender_stderr = contender.communicate(timeout=10)
    finally:
        release.touch(exist_ok=True)
        for process in (holder, contender):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    assert holder.returncode == 0, (holder_stdout, holder_stderr)
    assert contender is not None
    assert contender.returncode == 0, (contender_stdout, contender_stderr)
    assert completed.read_text(encoding="utf-8") == "completed"
    assert not (destination / "alpha").exists()
    assert not (destination / ".hermes-jack-in-manifest.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows missing-ancestor contention proof")
def test_public_sync_waits_for_concurrent_missing_ancestor_establishment(
    tmp_path: Path,
) -> None:
    source = tmp_path / "skills"
    parent = tmp_path / "missing-parent"
    destination = parent / "claude"
    ready = tmp_path / "holder-ready"
    release = tmp_path / "release-holder"
    attempted = tmp_path / "contender-attempted"
    completed = tmp_path / "contender-completed"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    script = """
import sys
import time
from pathlib import Path
from hermes_jack_in import sync as sync_module

role = sys.argv[1]
source, parent, destination, ready, release, attempted, completed = map(Path, sys.argv[2:])
if role == "holder":
    original = sync_module._windows_directory_identity
    blocked = False
    def block_with_created_handle_live(handle, path):
        global blocked
        if path == parent and not blocked:
            blocked = True
            ready.write_text("ready", encoding="utf-8")
            deadline = time.monotonic() + 10
            while not release.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("holder release was not signaled")
                time.sleep(0.01)
        return original(handle, path)
    sync_module._windows_directory_identity = block_with_created_handle_live
else:
    original_sleep = time.sleep
    retry_observed = False
    def observe_retry(delay):
        global retry_observed
        if delay == sync_module._WINDOWS_AUTHORITY_CONTENTION_RETRY_SECONDS and not retry_observed:
            retry_observed = True
            attempted.write_text("attempted", encoding="utf-8")
        original_sleep(delay)
    sync_module.time.sleep = observe_retry
sync_module.sync_library(source, destination, prefer_symlinks=False)
if role == "contender":
    completed.write_text("completed", encoding="utf-8")
"""
    common = [
        str(source),
        str(parent),
        str(destination),
        str(ready),
        str(release),
        str(attempted),
        str(completed),
    ]

    def wait_for(path: Path) -> None:
        deadline = time.monotonic() + 5
        while not path.exists():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"subprocess marker was not created: {path.name}")
            time.sleep(0.01)

    holder = subprocess.Popen(
        [sys.executable, "-I", "-c", script, "holder", *common],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    contender = None
    try:
        wait_for(ready)
        contender = subprocess.Popen(
            [sys.executable, "-I", "-c", script, "contender", *common],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        wait_for(attempted)
        time.sleep(0.25)
        assert not completed.exists()
        release.write_text("release", encoding="utf-8")
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        contender_stdout, contender_stderr = contender.communicate(timeout=10)
    finally:
        release.touch(exist_ok=True)
        for process in (holder, contender):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate()

    assert holder.returncode == 0, (holder_stdout, holder_stderr)
    assert contender is not None
    assert contender.returncode == 0, (contender_stdout, contender_stderr)
    assert completed.read_text(encoding="utf-8") == "completed"
    assert (destination / "alpha" / "SKILL.md").is_file()


@pytest.mark.skipif(
    not NATIVE_MUTATING_PLATFORM,
    reason="atomic exclusive rename is unsupported on this platform",
)
def test_rename_no_replace_preserves_an_existing_regular_file(tmp_path: Path) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("staged", encoding="utf-8")
    destination.write_text("sentinel", encoding="utf-8")

    with pytest.raises(FileExistsError):
        sync_module._rename_no_replace(source, destination)

    assert source.read_text(encoding="utf-8") == "staged"
    assert destination.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.skipif(
    os.name == "nt" or not NATIVE_MUTATING_PLATFORM,
    reason="supported POSIX implementation check",
)
def test_posix_no_replace_does_not_fall_back_to_check_then_os_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_text("staged", encoding="utf-8")

    def forbidden_portable_rename(*args, **kwargs):
        raise AssertionError("POSIX no-replace must use an exclusive rename primitive")

    monkeypatch.setattr(sync_module.os, "rename", forbidden_portable_rename)
    sync_module._rename_no_replace(source, destination)

    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "staged"


class _FakeCFunction:
    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> int:
        self.calls.append(args)
        return self.result


def _ctypes_values(call: tuple[object, ...]) -> tuple[object, ...]:
    return tuple(getattr(value, "value", value) for value in call)


def _patch_posix_dispatch_platform(
    sync_module: object,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    *,
    rename: object = os.rename,
) -> None:
    fake_os = SimpleNamespace(
        name="posix",
        fsencode=os.fsencode,
        rename=rename,
        strerror=os.strerror,
    )
    monkeypatch.setattr(sync_module, "os", fake_os)
    monkeypatch.setattr(sync_module, "sys", SimpleNamespace(platform=platform_name))


def test_linux_no_replace_prefers_the_libc_renameat2_wrapper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    renameat2 = _FakeCFunction()
    syscall = _FakeCFunction()
    libc = type("FakeLibC", (), {"renameat2": renameat2, "syscall": syscall})()
    _patch_posix_dispatch_platform(sync_module, monkeypatch, "linux")
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: libc)

    sync_module._rename_no_replace(source, destination)

    assert len(renameat2.calls) == 1
    assert _ctypes_values(renameat2.calls[0]) == (
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )
    assert syscall.calls == []


@pytest.mark.parametrize(
    ("machine", "syscall_number"),
    [
        ("aarch64", 276),
        ("i686", 353),
        ("ppc64le", 357),
        ("s390x", 347),
        ("x86_64", 316),
    ],
)
def test_linux_no_replace_uses_bounded_direct_syscall_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine: str,
    syscall_number: int,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    syscall = _FakeCFunction()
    libc = type("FakeLibC", (), {"syscall": syscall})()
    _patch_posix_dispatch_platform(sync_module, monkeypatch, "linux")
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: libc)

    sync_module._rename_no_replace(source, destination)

    assert len(syscall.calls) == 1
    assert _ctypes_values(syscall.calls[0]) == (
        syscall_number,
        -100,
        os.fsencode(source),
        -100,
        os.fsencode(destination),
        1,
    )


def test_linux_no_replace_fails_closed_for_an_unmapped_syscall_architecture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    renameat2 = _FakeCFunction()
    syscall = _FakeCFunction()
    libc = type("FakeLibC", (), {"renameat2": renameat2, "syscall": syscall})()
    _patch_posix_dispatch_platform(sync_module, monkeypatch, "linux")
    monkeypatch.setattr(platform, "machine", lambda: "sparc64")
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: libc)

    with pytest.raises(OSError) as raised:
        sync_module._rename_no_replace(source, destination)

    assert raised.value.errno == errno.ENOTSUP
    assert renameat2.calls == []
    assert syscall.calls == []


def test_macos_no_replace_dispatches_to_renamex_np_with_rename_excl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    renamex_np = _FakeCFunction()
    libc = type("FakeLibC", (), {"renamex_np": renamex_np})()
    _patch_posix_dispatch_platform(sync_module, monkeypatch, "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *args, **kwargs: libc)

    sync_module._rename_no_replace(source, destination)

    assert len(renamex_np.calls) == 1
    assert _ctypes_values(renamex_np.calls[0]) == (
        os.fsencode(source),
        os.fsencode(destination),
        4,
    )


def test_other_posix_no_replace_fails_closed_without_portable_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "source"
    destination = tmp_path / "destination"

    def forbidden_portable_rename(*args: object, **kwargs: object) -> None:
        raise AssertionError("unsupported POSIX must fail closed")

    _patch_posix_dispatch_platform(
        sync_module,
        monkeypatch,
        "freebsd14",
        rename=forbidden_portable_rename,
    )

    with pytest.raises(OSError) as raised:
        sync_module._rename_no_replace(source, destination)

    assert raised.value.errno == errno.ENOTSUP


def test_override_only_change_refreshes_materialized_output_and_check_detects_drift(
    tmp_path: Path,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(
        source,
        "dev/tdd",
        "name: tdd\ndescription: Test first.",
        "Use the `terminal` tool.\n",
    )
    bash_override = {
        "tdd": {
            "classification": "semantic-adaptation",
            "replacements": [
                {
                    "from": "Use the `terminal` tool.",
                    "to": "Use Claude Code's `Bash` tool.",
                }
            ],
        }
    }
    revised_override = {
        "tdd": {
            "classification": "semantic-adaptation",
            "replacements": [
                {
                    "from": "Use the `terminal` tool.",
                    "to": "Run it with Claude Code's `Bash` tool.",
                }
            ],
        }
    }
    sync_module.sync_library(
        source,
        destination,
        overrides=bash_override,
        prefer_symlinks=False,
    )
    manifest_path = destination / sync_module.MANIFEST_NAME
    first_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    drift = sync_module.check_library(source, destination, overrides=revised_override)
    result = sync_module.sync_library(
        source,
        destination,
        overrides=revised_override,
        prefer_symlinks=False,
    )

    assert [(issue.kind, issue.name) for issue in drift.issues] == [
        ("desired-output-changed", "tdd")
    ]
    assert [(action.operation, action.name) for action in result.actions] == [("update", "tdd")]
    rendered = (destination / "tdd" / "SKILL.md").read_text(encoding="utf-8")
    assert "Run it with Claude Code's `Bash` tool." in rendered
    second_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert second_manifest["skills"]["tdd"]["source_hash"] == first_manifest["skills"]["tdd"]["source_hash"]
    assert second_manifest["skills"]["tdd"]["desired_output_identity"].startswith("v1:")
    assert sync_module.check_library(source, destination, overrides=revised_override).issues == ()


def test_renderer_only_change_refreshes_materialized_output_and_check_detects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(
        source,
        "docs/converted",
        "name: converted\ndescription: Converted.\nversion: 1",
    )
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    original_render = sync_module.render_skill

    def revised_render(skill) -> str:
        return original_render(skill) + "\n<!-- renderer revision -->\n"

    monkeypatch.setattr(sync_module, "render_skill", revised_render)

    drift = sync_module.check_library(source, destination)
    result = sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert [(issue.kind, issue.name) for issue in drift.issues] == [
        ("desired-output-changed", "converted")
    ]
    assert [(action.operation, action.name) for action in result.actions] == [
        ("update", "converted")
    ]
    assert "renderer revision" in (destination / "converted" / "SKILL.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction creation is Windows-only")
def test_create_junction_does_not_shell_parse_metacharacters(tmp_path: Path) -> None:
    from hermes_jack_in import sync as sync_module

    target = tmp_path / "source&percent%CD%literal"
    link = tmp_path / "link&percent%CD%literal"
    target.mkdir()

    sync_module._create_junction(link, target)

    try:
        assert os.path.realpath(link) == os.path.realpath(target)
    finally:
        if os.path.lexists(link):
            os.rmdir(link)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction creation is Windows-only")
@pytest.mark.parametrize("operation_name", ["sync_library", "check_library"])
def test_operations_reject_source_beneath_junction_ancestor(
    tmp_path: Path, operation_name: str
) -> None:
    from hermes_jack_in import sync as sync_module

    external = tmp_path / "external"
    write_skill(external / "library", "plain", "name: plain\ndescription: Plain.")
    alias = tmp_path / "alias"
    sync_module._create_junction(alias, external)

    operation = getattr(sync_module, operation_name)
    with pytest.raises(sync_module.AdapterError, match="source root reparse ancestor is not allowed"):
        operation(alias / "library", tmp_path / "destination")


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction creation is Windows-only")
@pytest.mark.parametrize("operation_name", ["sync_library", "check_library", "remove_library"])
@pytest.mark.parametrize("alias_kind", ["root", "ancestor"])
def test_operations_reject_unresolved_destination_junctions_without_touching_external_state(
    tmp_path: Path,
    operation_name: str,
    alias_kind: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    write_skill(source, "research/plain", "name: plain\ndescription: Plain.")
    external_parent = tmp_path / "external-parent"
    real_destination = external_parent if alias_kind == "root" else external_parent / "claude"
    sync_module.sync_library(source, real_destination, prefer_symlinks=False)
    sentinel = real_destination / "external-sentinel.txt"
    sentinel.write_text("preserve me", encoding="utf-8")

    alias = tmp_path / "destination-alias"
    sync_module._create_junction(alias, external_parent)
    unresolved_destination = alias if alias_kind == "root" else alias / "claude"
    operation = getattr(sync_module, operation_name)
    arguments = (unresolved_destination,) if operation_name == "remove_library" else (source, unresolved_destination)
    relation = "point" if alias_kind == "root" else "ancestor"

    try:
        with pytest.raises(
            sync_module.AdapterError,
            match=rf"destination root reparse {relation} is not allowed",
        ):
            operation(*arguments)
        assert sentinel.read_text(encoding="utf-8") == "preserve me"
        assert (real_destination / "plain" / "SKILL.md").is_file()
    finally:
        if os.path.lexists(alias):
            os.rmdir(alias)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction creation is Windows-only")
def test_sync_does_not_traverse_junction_inserted_during_missing_ancestor_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination_ancestor = tmp_path / "missing-a"
    destination = destination_ancestor / "missing-b" / "claude"
    external = tmp_path / "external"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    external.mkdir()
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"preserve\x00me")
    before = _file_bytes(external)
    original_mkdir = sync_module.os.mkdir
    attack_attempted = False
    attack_in_progress = False
    replacement_succeeded = False

    def replace_first_created_ancestor(path, mode=0o777, *, dir_fd=None):
        nonlocal attack_attempted, attack_in_progress, replacement_succeeded
        if dir_fd is None:
            original_mkdir(path, mode)
        else:
            original_mkdir(path, mode, dir_fd=dir_fd)
        if Path(path) == destination_ancestor and not attack_in_progress:
            attack_attempted = True
            attack_in_progress = True
            try:
                os.rmdir(destination_ancestor)
                _junction_or_skip(sync_module, destination_ancestor, external)
                replacement_succeeded = True
            finally:
                attack_in_progress = False

    monkeypatch.setattr(sync_module.os, "mkdir", replace_first_created_ancestor)

    sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert not attack_attempted
    assert not replacement_succeeded
    assert _file_bytes(external) == before
    assert sentinel.read_bytes() == b"preserve\x00me"
    assert not (external / "missing-b").exists()
    assert not (external / sync_module.MANIFEST_NAME).exists()
    assert (destination / "alpha" / "SKILL.md").is_file()


def test_remove_missing_destination_does_not_create_ancestors(tmp_path: Path) -> None:
    from hermes_jack_in import sync as sync_module

    missing_ancestor = tmp_path / "missing-a"
    destination = missing_ancestor / "missing-b" / "claude"

    result = sync_module.remove_library(destination)

    assert result.actions == ()
    assert not missing_ancestor.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_authority_walk_opens_each_component_relative_to_retained_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    destination = tmp_path / "missing-a" / "missing-b"
    original_open = sync_module._nt_open_relative_directory
    calls: list[tuple[int, str, Path, int]] = []

    def observe(parent_handle, name, path, **kwargs):
        handle, created = original_open(parent_handle, name, path, **kwargs)
        calls.append((parent_handle, name, path, handle))
        return handle, created

    monkeypatch.setattr(sync_module, "_nt_open_relative_directory", observe)

    with sync_module._pinned_physical_directory(destination):
        assert destination.is_dir()

    assert calls
    assert all("/" not in name and "\\" not in name for _, name, _, _ in calls)
    for previous, current in zip(calls, calls[1:]):
        if current[2] == previous[2]:
            assert current[0] == previous[0]
        else:
            assert current[0] == previous[3]



@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_failed_windows_authority_scope_rolls_back_exact_created_empty_ancestors(
    tmp_path: Path,
) -> None:
    from hermes_jack_in import sync as sync_module

    first_created = tmp_path / "missing-a"
    destination = first_created / "missing-b" / "claude"

    with pytest.raises(RuntimeError, match="forced authority failure"):
        with sync_module._pinned_physical_directory(destination):
            assert destination.is_dir()
            raise RuntimeError("forced authority failure")

    assert not first_created.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_authority_rollback_never_recursively_deletes_outsider_content(
    tmp_path: Path,
) -> None:
    from hermes_jack_in import sync as sync_module

    destination = tmp_path / "missing-a" / "missing-b"
    outsider = destination / "outsider.bin"

    with pytest.raises(RuntimeError, match="forced failure after outsider write"):
        with sync_module._pinned_physical_directory(destination):
            outsider.write_bytes(b"outsider\x00content")
            raise RuntimeError("forced failure after outsider write")

    assert outsider.read_bytes() == b"outsider\x00content"


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_native_created_ancestor_replacement_before_next_open_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    first_created = tmp_path / "missing-a"
    destination = first_created / "missing-b" / "claude"
    external = tmp_path / "external"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    external.mkdir()
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"preserve\x00me")
    before = _file_bytes(external)
    original_open = sync_module._nt_open_relative_directory
    attack_attempted = False
    replacement_succeeded = False

    def replace_after_native_create(parent_handle, name, path, **kwargs):
        nonlocal attack_attempted, replacement_succeeded
        handle, created = original_open(parent_handle, name, path, **kwargs)
        if path == first_created and created:
            attack_attempted = True
            try:
                os.rmdir(first_created)
                sync_module._create_junction(first_created, external)
                replacement_succeeded = True
            except OSError:
                pass
        return handle, created

    monkeypatch.setattr(
        sync_module,
        "_nt_open_relative_directory",
        replace_after_native_create,
    )

    with pytest.raises(sync_module.AdapterError, match="reparse point is not allowed"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert attack_attempted
    assert replacement_succeeded
    assert _file_bytes(external) == before
    assert sentinel.read_bytes() == b"preserve\x00me"
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_authority_scope_preserves_primary_error_if_cleanup_also_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    destination = tmp_path / "missing-a" / "missing-b"
    original_release = sync_module._release_windows_directory_chain

    def release_then_report_failure(chain, *, rollback):
        original_release(chain, rollback=rollback)
        raise sync_module.AdapterError("injected cleanup report")

    monkeypatch.setattr(
        sync_module,
        "_release_windows_directory_chain",
        release_then_report_failure,
    )

    with pytest.raises(RuntimeError, match="primary transaction failure") as raised:
        with sync_module._pinned_physical_directory(destination):
            raise RuntimeError("primary transaction failure")

    assert isinstance(raised.value.__cause__, sync_module.AdapterError)
    assert str(raised.value.__cause__) == "injected cleanup report"
    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_close_failure_is_indeterminate_and_never_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    close_handle = _FakeCFunction(result=0)
    kernel32 = SimpleNamespace(CloseHandle=close_handle)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel32)

    with pytest.raises(sync_module.AdapterError, match="handle close failed"):
        sync_module._close_windows_directory_handle(123, tmp_path / "authority")

    assert len(close_handle.calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_release_path_never_retries_an_indeterminate_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    close_calls: list[int] = []

    def fail_close(handle: int, path: Path) -> None:
        close_calls.append(handle)
        raise sync_module.AdapterError("injected indeterminate close")

    monkeypatch.setattr(sync_module, "_close_windows_directory_handle", fail_close)
    entry = sync_module._PinnedWindowsDirectory(
        tmp_path / "created",
        123,
        456,
        1,
        b"identity".ljust(16, b"\x00"),
        True,
    )

    with pytest.raises(sync_module.AdapterError, match="cleanup was incomplete"):
        sync_module._release_windows_directory_chain([entry], rollback=True)

    assert close_calls == [123]
    assert entry.handle == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_anchor_identity_failure_releases_anchor_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    close_calls: list[int] = []
    monkeypatch.setattr(sync_module, "_open_windows_directory_anchor", lambda path: 123)

    def fail_identity(handle: int, path: Path):
        raise sync_module.AdapterError("injected anchor identity failure")

    def record_close(handle: int, path: Path) -> None:
        close_calls.append(handle)

    monkeypatch.setattr(sync_module, "_windows_directory_identity", fail_identity)
    monkeypatch.setattr(sync_module, "_close_windows_directory_handle", record_close)

    with pytest.raises(sync_module.AdapterError, match="anchor identity failure"):
        with sync_module._pinned_physical_directory(tmp_path / "destination"):
            raise AssertionError("unreachable")

    assert close_calls == [123]


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_created_child_identity_failure_rolls_back_created_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    first_created = tmp_path / "missing-a"
    destination = first_created / "missing-b"
    sentinel = tmp_path / "sentinel.bin"
    sentinel.write_bytes(b"preserve\x00me")
    original_identity = sync_module._windows_directory_identity

    def fail_created_child_identity(handle: int, path: Path):
        if path == first_created:
            raise sync_module.AdapterError("injected created-child identity failure")
        return original_identity(handle, path)

    monkeypatch.setattr(
        sync_module,
        "_windows_directory_identity",
        fail_created_child_identity,
    )

    with pytest.raises(sync_module.AdapterError, match="created-child identity failure"):
        with sync_module._pinned_physical_directory(destination):
            raise AssertionError("unreachable")

    assert not first_created.exists()
    assert sentinel.read_bytes() == b"preserve\x00me"


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_created_child_identity_failure_preserves_outsider_and_closes_handle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    first_created = tmp_path / "missing-a"
    destination = first_created / "missing-b"
    outsider = first_created / "outsider.bin"
    original_identity = sync_module._windows_directory_identity

    def insert_outsider_then_fail(handle: int, path: Path):
        if path == first_created:
            outsider.write_bytes(b"outsider\x00content")
            raise sync_module.AdapterError("injected created-child identity failure")
        return original_identity(handle, path)

    monkeypatch.setattr(
        sync_module,
        "_windows_directory_identity",
        insert_outsider_then_fail,
    )

    with pytest.raises(
        sync_module.AdapterError,
        match="created-child identity failure",
    ) as raised:
        with sync_module._pinned_physical_directory(destination):
            raise AssertionError("unreachable")

    assert isinstance(raised.value.__cause__, sync_module.AdapterError)
    assert outsider.read_bytes() == b"outsider\x00content"
    moved = tmp_path / "moved-outsider"
    os.replace(first_created, moved)
    assert (moved / "outsider.bin").read_bytes() == b"outsider\x00content"


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_created_child_stabilization_rejects_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    first_created = tmp_path / "missing-a"
    destination = first_created / "missing-b"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel.bin"
    sentinel.write_bytes(b"outside\x00preserved")
    original_close = sync_module._close_windows_directory_handle
    replaced = False

    def replace_after_initial_close(handle: int, path: Path) -> None:
        nonlocal replaced
        original_close(handle, path)
        if path == first_created and not replaced:
            replaced = True
            first_created.rmdir()
            _junction_or_skip(sync_module, first_created, outside)

    monkeypatch.setattr(
        sync_module,
        "_close_windows_directory_handle",
        replace_after_initial_close,
    )

    with pytest.raises(
        sync_module.AdapterError,
        match="identity changed|reparse point is not allowed",
    ):
        with sync_module._pinned_physical_directory(destination):
            raise AssertionError("unreachable")

    assert replaced
    assert sentinel.read_bytes() == b"outside\x00preserved"
    assert first_created.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_unexpected_nt_create_carrier_closes_the_returned_handle_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class NtCreateFile:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, output_handle, *args) -> int:
            self.calls += 1
            ctypes.cast(
                output_handle,
                ctypes.POINTER(ctypes.c_void_p),
            ).contents.value = 123
            ctypes.cast(
                args[2],
                ctypes.POINTER(IoStatusBlock),
            ).contents.Information = 99
            return 0

    nt_create_file = NtCreateFile()
    close_handle = _FakeCFunction(result=1)
    ntdll = SimpleNamespace(NtCreateFile=nt_create_file)
    kernel32 = SimpleNamespace(CloseHandle=close_handle)

    def fake_library(name, *args, **kwargs):
        return ntdll if name == "ntdll" else kernel32

    monkeypatch.setattr(ctypes, "WinDLL", fake_library)

    with pytest.raises(sync_module.AdapterError, match="create result is ambiguous"):
        sync_module._nt_open_relative_directory(
            456,
            "child",
            tmp_path / "child",
            create_missing=True,
        )

    assert nt_create_file.calls == 1
    assert len(close_handle.calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_malformed_native_status_closes_returned_handle_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    class NtCreateFile:
        def __call__(self, output_handle, *args) -> bool:
            ctypes.cast(
                output_handle,
                ctypes.POINTER(ctypes.c_void_p),
            ).contents.value = 123
            return False

    close_handle = _FakeCFunction(result=1)
    ntdll = SimpleNamespace(NtCreateFile=NtCreateFile())
    kernel32 = SimpleNamespace(CloseHandle=close_handle)

    def fake_library(name, *args, **kwargs):
        return ntdll if name == "ntdll" else kernel32

    monkeypatch.setattr(ctypes, "WinDLL", fake_library)

    with pytest.raises(sync_module.AdapterError, match="native status is malformed"):
        sync_module._nt_open_relative_directory(
            456,
            "child",
            tmp_path / "child",
            create_missing=True,
        )

    assert len(close_handle.calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_native_scalar_carriers_reject_bool_status_and_identity_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    nt_create_file = _FakeCFunction(result=False)
    get_information = _FakeCFunction(result=False)
    close_handle = _FakeCFunction(result=1)
    ntdll = SimpleNamespace(NtCreateFile=nt_create_file)
    kernel32 = SimpleNamespace(
        GetFileInformationByHandle=get_information,
        CloseHandle=close_handle,
    )

    def fake_library(name, *args, **kwargs):
        return ntdll if name == "ntdll" else kernel32

    monkeypatch.setattr(ctypes, "WinDLL", fake_library)

    with pytest.raises(sync_module.AdapterError, match="native status is malformed"):
        sync_module._nt_open_relative_directory(
            456,
            "child",
            tmp_path / "child",
            create_missing=True,
        )

    class NativeIntSubclass(int):
        pass

    nt_create_file.result = NativeIntSubclass(0)
    with pytest.raises(sync_module.AdapterError, match="native status is malformed"):
        sync_module._nt_open_relative_directory(
            456,
            "child",
            tmp_path / "child",
            create_missing=True,
        )

    class IoStatusBlock(ctypes.Structure):
        _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]

    class ZeroHandleCreate:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, output_handle, *args) -> int:
            self.calls += 1
            ctypes.cast(
                args[2],
                ctypes.POINTER(IoStatusBlock),
            ).contents.Information = 2
            return 0

    zero_handle_create = ZeroHandleCreate()
    ntdll.NtCreateFile = zero_handle_create
    with pytest.raises(sync_module.AdapterError, match="create result is ambiguous"):
        sync_module._nt_open_relative_directory(
            456,
            "child",
            tmp_path / "child",
            create_missing=True,
        )
    with pytest.raises(sync_module.AdapterError, match="identity is unavailable"):
        sync_module._windows_directory_identity(123, tmp_path / "authority")

    assert len(nt_create_file.calls) == 2
    assert zero_handle_create.calls == 1
    assert len(get_information.calls) == 1
    assert close_handle.calls == []


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_zero_native_file_id_is_rejected_as_indeterminate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    class GetBasicInformation:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, handle, output) -> int:
            self.calls += 1
            ctypes.cast(
                output,
                ctypes.POINTER(ctypes.c_ulong),
            ).contents.value = stat.FILE_ATTRIBUTE_DIRECTORY
            return 1

    get_basic = GetBasicInformation()
    get_file_id = _FakeCFunction(result=1)
    kernel32 = SimpleNamespace(
        GetFileInformationByHandle=get_basic,
        GetFileInformationByHandleEx=get_file_id,
    )
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel32)

    with pytest.raises(sync_module.AdapterError, match="identity is indeterminate"):
        sync_module._windows_directory_identity(123, tmp_path / "authority")

    assert get_basic.calls == 1
    assert len(get_file_id.calls) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_authority_releases_handles_after_normal_and_error_exit(
    tmp_path: Path,
) -> None:
    from hermes_jack_in import sync as sync_module

    normal = tmp_path / "normal"
    normal.mkdir()
    with sync_module._pinned_physical_directory(normal):
        with pytest.raises(OSError):
            os.replace(normal, tmp_path / "normal-held")
    os.replace(normal, tmp_path / "normal-released")

    failed = tmp_path / "failed"
    failed.mkdir()
    with pytest.raises(RuntimeError, match="injected body failure"):
        with sync_module._pinned_physical_directory(failed):
            raise RuntimeError("injected body failure")
    os.replace(failed, tmp_path / "failed-released")


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_cross_volume_identity_rejection_rolls_back_created_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    destination = tmp_path / "created-child"
    original_identity = sync_module._windows_directory_identity

    def report_cross_volume(handle, path):
        volume, file_index, attributes = original_identity(handle, path)
        if path == destination:
            volume += 1
        return volume, file_index, attributes

    monkeypatch.setattr(sync_module, "_windows_directory_identity", report_cross_volume)

    with pytest.raises(sync_module.AdapterError, match="crosses a volume boundary"):
        with sync_module._pinned_physical_directory(destination):
            raise AssertionError("unreachable")

    assert not destination.exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction creation is Windows-only")
def test_windows_native_walk_rejects_preexisting_intermediate_junction(
    tmp_path: Path,
) -> None:
    from hermes_jack_in import sync as sync_module

    external = tmp_path / "external"
    ancestor = tmp_path / "ancestor"
    destination = ancestor / "missing" / "claude"
    external.mkdir()
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"preserve\x00me")
    before = _file_bytes(external)
    _junction_or_skip(sync_module, ancestor, external)

    try:
        with pytest.raises(sync_module.AdapterError, match="reparse point is not allowed"):
            with sync_module._pinned_physical_directory(destination):
                raise AssertionError("unreachable")
    finally:
        if os.path.lexists(ancestor):
            os.rmdir(ancestor)

    assert _file_bytes(external) == before
    assert sentinel.read_bytes() == b"preserve\x00me"
    assert not (external / "missing").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_windows_rollback_refuses_reopened_directory_with_different_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    parent = tmp_path / "parent"
    parent.mkdir()
    target = parent / "created"

    with sync_module._pinned_physical_directory(parent) as authority:
        assert authority is not None
        parent_entry = authority[-1]
        handle, created = sync_module._nt_open_relative_directory(
            parent_entry.handle,
            target.name,
            target,
            create_missing=True,
        )
        assert created
        volume, file_index, _ = sync_module._windows_directory_identity(handle, target)
        replacement = target / "replacement.bin"
        stale_entry = sync_module._PinnedWindowsDirectory(
            target,
            handle,
            parent_entry.handle,
            volume,
            file_index,
            True,
        )
        original_close = sync_module._close_windows_directory_handle
        replacement_staged = False

        def close_then_replace(closing_handle: int, path: Path) -> None:
            nonlocal replacement_staged
            original_close(closing_handle, path)
            if closing_handle == handle and not replacement_staged:
                os.rmdir(target)
                target.mkdir()
                replacement.write_bytes(b"replacement\x00content")
                replacement_staged = True

        monkeypatch.setattr(
            sync_module,
            "_close_windows_directory_handle",
            close_then_replace,
        )

        with pytest.raises(sync_module.AdapterError, match="cleanup was incomplete"):
            sync_module._release_windows_directory_chain(
                [stale_entry],
                rollback=True,
            )

    assert replacement_staged
    assert target.is_dir()
    assert replacement.read_bytes() == b"replacement\x00content"


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_failed_new_destination_recovers_through_retry_and_remove(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    first_created = tmp_path / "missing-a"
    destination = first_created / "missing-b" / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    original_transaction = sync_module._sync_library_transaction
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected post-lock transaction failure")
        return original_transaction(*args, **kwargs)

    monkeypatch.setattr(sync_module, "_sync_library_transaction", fail_once)

    with pytest.raises(RuntimeError, match="post-lock transaction failure"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert not destination.exists()
    assert first_created.is_dir()
    lock_path = sync_module._destination_lock_path(destination)
    assert lock_path.is_file()

    result = sync_module.sync_library(source, destination, prefer_symlinks=False)
    assert [action.operation for action in result.actions] == ["install"]
    assert sync_module.check_library(source, destination).issues == ()

    removed = sync_module.remove_library(destination)
    assert [action.operation for action in removed.actions] == ["remove"]
    assert not (destination / "alpha").exists()
    assert not (destination / sync_module.MANIFEST_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows retained authority is Windows-only")
def test_remove_pins_destination_parent_before_transaction_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination_parent = tmp_path / "destination-parent"
    destination = destination_parent / "claude"
    displaced_parent = tmp_path / "displaced-parent"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    original_lock = sync_module._destination_transaction_lock
    attack_attempted = False
    parent_moved = False

    @contextmanager
    def replace_parent_before_lock(locked_destination: Path):
        nonlocal attack_attempted, parent_moved
        attack_attempted = True
        try:
            os.replace(destination_parent, displaced_parent)
            parent_moved = True
        except OSError:
            pass
        with original_lock(locked_destination):
            yield

    monkeypatch.setattr(
        sync_module,
        "_destination_transaction_lock",
        replace_parent_before_lock,
    )

    sync_module.remove_library(destination)

    assert attack_attempted
    assert not parent_moved
    assert not displaced_parent.exists()
    assert not (destination / "alpha").exists()
    assert not (destination / sync_module.MANIFEST_NAME).exists()


def test_remove_does_not_mask_mid_transaction_missing_authority_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    sync_module.sync_library(source, destination, prefer_symlinks=False)

    def fail_mid_transaction(*args, **kwargs):
        raise sync_module._PhysicalDirectoryMissing("injected authority loss")

    monkeypatch.setattr(
        sync_module,
        "_remove_library_transaction",
        fail_mid_transaction,
    )

    with pytest.raises(sync_module._PhysicalDirectoryMissing, match="authority loss"):
        sync_module.remove_library(destination)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction creation is Windows-only")
def test_sync_revalidates_destination_after_acquiring_transaction_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    external = tmp_path / "external"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    external.mkdir()
    original_lock = sync_module._destination_transaction_lock

    @contextmanager
    def replace_destination_before_lock_yields(locked_destination: Path):
        assert locked_destination == destination
        with original_lock(locked_destination):
            _junction_or_skip(sync_module, destination, external)
            try:
                yield
            finally:
                if os.path.lexists(destination):
                    os.rmdir(destination)

    monkeypatch.setattr(
        sync_module,
        "_destination_transaction_lock",
        replace_destination_before_lock_yields,
    )

    with pytest.raises(sync_module.AdapterError, match="destination root reparse point"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert not (external / "alpha").exists()
    assert not (external / sync_module.MANIFEST_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction creation is Windows-only")
def test_sync_rejects_destination_replaced_after_post_lock_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    external = tmp_path / "external"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    external.mkdir()
    sentinel = external / "sentinel.bin"
    sentinel.write_bytes(b"preserve\x00me")
    before = _file_bytes(external)
    original_resolve = sync_module._resolve_destination
    resolve_calls = 0

    def resolve_then_replace(path: Path) -> Path:
        nonlocal resolve_calls
        resolved = original_resolve(path)
        resolve_calls += 1
        if resolve_calls == 3:
            _junction_or_skip(sync_module, destination, external)
        return resolved

    monkeypatch.setattr(sync_module, "_resolve_destination", resolve_then_replace)

    try:
        with pytest.raises(
            sync_module.AdapterError,
            match="destination directory reparse point",
        ):
            sync_module.sync_library(source, destination, prefer_symlinks=False)
    finally:
        if os.path.lexists(destination):
            os.rmdir(destination)

    assert resolve_calls == 3
    assert _file_bytes(external) == before
    assert sentinel.read_bytes() == b"preserve\x00me"
    assert not (external / "alpha").exists()
    assert not (external / sync_module.MANIFEST_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction creation is Windows-only")
def test_sync_pins_destination_before_first_transaction_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    external = tmp_path / "external"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    external.mkdir()
    original_load_manifest = sync_module._load_manifest
    attack_attempted = False
    replacement_succeeded = False

    def replace_destination_before_manifest_load(path: Path):
        nonlocal attack_attempted, replacement_succeeded
        attack_attempted = True
        try:
            if os.path.lexists(path):
                os.rmdir(path)
            sync_module._create_junction(path, external)
            replacement_succeeded = True
        except OSError:
            pass
        return original_load_manifest(path)

    monkeypatch.setattr(
        sync_module,
        "_load_manifest",
        replace_destination_before_manifest_load,
    )

    try:
        sync_module.sync_library(source, destination, prefer_symlinks=False)
    finally:
        if os.path.lexists(destination) and sync_module._is_windows_reparse_point(destination):
            os.rmdir(destination)

    assert attack_attempted
    assert not replacement_succeeded
    assert not (external / "alpha").exists()
    assert not (external / sync_module.MANIFEST_NAME).exists()
    assert (destination / "alpha" / "SKILL.md").is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory pinning is Windows-only")
def test_sync_keeps_destination_pinned_through_manifest_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    displaced = tmp_path / "displaced"
    external = tmp_path / "external"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    external.mkdir()
    original_write_manifest = sync_module._write_manifest
    attack_attempted = False
    replacement_succeeded = False

    def replace_destination_before_manifest_checkpoint(*args, **kwargs):
        nonlocal attack_attempted, replacement_succeeded
        if not attack_attempted:
            attack_attempted = True
            try:
                os.replace(destination, displaced)
                sync_module._create_junction(destination, external)
                replacement_succeeded = True
            except OSError:
                pass
        return original_write_manifest(*args, **kwargs)

    monkeypatch.setattr(
        sync_module,
        "_write_manifest",
        replace_destination_before_manifest_checkpoint,
    )

    try:
        sync_module.sync_library(source, destination, prefer_symlinks=False)
    finally:
        if os.path.lexists(destination) and sync_module._is_windows_reparse_point(destination):
            os.rmdir(destination)

    assert attack_attempted
    assert not replacement_succeeded
    assert not displaced.exists()
    assert not (external / sync_module.MANIFEST_NAME).exists()
    assert (destination / "alpha" / "SKILL.md").is_file()
    assert (destination / sync_module.MANIFEST_NAME).is_file()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory pinning is Windows-only")
def test_remove_pins_existing_destination_before_manifest_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    displaced = tmp_path / "displaced"
    external = tmp_path / "external"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    external.mkdir()
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    original_load_manifest = sync_module._load_manifest
    attack_attempted = False
    replacement_succeeded = False

    def replace_destination_before_manifest_load(path: Path):
        nonlocal attack_attempted, replacement_succeeded
        attack_attempted = True
        try:
            os.replace(destination, displaced)
            sync_module._create_junction(destination, external)
            replacement_succeeded = True
        except OSError:
            pass
        return original_load_manifest(path)

    monkeypatch.setattr(
        sync_module,
        "_load_manifest",
        replace_destination_before_manifest_load,
    )

    try:
        sync_module.remove_library(destination)
    finally:
        if os.path.lexists(destination) and sync_module._is_windows_reparse_point(destination):
            os.rmdir(destination)

    assert attack_attempted
    assert not replacement_succeeded
    assert not displaced.exists()
    assert not (external / sync_module.MANIFEST_NAME).exists()
    assert not (destination / "alpha").exists()
    assert not (destination / sync_module.MANIFEST_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows directory pinning is Windows-only")
def test_sync_pins_destination_parent_before_transaction_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination_parent = tmp_path / "destination-parent"
    destination = destination_parent / "claude"
    displaced_parent = tmp_path / "displaced-parent"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    original_lock = sync_module._destination_transaction_lock
    attack_attempted = False
    parent_moved = False

    @contextmanager
    def replace_parent_before_lock(locked_destination: Path):
        nonlocal attack_attempted, parent_moved
        attack_attempted = True
        try:
            os.replace(destination_parent, displaced_parent)
            parent_moved = True
        except OSError:
            pass
        with original_lock(locked_destination):
            yield

    monkeypatch.setattr(
        sync_module,
        "_destination_transaction_lock",
        replace_parent_before_lock,
    )

    try:
        sync_module.sync_library(source, destination, prefer_symlinks=False)
    finally:
        if displaced_parent.exists() and not destination_parent.exists():
            os.replace(displaced_parent, destination_parent)

    assert attack_attempted
    assert not parent_moved
    assert not displaced_parent.exists()
    assert (destination / "alpha" / "SKILL.md").is_file()
    assert (destination / sync_module.MANIFEST_NAME).is_file()


@pytest.mark.parametrize("operation_name", ["sync", "check", "remove"])
@pytest.mark.parametrize("alias_kind", ["root", "ancestor"])
def test_operations_reject_destination_symlinks_without_changing_external_state(
    tmp_path: Path,
    operation_name: str,
    alias_kind: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    write_skill(source, "research/plain", "name: plain\ndescription: Plain.")
    external_parent = tmp_path / "external-parent"
    real_destination = external_parent / "claude"
    sync_module.sync_library(source, real_destination, prefer_symlinks=False)
    sentinel = real_destination / "external-sentinel.bin"
    sentinel.write_bytes(b"preserve\x00me")
    before = _file_bytes(real_destination)

    alias = tmp_path / "destination-alias"
    alias_target = real_destination if alias_kind == "root" else external_parent
    _directory_symlink_or_skip(alias, alias_target)
    assert alias.is_symlink()
    destination = alias if alias_kind == "root" else alias / "claude"
    relation = "root symlink" if alias_kind == "root" else "root symlink ancestor"

    try:
        with pytest.raises(sync_module.AdapterError, match=relation):
            if operation_name == "sync":
                sync_module.sync_library(
                    source,
                    destination,
                    prefer_symlinks=False,
                )
            elif operation_name == "check":
                sync_module.check_library(source, destination)
            else:
                sync_module.remove_library(destination, dry_run=True)

        assert _file_bytes(real_destination) == before
        assert sentinel.read_bytes() == b"preserve\x00me"
        assert (real_destination / "plain" / "SKILL.md").is_file()
    finally:
        if os.path.lexists(alias):
            alias.unlink()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction fallback is Windows-only")
def test_symlink_failure_falls_back_to_owned_live_junction(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(source, "research/plain", "name: plain\ndescription: Plain.")

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(22, "privilege not held", None, 1314)

    monkeypatch.setattr(sync_module.Path, "symlink_to", denied)
    result = sync_module.sync_library(source, destination)

    target = destination / "plain"
    assert result.actions[0].mode == "junction"
    assert target.is_dir()
    assert os.path.realpath(target) == os.path.realpath(source_skill)
    manifest = json.loads((destination / ".hermes-claude-skills-adapter.json").read_text(encoding="utf-8"))
    assert manifest["skills"]["plain"]["mode"] == "junction"

    (source_skill / "live.txt").write_text("live", encoding="utf-8")
    assert (target / "live.txt").read_text(encoding="utf-8") == "live"
    (source_skill / "live.txt").unlink()

    second = sync_module.sync_library(source, destination)
    assert second.actions == ()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction fallback is Windows-only")
def test_default_sync_migrates_managed_copy_to_live_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(source, "research/plain", "name: plain\ndescription: Plain.")
    sync_module.sync_library(source, destination, prefer_symlinks=False)

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(1314, "privilege not held")

    monkeypatch.setattr(sync_module.Path, "symlink_to", denied)
    result = sync_module.sync_library(source, destination)

    assert [(action.operation, action.mode) for action in result.actions] == [("update", "junction")]
    assert os.path.realpath(destination / "plain") == os.path.realpath(source_skill)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction fallback is Windows-only")
def test_live_source_change_checkpoints_without_relinking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(source, "research/plain", "name: plain\ndescription: Plain.")

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(1314, "privilege not held")

    monkeypatch.setattr(sync_module.Path, "symlink_to", denied)
    sync_module.sync_library(source, destination)
    (source_skill / "SKILL.md").write_text(
        "---\nname: plain\ndescription: Changed.\n---\n\nChanged body.\n",
        encoding="utf-8",
    )

    def must_not_reinstall(*args: object, **kwargs: object) -> None:
        raise AssertionError("live source changes must not recreate the link")

    monkeypatch.setattr(sync_module, "_install", must_not_reinstall)
    result = sync_module.sync_library(source, destination)

    assert [(action.operation, action.mode) for action in result.actions] == [
        ("checkpoint-live-source", "junction")
    ]
    assert "Changed body" in (destination / "plain" / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("old_target_still_exists", [False, True])
def test_live_skill_relocation_relinks_after_validating_the_owned_target(
    tmp_path: Path,
    old_target_still_exists: bool,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    original_source = write_skill(
        source,
        "research/plain",
        "name: plain\ndescription: Plain.",
    )
    sync_module.sync_library(source, destination)
    relocated_source = source / "moved" / "plain"
    relocated_source.parent.mkdir()
    original_source.rename(relocated_source)
    if old_target_still_exists:
        original_source.mkdir()

    drift = sync_module.check_library(source, destination)

    result = sync_module.sync_library(source, destination)

    assert [(issue.kind, issue.name) for issue in drift.issues] == [
        ("desired-output-changed", "plain")
    ]
    assert [(action.operation, action.name) for action in result.actions] == [
        ("update", "plain")
    ]
    target = destination / "plain"
    assert target.resolve(strict=True) == relocated_source.resolve(strict=True)
    manifest = json.loads(
        (destination / sync_module.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    assert Path(manifest["skills"]["plain"]["target"]).resolve(strict=True) == (
        relocated_source.resolve(strict=True)
    )
    assert manifest["skills"]["plain"]["source"] == "moved/plain"
    assert sync_module.check_library(source, destination).issues == ()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction fallback is Windows-only")
def test_missing_live_link_is_reinstalled_when_source_also_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(source, "research/plain", "name: plain\ndescription: Plain.")

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(1314, "privilege not held")

    monkeypatch.setattr(sync_module.Path, "symlink_to", denied)
    sync_module.sync_library(source, destination)
    os.rmdir(destination / "plain")
    (source_skill / "SKILL.md").write_text(
        "---\nname: plain\ndescription: Changed.\n---\n\nChanged body.\n",
        encoding="utf-8",
    )

    result = sync_module.sync_library(source, destination)

    assert [(action.operation, action.mode) for action in result.actions] == [("update", "junction")]
    assert (destination / "plain" / "SKILL.md").is_file()
    assert sync_module.check_library(source, destination).issues == ()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction fallback is Windows-only")
def test_remove_owned_junction_never_deletes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(source, "research/plain", "name: plain\ndescription: Plain.")

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(1314, "privilege not held")

    monkeypatch.setattr(sync_module.Path, "symlink_to", denied)
    sync_module.sync_library(source, destination)

    result = sync_module.remove_library(destination)

    assert [(action.operation, action.mode) for action in result.actions] == [("remove", "junction")]
    assert not (destination / "plain").exists()
    assert (source_skill / "SKILL.md").is_file()


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction fallback is Windows-only")
def test_linked_skill_that_becomes_incompatible_is_removed_without_source_damage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(source, "research/plain", "name: plain\ndescription: Plain.")

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(22, "privilege not held", None, 1314)

    monkeypatch.setattr(sync_module.Path, "symlink_to", denied)
    sync_module.sync_library(source, destination)
    skill_file = source_skill / "SKILL.md"
    skill_file.write_text(skill_file.read_text(encoding="utf-8") + "\nUse `terminal`.\n", encoding="utf-8")

    with pytest.raises(sync_module.AdapterError, match="--allow-empty"):
        sync_module.sync_library(source, destination)
    assert os.path.lexists(destination / "plain")
    assert skill_file.is_file()

    result = sync_module.sync_library(source, destination, allow_empty=True)

    assert [(action.operation, action.mode) for action in result.actions] == [
        ("remove-stale", "junction")
    ]
    assert not os.path.lexists(destination / "plain")
    assert skill_file.is_file()
    assert "Use `terminal`." in skill_file.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction fallback is Windows-only")
def test_remove_broken_owned_junction_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(source, "research/plain", "name: plain\ndescription: Plain.")

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(1314, "privilege not held")

    monkeypatch.setattr(sync_module.Path, "symlink_to", denied)
    sync_module.sync_library(source, destination)
    target = destination / "plain"
    shutil.rmtree(source_skill)

    try:
        with pytest.raises(sync_module.OwnershipError, match="modified owned artifact"):
            sync_module.remove_library(destination)
        assert os.path.lexists(target)
    finally:
        if os.path.lexists(target):
            os.rmdir(target)


def test_sync_refuses_to_overwrite_unmanaged_skill(tmp_path: Path) -> None:
    from hermes_jack_in.sync import OwnershipError, sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "research/plain", "name: plain\ndescription: Plain.")
    write_skill(destination, "plain", "name: plain\ndescription: Existing user skill.")

    with pytest.raises(OwnershipError, match="unmanaged Claude skill"):
        sync_library(source, destination)

    assert "Existing user skill" in (destination / "plain" / "SKILL.md").read_text(encoding="utf-8")


def test_sync_preflights_all_collisions_before_writing_any_artifact(tmp_path: Path) -> None:
    from hermes_jack_in.sync import OwnershipError, sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    write_skill(source, "two/beta", "name: beta\ndescription: Beta.")
    write_skill(destination, "beta", "name: beta\ndescription: Existing.")

    with pytest.raises(OwnershipError, match="unmanaged Claude skill"):
        sync_library(source, destination)

    assert not (destination / "alpha").exists()


def test_mid_sync_runtime_failure_keeps_completed_artifact_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.\nversion: 1")
    write_skill(source, "two/beta", "name: beta\ndescription: Beta.\nversion: 1")
    original = sync_module._install

    def fail_beta(skill, target, prefer_symlinks):
        if skill.name == "beta":
            raise OSError("simulated disk failure")
        return original(skill, target, prefer_symlinks)

    monkeypatch.setattr(sync_module, "_install", fail_beta)
    with pytest.raises(OSError, match="simulated disk failure"):
        sync_module.sync_library(source, destination, prefer_symlinks=False)

    manifest = json.loads((destination / sync_module.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest["skills"]) == {"alpha"}
    assert (destination / "alpha" / "SKILL.md").exists()


def test_source_and_destination_must_not_overlap(tmp_path: Path) -> None:
    from hermes_jack_in.sync import AdapterError, sync_library

    source = tmp_path / "skills"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")

    with pytest.raises(AdapterError, match="must not overlap"):
        sync_library(source, source / ".claude" / "skills")


def test_malformed_manifest_skill_key_is_rejected_before_path_use(tmp_path: Path) -> None:
    from hermes_jack_in.sync import MANIFEST_NAME, OwnershipError, remove_library

    destination = tmp_path / "claude"
    destination.mkdir()
    (destination / MANIFEST_NAME).write_text(
        json.dumps({"version": 1, "skills": {"../outside": {"mode": "materialized"}}}),
        encoding="utf-8",
    )

    with pytest.raises(OwnershipError, match="invalid manifest skill name"):
        remove_library(destination)


@pytest.mark.parametrize(
    "fixture_name",
    ["schema-v1-copy.json", "schema-v2-copy.json"],
)
def test_genuine_legacy_manifest_is_migrated_only_after_exact_artifact_proof(
    tmp_path: Path,
    fixture_name: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source, destination, manifest_path = _stage_legacy_copy_fixture(
        tmp_path,
        fixture_name,
    )

    result = sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert result.actions == ()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 3
    assert manifest["skills"]["alpha"]["source_hash"] != (
        "5db74e16752f477798ef4f46690c92d18007a8a92ab8fcb61a4b0552b0a074ed"
    )
    assert manifest["skills"]["alpha"]["artifact_hash"] != (
        "9d5447671a0b186aa3522c5c95f3d57570341f3e2fdb1bc676c83f62399e3caf"
    )
    assert manifest["skills"]["alpha"]["desired_output_identity"].startswith("v1:")
    assert sync_module.check_library(source, destination).issues == ()


def test_mislabeled_modern_v2_manifest_remains_readable_and_rewrites_as_v3(
    tmp_path: Path,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 2
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = sync_module.sync_library(source, destination, prefer_symlinks=False)

    assert result.actions == ()
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["version"] == 3


@pytest.mark.parametrize("operation", ["overwrite", "remove"])
def test_legacy_hash_collision_mutation_cannot_authorize_destructive_action(
    tmp_path: Path,
    operation: str,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    source_skill = source / "one" / "alpha"
    source_skill.mkdir(parents=True)
    (source_skill / "SKILL.md").write_bytes(LEGACY_SKILL_BYTES)
    (source_skill / "a").write_bytes(b"")
    (source_skill / "b").write_bytes(b"payload")
    destination = tmp_path / "claude"
    destination_skill = destination / "alpha"
    shutil.copytree(source_skill, destination_skill)
    original_legacy_hash = sync_module._legacy_tree_hash(destination_skill)
    (destination_skill / "a").write_bytes(b"F\0b\0payload")
    (destination_skill / "b").unlink()
    assert sync_module._legacy_tree_hash(destination_skill) == original_legacy_hash
    payload = {
        "version": 2,
        "source": str(source.resolve()),
        "skills": {
            "alpha": {
                "source": "one/alpha",
                "source_hash": _legacy_source_hash(source_skill),
                "classification": "directly-portable",
                "mode": "copy-fallback",
                "artifact_hash": original_legacy_hash,
            }
        },
    }
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    before = _destination_bytes(destination)

    with pytest.raises(
        sync_module.OwnershipError,
        match="legacy manifest.*operator reconciliation",
    ):
        if operation == "overwrite":
            sync_module.sync_library(source, destination, prefer_symlinks=False)
        else:
            sync_module.remove_library(destination)

    assert _destination_bytes(destination) == before


def test_legacy_migration_is_complete_before_a_partial_sync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    entries = {}
    for category, name in (("one", "alpha"), ("two", "beta")):
        skill_dir = source / category / name
        skill_dir.mkdir(parents=True)
        skill_bytes = LEGACY_SKILL_BYTES.replace(b"alpha", name.encode("ascii"), 1).replace(
            b"Alpha", name.title().encode("ascii"), 1
        )
        (skill_dir / "SKILL.md").write_bytes(skill_bytes)
        destination_skill = destination / name
        shutil.copytree(skill_dir, destination_skill)
        entries[name] = {
            "source": f"{category}/{name}",
            "source_hash": _legacy_source_hash(skill_dir),
            "classification": "directly-portable",
            "mode": "copy-fallback",
            "artifact_hash": sync_module._legacy_tree_hash(destination_skill),
        }
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest_path.write_text(
        json.dumps(
            {"version": 2, "source": str(source.resolve()), "skills": entries},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    original_install = sync_module._install

    def fail_beta(skill, target, prefer_symlinks):
        if skill.name == "beta":
            raise OSError("simulated migration follow-up failure")
        return original_install(skill, target, prefer_symlinks)

    monkeypatch.setattr(sync_module, "_install", fail_beta)
    with pytest.raises(OSError, match="simulated migration follow-up failure"):
        sync_module.sync_library(source, destination)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["version"] == 3
    assert set(manifest["skills"]) == {"alpha", "beta"}
    assert all(
        entry["desired_output_identity"].startswith("v1:")
        for entry in manifest["skills"].values()
    )


def test_legacy_live_manifest_migrates_after_canonical_source_bytes_change(
    tmp_path: Path,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(
        source,
        "one/alpha",
        "name: alpha\ndescription: Alpha.",
        "Original body.\n",
    )
    sync_module.sync_library(source, destination)
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 2
    del manifest["skills"]["alpha"]["desired_output_identity"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (source_skill / "SKILL.md").write_text(
        "---\nname: alpha\ndescription: Changed.\n---\n\nChanged body.\n",
        encoding="utf-8",
    )

    result = sync_module.sync_library(source, destination)

    assert result.actions == ()
    migrated = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert migrated["version"] == 3
    assert "desired_output_identity" in migrated["skills"]["alpha"]
    assert sync_module.check_library(source, destination).issues == ()


def test_manifest_v1_cannot_claim_junction_ownership(tmp_path: Path) -> None:
    from hermes_jack_in.sync import MANIFEST_NAME, OwnershipError, remove_library

    destination = tmp_path / "claude"
    destination.mkdir()
    payload = {
        "version": 1,
        "source": "C:/source",
        "skills": {
            "alpha": {
                "source": "one/alpha",
                "source_hash": "0" * 64,
                "classification": "directly-portable",
                "mode": "junction",
                "artifact_hash": "0" * 64,
                "target": "C:/source/one/alpha",
            }
        },
    }
    (destination / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(OwnershipError, match="invalid manifest entry"):
        remove_library(destination)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction validation is Windows-only")
def test_manifest_v1_symlink_mode_cannot_claim_a_physical_junction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    source_skill = write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")

    def denied(*args: object, **kwargs: object) -> None:
        raise OSError(1314, "privilege not held")

    monkeypatch.setattr(sync_module.Path, "symlink_to", denied)
    sync_module.sync_library(source, destination)
    target = destination / "alpha"
    manifest_path = destination / sync_module.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["version"] = 1
    manifest["skills"]["alpha"]["mode"] = "symlink"
    del manifest["skills"]["alpha"]["desired_output_identity"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    try:
        with pytest.raises(sync_module.OwnershipError, match="modified owned artifact"):
            sync_module.remove_library(destination)
        assert os.path.lexists(target)
        assert (source_skill / "SKILL.md").is_file()
    finally:
        if os.path.lexists(target):
            os.rmdir(target)


@pytest.mark.parametrize(
    "payload, message",
    [
        ([], "manifest root must be a mapping"),
        ({"version": 1, "skills": {"alpha": "bad"}}, "invalid manifest entry"),
        (
            {
                "version": 1,
                "source": "C:/source",
                "skills": {
                    "alpha": {
                        "source": "../escape",
                        "source_hash": "bad",
                        "classification": "directly-portable",
                        "mode": "materialized",
                        "artifact_hash": "bad",
                    }
                },
            },
            "invalid manifest entry",
        ),
    ],
)
def test_malformed_manifest_schema_is_rejected(tmp_path: Path, payload: object, message: str) -> None:
    from hermes_jack_in.sync import MANIFEST_NAME, OwnershipError, remove_library

    destination = tmp_path / "claude"
    destination.mkdir()
    (destination / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(OwnershipError, match=message):
        remove_library(destination)


@pytest.mark.parametrize("source_value", [None, 123, ["C:/source"]])
def test_nonempty_manifest_requires_string_top_level_source(tmp_path: Path, source_value: object) -> None:
    from hermes_jack_in.sync import MANIFEST_NAME, OwnershipError, remove_library

    destination = tmp_path / "claude"
    destination.mkdir()
    payload = {
        "version": 1,
        "skills": {
            "alpha": {
                "source": "one/alpha",
                "source_hash": "0" * 64,
                "classification": "metadata-path-conversion",
                "mode": "materialized",
                "artifact_hash": "0" * 64,
            }
        },
    }
    if source_value is not None:
        payload["source"] = source_value
    (destination / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(OwnershipError, match="missing a valid source"):
        remove_library(destination)


def test_manifest_entry_rejects_windows_drive_or_backslash_source(tmp_path: Path) -> None:
    from hermes_jack_in.sync import MANIFEST_NAME, OwnershipError, remove_library

    destination = tmp_path / "claude"
    destination.mkdir()
    payload = {
        "version": 1,
        "source": "C:/source",
        "skills": {
            "alpha": {
                "source": "C:\\outside",
                "source_hash": "0" * 64,
                "classification": "metadata-path-conversion",
                "mode": "materialized",
                "artifact_hash": "0" * 64,
            }
        },
    }
    (destination / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(OwnershipError, match="invalid manifest entry"):
        remove_library(destination)


def test_changed_and_removed_sources_update_and_remove_only_owned_artifacts(tmp_path: Path) -> None:
    from hermes_jack_in.sync import check_library, sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    alpha = write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.\nversion: 1")
    beta = write_skill(source, "two/beta", "name: beta\ndescription: Beta.\nversion: 1")
    sync_library(source, destination, prefer_symlinks=False)

    (alpha / "SKILL.md").write_text("---\nname: alpha\ndescription: Alpha changed.\nversion: 2\n---\n\nChanged.\n", encoding="utf-8")
    for child in beta.iterdir():
        child.unlink()
    beta.rmdir()

    drift = check_library(source, destination)
    assert {issue.kind for issue in drift.issues} == {"source-changed", "stale-output"}

    result = sync_library(source, destination, prefer_symlinks=False)
    assert {(a.operation, a.name) for a in result.actions} == {("update", "alpha"), ("remove-stale", "beta")}
    assert not (destination / "beta").exists()
    assert "Alpha changed" in (destination / "alpha" / "SKILL.md").read_text(encoding="utf-8")


def test_modified_owned_output_is_preserved_fail_closed(tmp_path: Path) -> None:
    from hermes_jack_in.sync import OwnershipError, remove_library, sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.\nversion: 1")
    sync_library(source, destination, prefer_symlinks=False)
    output = destination / "alpha" / "SKILL.md"
    output.write_text(output.read_text(encoding="utf-8") + "user edit\n", encoding="utf-8")

    with pytest.raises(OwnershipError, match="modified owned artifact"):
        remove_library(destination)
    assert output.exists()


def test_empty_directory_output_mutation_is_detected(tmp_path: Path) -> None:
    from hermes_jack_in.sync import OwnershipError, remove_library, sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.\nversion: 1")
    sync_library(source, destination, prefer_symlinks=False)
    (destination / "alpha" / "unexpected-empty-directory").mkdir()

    with pytest.raises(OwnershipError, match="modified owned artifact"):
        remove_library(destination)


def test_tree_hash_length_frames_records_that_collided_under_legacy_serialization(
    tmp_path: Path,
) -> None:
    from hermes_jack_in.sync import _tree_hash

    multiple_records = tmp_path / "multiple-records"
    multiple_records.mkdir()
    (multiple_records / "a").write_bytes(b"")
    (multiple_records / "b").write_bytes(b"payload")
    embedded_record = tmp_path / "embedded-record"
    embedded_record.mkdir()
    (embedded_record / "a").write_bytes(b"F\0b\0payload")

    assert _tree_hash(multiple_records) != _tree_hash(embedded_record)


def test_looping_link_target_fails_closed_in_ownership_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hermes_jack_in import sync as sync_module

    target = tmp_path / "loop"
    target.mkdir()
    entry = {
        "mode": "junction",
        "target": str(tmp_path / "expected"),
        "artifact_hash": "0" * 64,
    }
    original_resolve = sync_module.Path.resolve

    def looping_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if path == target:
            raise RuntimeError("Symlink loop")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(sync_module, "_is_live_link", lambda path: True)
    monkeypatch.setattr(sync_module.Path, "resolve", looping_resolve)
    assert not sync_module._owned_artifact_is_unchanged(target, entry)


@pytest.mark.skipif(os.name != "nt", reason="NTFS junction fallback is Windows-only")
def test_dangling_unmanaged_junction_is_a_collision(tmp_path: Path) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    external = tmp_path / "external"
    external.mkdir()
    destination.mkdir()
    target = destination / "alpha"
    sync_module._create_junction(target, external)
    external.rmdir()

    try:
        with pytest.raises(sync_module.OwnershipError, match="unmanaged Claude skill"):
            sync_module.sync_library(source, destination)
    finally:
        if os.path.lexists(target):
            os.rmdir(target)


def test_check_reports_dangling_unmanaged_symlink_as_collision(tmp_path: Path) -> None:
    from hermes_jack_in.sync import check_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.")
    destination.mkdir()
    _directory_symlink_or_skip(destination / "alpha", tmp_path / "missing-target")

    result = check_library(source, destination)

    assert [(issue.kind, issue.name) for issue in result.issues] == [("unmanaged-collision", "alpha")]


def test_remove_rolls_back_owned_artifacts_and_preserves_unrelated_skills(tmp_path: Path) -> None:
    from hermes_jack_in.sync import remove_library, sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.\nversion: 1")
    write_skill(destination, "personal", "name: personal\ndescription: Mine.")
    sync_library(source, destination, prefer_symlinks=False)

    result = remove_library(destination)

    assert [(a.operation, a.name) for a in result.actions] == [("remove", "alpha")]
    assert (destination / "personal" / "SKILL.md").exists()
    assert not (destination / ".hermes-claude-skills-adapter.json").exists()


def test_missing_owned_output_is_recreated_from_canonical_source(tmp_path: Path) -> None:
    from hermes_jack_in.sync import sync_library

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.\nversion: 1")
    sync_library(source, destination, prefer_symlinks=False)
    shutil.rmtree(destination / "alpha")

    result = sync_library(source, destination, prefer_symlinks=False)

    assert [(action.operation, action.name) for action in result.actions] == [("update", "alpha")]
    assert (destination / "alpha" / "SKILL.md").exists()


def test_destination_cannot_be_repointed_to_a_different_source_library(tmp_path: Path) -> None:
    from hermes_jack_in.sync import OwnershipError, sync_library

    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "claude"
    write_skill(first, "one/alpha", "name: alpha\ndescription: Alpha.\nversion: 1")
    write_skill(second, "one/beta", "name: beta\ndescription: Beta.\nversion: 1")
    sync_library(first, destination, prefer_symlinks=False)

    with pytest.raises(OwnershipError, match="belongs to a different source"):
        sync_library(second, destination, prefer_symlinks=False)


def test_partial_remove_updates_manifest_for_retry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hermes_jack_in import sync as sync_module

    source = tmp_path / "skills"
    destination = tmp_path / "claude"
    write_skill(source, "one/alpha", "name: alpha\ndescription: Alpha.\nversion: 1")
    write_skill(source, "two/beta", "name: beta\ndescription: Beta.\nversion: 1")
    sync_module.sync_library(source, destination, prefer_symlinks=False)
    original = sync_module.shutil.rmtree

    def fail_beta(path):
        if Path(path).name == "beta":
            raise OSError("simulated open handle")
        return original(path)

    monkeypatch.setattr(sync_module.shutil, "rmtree", fail_beta)
    with pytest.raises(OSError, match="simulated open handle"):
        sync_module.remove_library(destination)

    manifest = json.loads((destination / sync_module.MANIFEST_NAME).read_text(encoding="utf-8"))
    assert set(manifest["skills"]) == {"beta"}
    assert not (destination / "alpha").exists()
    assert (destination / "beta" / "SKILL.md").is_file()
