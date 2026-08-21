# Development setup

This is PLAN.md §1.4, reproduced verbatim so contributors never need to read
PLAN.md to get started.

## Virtual environment (required — never install into system Python)

```bash
# From the repo root
python3.11 -m venv .venv

# Activate
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows PowerShell

# Confirm you're in the venv — should print a path inside .venv
which python                        # macOS / Linux
where python                        # Windows

# Upgrade packaging tools
python -m pip install --upgrade pip setuptools wheel
```

Every `pip install` in this project runs inside the activated venv. If a
command fails with a permissions error, that's the signal the venv isn't
active — stop and activate it, do not use `sudo` or `--user`.

**Note for contributors on a machine without Python 3.11 on `PATH`:** the
project's floor is 3.11+ (see PLAN.md §1.2 for why), and CI tests 3.11, 3.12,
and 3.13. If only a newer interpreter is available locally, you can still
develop against it, but be aware code that only works on your newer version
may fail CI on the floor version.

## Install the project in editable mode with dev extras

```bash
pip install -e ".[dev]"
```

This requires `pyproject.toml` to define an optional dependency group:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-cov",
    "ruff",
    "mypy",
    "pre-commit",
]
```

## Pre-commit hooks

```bash
pre-commit install
```

`.pre-commit-config.yaml` runs: `ruff check --fix`, `ruff format`, `mypy`, and
a large-file guard (`check-added-large-files`, max 500KB — a tripwire against
accidentally committing a real conversation bundle).

## The standard verification loop

Run this before every commit and at the end of every work session:

```bash
ruff check .          # lint
ruff format --check . # formatting
mypy src/             # types
pytest -v             # tests
pytest --cov=ferry --cov-report=term-missing   # coverage
```

Paste the actual output of `pytest` into `PROGRESS.md`, not a summary —
"tests pass" is not acceptable; the summary line (e.g. `24 passed in 1.83s`)
is.

## Makefile (convenience)

```makefile
.PHONY: install lint format typecheck test check clean

install:
	pip install -e ".[dev]"
	pre-commit install

lint:
	ruff check .

format:
	ruff format .

typecheck:
	mypy src/

test:
	pytest -v

check: lint typecheck test

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
```

**Windows users without `make`:** run the underlying commands directly —
`pip install -e ".[dev]"` then `pre-commit install` for `make install`;
`ruff check .` for `make lint`; `ruff format .` for `make format`; `mypy src/`
for `make typecheck`; `pytest -v` for `make test`.
