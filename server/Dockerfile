# Build context is the repository root (see docker-compose.yml) so the
# runtime's seccomp profile can be included alongside the API.
FROM docker:28-cli AS docker-cli
FROM node:22-bookworm-slim AS node

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY server/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Docker CLI for the optional computer runtime. It only ever talks to the
# daemon named by DOCKER_HOST (the socket proxy in compose); no socket is
# mounted into this image.
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
# node, npm and npx for MCP servers started with `npx ...`; uvx for Python ones.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && pip install --no-cache-dir uv
# node, npm and npx for MCP servers started with `npx ...`; uvx for Python ones.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && pip install --no-cache-dir uv

COPY server/app ./app
COPY server/run.py ./
COPY runtime/seccomp_profile.json ./runtime/seccomp_profile.json

# Persistent state lives in /data (SQLite, encryption key, auth token) and the
# bots' confined workspace in /workspace. Both are volumes in docker-compose.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data /workspace \
    && chown -R appuser:appuser /data /workspace /app
USER appuser

ENV HOST=0.0.0.0 \
    PORT=8000 \
    DATA_DIR=/data \
    WORKSPACE_ROOT=/workspace \
    COMPUTER_PROVIDER=fake \
    COMPUTER_DOCKER_SECCOMP_PROFILE=/app/runtime/seccomp_profile.json

EXPOSE 8000

# --proxy-headers lets the API log the real client address when Caddy is in
# front. Loopback session bootstrap is disabled whenever APP_AUTH_TOKEN is set,
# so trusting the forwarded address never grants access on its own.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]
