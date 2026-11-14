FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN pip install "fastapi>=0.115" "uvicorn[standard]>=0.30"
COPY acceptance/services/fake_provider/app.py .
RUN useradd --system --uid 10001 --no-create-home svc
USER svc
EXPOSE 9000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9000", "--no-access-log"]
