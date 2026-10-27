FROM node:22-slim AS node
FROM python:3.13-slim-trixie
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /usr/local/bin/uv
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && apt-get update \
 && apt-get install -y --no-install-recommends bash bubblewrap iptables git ca-certificates tini \
 && rm -rf /var/lib/apt/lists/*
RUN npm install --prefix /usr/local/lib/box-bundler --save-exact --omit=dev --no-audit --no-fund \
    esbuild@0.28.2 parse5@8.0.0
COPY box_agent_sdlc/bundler.mjs /usr/local/lib/box-bundler/bundler.mjs
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 \
    EXECUTOR_PORT=8082 EXECUTOR_SANDBOX=bubblewrap \
    npm_config_cache=/tmp/npm-cache
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY box_agent_sdlc/ box_agent_sdlc/
RUN uv sync --frozen --no-dev \
 && mkdir -p /workspaces \
 && chown 65532:65532 /workspaces
USER 65532:65532
EXPOSE 8082
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "box_agent_sdlc.executor"]
