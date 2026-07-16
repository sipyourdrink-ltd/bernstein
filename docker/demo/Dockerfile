# Demo image — bernstein + Claude Code CLI for real agent execution
#
# Build context must be the repository root:
#   docker build -f docker/demo/Dockerfile -t bernstein-demo .
#
# Stage 1: build the bernstein wheel
# python:3.13-slim
FROM python:3.13-slim@sha256:bffeb7bd6a85767587059c6ba23e1e9122078e3aa3fa836099171b9bb5a9bb00 AS build

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir hatchling==1.29.0 && \
    python -m hatchling build

# Stage 2: runtime with Node.js for Claude Code
# python:3.13-slim
FROM python:3.13-slim@sha256:bffeb7bd6a85767587059c6ba23e1e9122078e3aa3fa836099171b9bb5a9bb00

# Node.js + Claude Code + git + curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    nodejs \
    npm \
    && npm install -g @anthropic-ai/claude-code@2.1.143 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY --from=build /app/dist/*.whl /tmp/

# Copy the demo project template into a fixed location
COPY templates/demo/ /workspace/project/

# Copy the demo-cycle script
COPY docker/demo/demo-cycle.sh /usr/local/bin/demo-cycle
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl && \
    chmod +x /usr/local/bin/demo-cycle && \
    useradd -m -u 1000 bernstein && \
    chown -R bernstein:bernstein /workspace
USER bernstein

# Task server port
EXPOSE 8052

# Probe the demo task server. Demo mode always runs `bernstein conduct`, which
# starts the HTTP server on 8052; if a downstream consumer overrides CMD they
# can disable this with `--health-cmd=NONE`.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8052/health || exit 1

# State directory — mount a volume here so demo state persists across cycles
VOLUME ["/workspace/.sdd"]

ENTRYPOINT ["bernstein"]
CMD ["conduct"]
