# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /srv
COPY pyproject.toml ./
# Install dependencies before copying source so code changes do not bust the layer.
RUN mkdir -p app && touch app/__init__.py && pip install .

FROM base AS dev
RUN pip install ".[dev]"
COPY . .
CMD ["pytest", "-q"]

FROM base AS runtime
COPY . .
RUN pip install --no-deps . \
    && useradd --system --uid 10001 --no-create-home adaptirate
USER adaptirate
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
