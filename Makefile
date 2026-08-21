.PHONY: install lint format typecheck test check clean verify-m1

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
	pytest -v --basetemp=.pytest-tmp

check: lint typecheck test

verify-m1:
	python scripts/selfcheck/verify_m1.py

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .pytest-tmp htmlcov .coverage dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
