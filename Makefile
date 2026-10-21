.DEFAULT_GOAL := help
VENV ?= .venv
PY := $(VENV)/bin/python

.PHONY: help venv install dev worker test cov lint fmt docker-up docker-down clean

help: ## Show targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

venv: ## Create virtualenv
	test -d $(VENV) || python3 -m venv $(VENV)

install: venv ## Install with dev + smtp extras
	$(VENV)/bin/pip install --upgrade pip
	$(VENV)/bin/pip install -e ".[dev,smtp]"

dev: ## API + in-process worker with reload on :8000
	$(VENV)/bin/uvicorn notifications.main:app --reload --port 8000

worker: ## Standalone delivery worker
	$(PY) -m notifications.worker

test: ## Run tests
	$(PY) -m pytest

cov: ## Tests with coverage
	$(PY) -m pytest --cov=notifications --cov-report=term-missing

lint: ## Ruff check + format check
	$(VENV)/bin/ruff check notifications tests
	$(VENV)/bin/ruff format --check notifications tests

fmt: ## Format + autofix
	$(VENV)/bin/ruff format notifications tests
	$(VENV)/bin/ruff check --fix notifications tests

docker-up: ## api + worker + postgres
	docker compose up --build -d

docker-down: ## Stop stack
	docker compose down

clean: ## Remove caches and local state
	rm -rf .pytest_cache .ruff_cache htmlcov .coverage notifications.db push-outbox.jsonl
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
