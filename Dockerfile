# One image for the Python services (api, gate). docker-compose picks the command.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

RUN pip install --no-cache-dir uv
COPY pyproject.toml ./
RUN uv pip install --system -r pyproject.toml

COPY . .

# Run as a non-root user
RUN useradd --create-home trustgate && chown -R trustgate /app
USER trustgate

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
