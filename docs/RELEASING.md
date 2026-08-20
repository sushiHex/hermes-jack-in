# Release process

A release is made from a clean, reviewed public root commit—not from the private
development ancestry.

## Preconditions

1. The intended version is present in `pyproject.toml` and `uv.lock`, and its
   changes are recorded under `Unreleased` in `CHANGELOG.md`.
2. The native GitHub Actions matrix is green on Windows, Linux, and macOS for
   every declared Python minor.
3. `uv run --frozen python scripts/release_gate.py` passes from a clean archive
   of the exact candidate.
4. Wheel and sdist contents match the release allowlist, build twice to identical
   hashes with a fixed `SOURCE_DATE_EPOCH`, and install successfully in isolated
   environments.
5. A sanitized real Hermes scan and non-mutating destination plan have zero
   issues. A temporary Claude Code canary records the exact Claude version.
6. Documentation, security policy, platform claims, and dependency lock are
   current. No private plans, inventories, paths, credentials, or raw audit logs
   are reachable from the public history.

For a private release audit, set `HERMES_JACK_IN_FORBIDDEN_MARKERS` to a
comma-separated list of owner-specific identifiers that must not occur in any
wheel or sdist member. Keep the values outside the repository and its logs.

## Publication

Before tagging, convert the matching `Unreleased` notes into a dated version
section. Use a signed `vX.Y.Z` tag whose version exactly matches package metadata.
Build from that tag. Publish to PyPI only through GitHub OIDC Trusted Publishing and a
protected production environment; do not store a long-lived PyPI token.
Generate SHA-256 checksums and retain build provenance with the release.

Repository creation, remote configuration, pushing, tagging, GitHub environment
configuration, and package publication are separate owner-authorized actions.
The repository intentionally contains no automatic publishing workflow until
those controls exist and have been reviewed.
