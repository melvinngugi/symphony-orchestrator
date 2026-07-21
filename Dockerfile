FROM python:3.14-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY requirements.txt /app/requirements.txt
RUN uv venv /opt/venv
RUN uv pip install --python /opt/venv/bin/python --no-cache --compile-bytecode -r /app/requirements.txt

COPY app /app/app
COPY WORKFLOW.md /app/WORKFLOW.md
RUN uv run --python /opt/venv/bin/python python -m compileall -q /app/app

FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSPACE_ROOT=/app/workspaces \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/app /app/app
COPY --from=builder /app/WORKFLOW.md /app/WORKFLOW.md

RUN mkdir -p /app/workspaces

EXPOSE 8000

CMD ["python", "-m", "app.main"]
