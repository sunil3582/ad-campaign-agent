.PHONY: eval check

eval:
	@if [ -x .venv/bin/python ]; then .venv/bin/python -m pytest -v; \
	elif command -v docker >/dev/null 2>&1; then \
	  docker compose run --build --rm --entrypoint /app/.venv/bin/python app -m pytest -v; \
	else echo 'Install Docker with Compose, or create the documented local environment.'; exit 1; fi

check:
	@if [ -x .venv/bin/ruff ]; then .venv/bin/ruff check guardian tests && .venv/bin/ruff format --check guardian tests; \
	elif command -v docker >/dev/null 2>&1; then \
	  docker compose run --build --rm --entrypoint /app/.venv/bin/ruff app check guardian tests && \
	  docker compose run --rm --entrypoint /app/.venv/bin/ruff app format --check guardian tests; \
	else echo 'Install dependencies with uv sync --frozen, or install Docker with Compose.'; exit 1; fi
