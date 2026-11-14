# Build from the repository root:  docker build -f examples/fastapi/Dockerfile .
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY sdk-python /sdk
RUN pip install "/sdk[host]" "fastapi>=0.115" "uvicorn[standard]>=0.30"
COPY examples/fastapi/ .
RUN useradd --system --uid 10001 --no-create-home demo
USER demo
EXPOSE 8090
# Create the demo policies and an API key, then start the app with that key.
CMD ["sh", "-c", "ADAPTIRATE_API_KEY=$(python bootstrap.py) exec uvicorn app:app --host 0.0.0.0 --port 8090 --no-access-log"]
