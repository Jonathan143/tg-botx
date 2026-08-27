.PHONY: install dev format lint typecheck test check

install:
	python -m pip install -e .

dev:
	python -m pip install -e '.[dev]'

format:
	python -m ruff format src tests
	python -m ruff check --fix src tests

lint:
	python -m ruff check src tests
	python -m ruff format --check src tests

typecheck:
	python -m mypy src/tg_botx

test:
	python -m pytest

check: lint typecheck test
