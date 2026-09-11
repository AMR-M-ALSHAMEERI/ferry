# Development

How to set up Ferry for working on it, and the checks every change goes
through.

## Virtual environment

Always work inside a virtual environment, never in your system Python.

```bash
python3.11 -m venv .venv
```

Then activate it.

On macOS and Linux:

```bash
source .venv/bin/activate
```

On Windows, in PowerShell:

```bash
.venv\Scripts\Activate.ps1
```

Check that it worked: `which python` on macOS and Linux, or `where python` on
Windows, should show a path inside `.venv`. Then update the packaging tools:

```bash
python -m pip install --upgrade pip setuptools wheel
```

If a `pip install` fails with a permissions error, the environment is not
active. Activate it rather than reaching for `sudo` or `--user`.

**Python versions.** Ferry supports Python 3.11 and newer, and CI tests 3.11,
3.12, 3.13 and 3.14 on Windows, macOS and Linux: twelve combinations on every
push. You can develop on a newer Python, but code that only works there will
fail CI on 3.11.

## Installing Ferry for development

```bash
pip install -e ".[dev]"
```

That installs Ferry in editable mode along with pytest, ruff, mypy and
pre-commit. The `dev` group in `pyproject.toml` lists them. **ruff and mypy are
pinned to exact versions,** and the same versions are pinned in
`.pre-commit-config.yaml`. Both decide whether a change passes, so a version
that floats is a check that can change its mind between your machine and CI.
When you update one pin, update the other.

## Pre-commit hooks

```bash
pre-commit install
```

Before every commit, the hooks run ruff's linter and formatter and mypy, trim
trailing whitespace, fix file endings, look for leftover merge conflict
markers, and refuse any file over 500 KB. That last one is a tripwire against
committing a real conversation bundle by accident.

## Checking a change

Run these before every commit:

```bash
ruff check .
```

```bash
ruff format --check .
```

```bash
mypy src/
```

```bash
pytest --basetemp=.pytest-tmp
```

Or run all of them with `make check`. For coverage:

```bash
pytest --cov=ferry --cov-report=term-missing
```

**The test suite never touches your real assistants.** Every test points Ferry
at empty temporary folders, and the same goes for Ferry's own backups and
records, so running the suite on a machine full of real history changes
nothing.

## Self-checks against real data

The tests use invented conversations. The self-checks use yours, read only:

```bash
make verify-m8
```

There is one per milestone, from `verify-m1` to `verify-m8`. Each prints a
numbered PASS, FAIL or SKIP line per check, never prints conversation content,
and never changes anything. Run the one that covers the area you are working
on.

## Other scripts

| Command | What it does |
|---|---|
| `python scripts/gen_schema.py` | Regenerates the conversation schema in `schemas/` after a model change. A test fails if you forget. |
| `python scripts/make_logo.py` | Redraws the logo images in `docs/assets/` from the same wordmark the terminal shows. |

## Without make

On Windows without `make`, run the commands directly: `pip install -e ".[dev]"`
and `pre-commit install` instead of `make install`, the four checking commands
above instead of `make check`, and `python scripts/selfcheck/verify_m8.py`
instead of `make verify-m8`.
