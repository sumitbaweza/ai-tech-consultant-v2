# One image for every service. uv's official image ships Python 3.14 and uv itself.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS base

WORKDIR /app
# Compile .pyc at install time, copy packages instead of linking, keep the venv outside /app
# so a bind mount of the source (the `tools` service) doesn't hide it
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# git: the Self-Improve Loop works on branches in git worktrees.
# libatomic1: needed by the Node.js that pyright downloads (dev image only, but small).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first: this layer is reused until pyproject.toml or uv.lock change
COPY pyproject.toml uv.lock .python-version ./

FROM base AS runtime
RUN uv sync --frozen --no-dev
COPY . .
RUN useradd --create-home app && mkdir -p /data/storage && chown -R app /app /data
USER app
EXPOSE 8080
CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8080"]

# Tests, lint and type checks run here, against the source bind-mounted at /app
FROM base AS dev
RUN uv sync --frozen
# pyright brings its own Node.js; download it into the image once, not on every run
RUN pyright --version
CMD ["pytest", "-q"]
