.PHONY: install lint format typecheck test check clean verify-m1 verify-m2 verify-m3 verify-m4 verify-m5 verify-m6 verify-m7 verify-m7b verify-m7c verify-m8

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

verify-m6:
	python scripts/selfcheck/verify_m6.py

verify-m7:
	python scripts/selfcheck/verify_m7.py

verify-m7b:
	python scripts/selfcheck/verify_m7b.py

verify-m7c:
	python scripts/selfcheck/verify_m7c.py

verify-m8:
	python scripts/selfcheck/verify_m8.py
