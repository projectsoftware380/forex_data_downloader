.PHONY: setup lint format test

setup:
	python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

lint:
	ruff src

format:
	black src

test:
	pytest -q