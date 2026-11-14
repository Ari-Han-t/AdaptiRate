# Build from the repository root (the SDK lives in ./sdk-python).
FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY sdk-python /sdk
RUN pip install /sdk "fastapi>=0.115" "uvicorn[standard]>=0.30"
COPY acceptance/services/orders_api/app.py .
RUN useradd --system --uid 10001 --no-create-home svc
USER svc
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
