FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir uv==0.13.8
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
COPY guardian ./guardian
COPY migrations ./migrations
COPY data ./data
COPY tests ./tests
RUN uv sync --frozen && useradd --create-home --user-group guardian && chown -R guardian:guardian /app
USER guardian
ENTRYPOINT ["/app/.venv/bin/guardian"]

