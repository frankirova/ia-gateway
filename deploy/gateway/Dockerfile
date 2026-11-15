FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir "fastapi>=0.115,<1" "uvicorn>=0.34,<1"

COPY app /app/app

RUN useradd --uid 10002 --user-group --create-home gateway \
    && chown -R gateway:gateway /app

USER gateway

EXPOSE 8000

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
