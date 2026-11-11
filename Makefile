.PHONY: sync hooks lint typecheck test build hygiene check

sync:
	uv sync --locked

hooks:
	uv run --locked pre-commit install

lint:
	uv run --locked ruff check src tests scripts
	uv run --locked ruff format --check src tests scripts

typecheck:
	uv run --locked mypy

test:
	uv run --locked pytest

build:
	uv build

hygiene:
	uv run --locked python scripts/check_repository_hygiene.py

check: hygiene lint typecheck test build
