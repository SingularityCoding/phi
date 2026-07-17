# Contributing to Phi

Phi is currently developed as a complete Agent Harness reference implementation. Course materials
will be derived from a stable implementation later; contributions should target the reference
runtime rather than a provisional lesson plan.

## Prerequisites

- Python 3.12
- [`uv`](https://docs.astral.sh/uv/)
- Git

Run commands from the repository root.

## Set up the repository

Install the locked dependencies:

```bash
uv sync --locked
```

Create a local configuration file when you need to launch Phi:

```bash
cp .env.example .env
```

Never commit a real LiteLLM virtual key. Keep secrets out of tests, fixtures, screenshots, traces,
and documentation.

## Run the current application

The bare command currently launches the minimal Textual shell:

```bash
uv run phi
```

Commands documented in system-design documents are not available until their code and tests land.

## Required validation

Before handing off a code or configuration change, run the full local suite:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

For a documentation-only change, run at least:

```bash
git diff --check
```

Run the full suite as well when documentation changes commands, paths, imports, configuration, or
claims about implemented behavior.

The pre-commit configuration also runs repository hygiene and Ruff checks. The pre-push stage runs
the test suite.

## Preview the course site

The course site prototype lives under `docs/course/`. It is a teaching surface, not design
authority for the reference runtime. Install its locked dependency group and start the local
preview from the repository root:

```bash
uv sync --locked --group docs
uv run --group docs mkdocs serve
```

Open `http://127.0.0.1:8000/` in a browser. Before handing off course-site changes, run:

```bash
uv run --group docs mkdocs build --strict
```

The generated `site/` directory is ignored. GitHub Actions builds the same source and uploads only
that generated directory to GitHub Pages.

## Test policy

- Default tests must be deterministic and offline.
- Model and Harness tests should use an injected Scripted Model.
- A Scripted Model records every request and fails when its response script is exhausted.
- Assert protocol shape, event order, stopping reasons, authorization decisions, and Environment
  outcomes.
- Do not assert exact nondeterministic wording from a live model.
- Real LiteLLM Proxy contract tests must be separately marked and explicitly opted into.
- Behavioral evaluations should inspect the final Environment state rather than trusting the
  model's self-report.

Run the opt-in Model contracts only with configured Proxy credentials:

```bash
PHI_RUN_MODEL_CONTRACTS=1 uv run pytest -m contract tests/model/test_contract.py
```

## Dependencies and generated files

- Use `uv`; do not install project dependencies with `pip` directly.
- Add or remove dependencies through `uv` so `pyproject.toml` and `uv.lock` stay synchronized.
- Do not hand-edit `.venv/`, `__pycache__/`, `.coverage`, `.pytest_cache/`, `.ruff_cache/`, or other
  generated files.

## Code conventions

- Use Python 3.12 and complete annotations for public boundaries.
- Prefer async APIs for network I/O, streaming, cancellation, tools, MCP, and subagents.
- Use absolute imports from `phi`.
- Keep CLI callbacks thin and Textual widgets focused on presentation and interaction.
- Keep `__init__.py` files small and expose only intentional public APIs.
- Add packages only when their first implementation arrives; do not create an empty target tree.

Stable architectural constraints live in [`AGENTS.md`](AGENTS.md). Current design lives under
[`docs/`](docs/README.md), and canonical terminology lives in [`CONTEXT.md`](CONTEXT.md).
