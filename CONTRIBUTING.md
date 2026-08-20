# Contributing

Hermes Jack-In welcomes focused bug reports and pull requests.

## Before opening a change

- Use an issue for behavioral or security-boundary changes so the expected
  contract can be agreed first.
- Never use a real `~/.claude/skills` directory in tests. Destructive and
  mutation-race tests must use temporary fixtures.
- Do not weaken destination alias, ownership, identity, or rollback checks to
  make a test pass.
- Do not include personal paths, live skill inventories, credentials, or raw
  audit artifacts.

## Development

Install [uv](https://docs.astral.sh/uv/), clone the repository, then run:

```text
uv sync --frozen
uv run --frozen python scripts/release_gate.py
```

Behavior changes follow red-green-refactor: add a focused failing regression,
make the smallest production change, then run the complete release gate.

## Pull requests

Explain the safety contract affected, tests added, platforms exercised, and any
remaining limitations. Keep generated artifacts and private investigation
notes out of the commit. By contributing, you agree that your contribution is
licensed under the MIT License.
