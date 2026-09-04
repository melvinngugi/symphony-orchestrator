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
COPY agents.yaml /app/agents.yaml
COPY agent-output-schema.json /app/agent-output-schema.json
COPY backlog-curation-schema.json /app/backlog-curation-schema.json
RUN uv run --python /opt/venv/bin/python python -m compileall -q /app/app

FROM python:3.14-slim AS codex

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl -fsSL https://chatgpt.com/codex/install.sh | sh \
    && install -m 0755 "$(readlink -f /root/.local/bin/codex)" /usr/local/bin/codex \
    && rm -rf /var/lib/apt/lists/*

FROM python:3.14-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKSPACE_ROOT=/app/workspaces \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app/app /app/app
COPY --from=builder /app/WORKFLOW.md /app/WORKFLOW.md
COPY --from=builder /app/agents.yaml /app/agents.yaml
COPY --from=builder /app/agent-output-schema.json /app/agent-output-schema.json
COPY --from=builder /app/backlog-curation-schema.json /app/backlog-curation-schema.json
COPY --from=codex /usr/local/bin/codex /usr/local/bin/codex

RUN mkdir -p /app/workspaces /root/.codex

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=4).read()"]

CMD ["python", "-m", "app.main"]
