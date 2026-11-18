# The agent as an HTTP service. Build from the repository root, because the
# agent also needs scenarios/categories.yaml:
#   docker build -f agent/Dockerfile .
# compose.yaml does this for you.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.15 /uv /bin/uv

WORKDIR /app/agent

# Dependencies first, so code changes don't reinstall them.
COPY agent/pyproject.toml agent/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY agent/src ./src
RUN uv sync --frozen --no-dev

# Only the category list. The answer keys and manifests next to it stay out of
# the image: the agent must never be able to read what it is graded against.
COPY scenarios/categories.yaml /app/scenarios/categories.yaml

ENV PATH=/app/agent/.venv/bin:$PATH \
    CATEGORIES_FILE=/app/scenarios/categories.yaml \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
EXPOSE 8000

# compose.yaml replaces this with your own user id, so the kubeconfigs are readable.
USER nobody
# Bind to every interface inside the container; compose.yaml publishes the
# port on 127.0.0.1 only.
CMD ["uvicorn", "diagnoster.server:app_from_env", "--factory", "--host", "0.0.0.0", "--port", "8000"]
