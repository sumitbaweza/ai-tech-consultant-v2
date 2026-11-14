# Start everything with `make up`. Windows has no `make`: run the command lines in PowerShell.
.PHONY: up down logs ps psql test lint format typecheck lock eval-url local-only

# Build and start every service in the background
up:
	docker compose up -d --build

# Stop every service; the data stays in the pgdata and storage volumes
down:
	docker compose down

logs:
	docker compose logs -f

ps:
	docker compose ps

psql:
	docker compose exec postgres psql -U postgres -d consultant

# Tests run in Docker on Python 3.14, against the consultant_test database
test:
	docker compose run --rm tools pytest -q

lint:
	docker compose run --rm tools sh -c "ruff check . && ruff format --check ."

typecheck:
	docker compose run --rm tools pyright

format:
	docker compose run --rm tools sh -c "ruff check --fix . && ruff format ."

# Refresh uv.lock after changing pyproject.toml
lock:
	docker compose run --rm tools uv lock

# The 10 URL benchmark cases need the fixture website (no model calls, no cost)
eval-url:
	docker compose --profile eval up -d fixture-site
	docker compose exec web python -m eval_lab.cli url-cases

# Run the whole app on the 2 local Ollama models only (see README)
local-only:
	docker compose exec web python -m workspace.cli local-only --business "Solution Center Demo" --on
