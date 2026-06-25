# Convenience targets. On Windows where the launcher is `py`, run e.g.:
#   make seed PYTHON=py
PYTHON ?= python

.PHONY: help setup migrate seed run scheduler test lint simulate reprocess docker-up docker-down

help:
	@echo "Targets: setup migrate seed run scheduler test lint simulate reprocess docker-up docker-down"
	@echo "  simulate:  make simulate DATE=2026-06-20 MODE=HISTORICAL"
	@echo "  reprocess: make reprocess DATES='2026-06-20 2026-06-21'"

setup:
	$(PYTHON) -m pip install -r requirements.txt

migrate:
	$(PYTHON) -m alembic upgrade head

seed:
	$(PYTHON) -m app.db.seed

run:
	$(PYTHON) -m uvicorn app.main:app --host 0.0.0.0 --port 8000

scheduler:
	$(PYTHON) -m app.scheduler_runner

test:
	$(PYTHON) -m pytest -q

lint:
	$(PYTHON) -m ruff check app tests

# Usage: make simulate DATE=2026-06-20 MODE=HISTORICAL
simulate:
	$(PYTHON) -m app.cli simulate --date $(DATE) $(if $(MODE),--mode $(MODE),)

# Usage: make reprocess DATES="2026-06-20 2026-06-21"
reprocess:
	$(PYTHON) -m app.cli reprocess --dates $(DATES)

docker-up:
	docker compose up --build

docker-down:
	docker compose down
