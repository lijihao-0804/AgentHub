FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY apps ./apps
COPY packages ./packages
COPY migrations ./migrations
COPY alembic.ini ./
COPY scripts ./scripts

RUN uv sync --locked --no-dev

# `uv run` re-syncs before it runs anything, and the sync it does is the full
# one -- so every container start reinstalled the dev group this build went to
# the trouble of excluding, from the network, before serving a single request.
# The environment is already complete by this line; nothing at runtime should
# be reaching for a package index.
ENV UV_NO_SYNC=1

EXPOSE 8000
CMD ["uv", "run", "uvicorn", "apps.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
