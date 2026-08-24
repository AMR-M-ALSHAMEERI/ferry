.PHONY: install lint format typecheck test check clean verify-m1 verify-m2 verify-m3 verify-m4 verify-m5

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

verify-m2:
	python scripts/selfcheck/verify_m2.py

verify-m3:
	python scripts/selfcheck/verify_m3.py

verify-m4:
	python scripts/selfcheck/verify_m4.py

verify-m5:
	python scripts/selfcheck/verify_m5.py

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache .pytest-tmp htmlcov .coverage dist build
	find . -type d -name __pycache__ -exec rm -rf {} +
