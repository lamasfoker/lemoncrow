# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Environment

**All Python commands must use `uv run`** — the project uses `uv` for dependency management and there is no activated venv. Direct `python3` calls will fail or use the wrong environment.

```bash
uv run python -c "..."          # one-off Python
uv run pytest ...               # tests
uv run mypy src                 # type-check
uv run lemoncrow ...                   # CLI
```

The `.venv` is at `.venv/bin/python3` if you need the path explicitly.

## Common Commands

```bash
# Test: only the files your change touches. tests/conftest.py refuses runs over
# 1000 tests; agents never set LEMONCROW_FULL_SUITE (a person opts in per command).
uv run pytest tests/path/test_file.py -q # single file
uv run pytest -q -k "test_name"          # single test by name

# Lint / format / typecheck
make lint           # ruff
make format         # ruff --fix + black + prettier (frontend)
make typecheck      # mypy --strict src

# Full pre-commit gate: runs the whole suite, so a person runs it with LEMONCROW_FULL_SUITE=1
make pre-commit     # format + lint + typecheck + docs + test

# Docs governance
make sync-agent-context   # regenerate host instruction files from integrations/agents/shared/

# Install Claude plugin (after changing integrations/claude/plugin/)
bash scripts/install_claude.sh
```

|     |     |     |
| --- | --- | --- |
|     |     |     |

## Status index

`docs/status/capabilities.toml` (private checkout only; `make status-index` renders `docs/status/README.md`) is the present-tense answer to what ships, in what state, with what default and which tests prove it; `docs/planning/` plans are history. Change it in the same commit that changes a capability's state or default; `make docs-check` enforces it.

## Coding Guidelines

The full guidelines (think before coding, simplicity first, surgical changes, goal-driven execution) are embedded in every LemonCrow persona. Source of truth: `integrations/agents/shared/coding-guidelines.md` — do not restate them here.
