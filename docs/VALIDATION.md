# Validation

This document separates implemented support, local execution, native CI, and
runtime interoperability evidence. Unit tests or emulated system calls are not
reported as native platform qualification.

## Release policy

A version is releasable only when:

1. the exact committed public root passes `scripts/release_gate.py` from a clean
   `git archive`;
2. the GitHub Actions matrix passes natively on Windows, Linux, and macOS for
   every declared Python minor;
3. wheel and sdist build twice to identical bytes under a fixed source epoch,
   match exact recursive allowlists, and pass isolated installed-artifact and
   installed-guard behavior;
4. a real Hermes library scan and non-mutating destination plan have no issues;
5. an isolated Claude Code canary records its exact Claude version; and
6. public-history privacy, secret, license, metadata, and documentation audits
   have no blockers.

## 0.1.0 prerelease evidence — 2026-08-19

Version `0.1.0` is prepared as a source-only GitHub prerelease. No package is
published to PyPI and no wheel or sdist is attached to the release. The release
body is the authoritative dynamic evidence record: it must identify the exact
tag target, local gate result, main and tag CI URLs with every declared native
matrix job passing, signed-tag verification, and reproducible artifact hashes.

| Evidence | Windows | Linux | macOS |
|---|---|---|---|
| Full unit/adversarial suite | Executed locally | Required in CI | Required in CI |
| Native no-replace publication path | Executed locally | Required in CI | Required in CI |
| Python 3.10–3.14 matrix | Required in CI | Required in CI | Required in CI |
| Reproducible clean-archive artifacts | Passed locally | Required in CI | Required in CI |
| Installed wheel and sdist lifecycle | Passed locally | Required in CI | Required in CI |
| Installed guard deny/allow canary | Passed locally | Required in CI | Required in CI |
| Separate-process advisory lock | Passed locally | Required in CI | Required in CI |
| Claude Code interoperability canary | Passed with Claude Code 2.1.237 | Not claimed | Not claimed |

The implementation has platform-specific fail-closed tests for Linux
`renameat2(..., RENAME_NOREPLACE)` and macOS
`renamex_np(..., RENAME_EXCL)`. Those tests do not substitute for native runner
execution. Unsupported POSIX systems and unavailable primitives return an
unsupported-operation error rather than falling back to overwrite-capable
publication.

The Claude Code canary used an isolated temporary project and a uniquely named
skill installed in copy mode. A public GitHub commit installation classified
direct, converted, explicitly adapted, and excluded fixtures as intended;
`check` returned no issues, repeated synchronization was a no-op, and Claude
Code 2.1.237 resolved the projected bundled reference and returned its canary
token. The guard denied a protected-tree mutation and allowed a read-only
control. Removal cleaned only managed skills. The personal Claude skill
directory was not modified. A real 103-skill Hermes source scan also completed
with no issues; the non-mutating plan against the existing personal destination
reported no actions and no issues. Inventory names, tokens, and local paths are
not published as release evidence.

## Reproduction

From a clean candidate commit:

```bash
uv sync --frozen
uv run --frozen python scripts/release_gate.py
```

The gate rejects dirty source, archives `HEAD`, installs a frozen environment in
the extracted archive, runs pytest/Ruff/compileall, builds twice with a fixed
`SOURCE_DATE_EPOCH`, validates exact recursive archive contents and metadata,
compares hashes, proves an installed guard deny plus a read-only allow control,
and exercises both installed artifacts through scan/sync/check/no-op/remove while
preserving an unmanaged sentinel. The test suite also launches separate processes
to prove destination-lock serialization through the host OS.

See [Release process](RELEASING.md) for the tag and publication boundary.

## Windows destination-authority qualification

The Windows checkpoint suite must exercise these cases on an NTFS host rather
than treating a mocked path check as sufficient:

- handle-relative, one-component-at-a-time creation from retained parent
  authority;
- physical junction-replacement attempts during missing-ancestor creation and
  before sync/remove lock acquisition;
- side-effect-free removal of a destination whose parent chain is absent;
- exact empty-ancestor rollback, non-recursive preservation of outsider
  content, cross-volume identity rejection, and retry after a post-lock
  transaction failure;
- malformed `NTSTATUS`, `HANDLE`, `IO_STATUS_BLOCK.Information`, and identity
  carriers, including close-on-rejected-success behavior;
- exactly-once close failure reporting and proof that normal and exceptional
  exits release rename-blocking authority handles; and
- serialization of public `sync_library()` and `remove_library()` calls in
  separate Python processes.

The remaining recursive directory creation sites are deliberately outside the
Windows authority boundary: supporting-file parents are source-derived paths
inside an already allocated staging root, and the POSIX branch retains its
existing recursive establishment behavior. The handle-relative ancestor fix
and its native race claims are Windows-specific.

For release qualification, run the full suite under every supported Python
minor (3.10 through 3.14), then run the clean-archive release gate. Record the
exact pass/skip totals and candidate commit in release evidence; do not infer
Windows coverage from a non-Windows run where these tests are skipped.
