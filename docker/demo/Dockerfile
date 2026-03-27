# Demo image — bernstein + Claude Code CLI for real agent execution
#
# Build context must be the repository root:
#   docker build -f docker/demo/Dockerfile -t bernstein-demo .
#
# Stage 1: build the bernstein wheel
FROM python:3.12-slim AS build

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir hatchling && \
    python -m hatchling build

# Stage 2: runtime with Node.js for Claude Code
FROM python:3.12-slim

# Node.js + Claude Code + git + curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    nodejs \
    npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY --from=build /app/dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl

# Copy the demo project template into a fixed location
COPY templates/demo/ /workspace/project/

# Copy the demo-cycle script
COPY docker/demo/demo-cycle.sh /usr/local/bin/demo-cycle
RUN chmod +x /usr/local/bin/demo-cycle

# Non-root user
RUN useradd -m -u 1000 bernstein && \
    chown -R bernstein:bernstein /workspace
USER bernstein

# Task server port
EXPOSE 8052

# State directory — mount a volume here so demo state persists across cycles
VOLUME ["/workspace/.sdd"]

ENTRYPOINT ["bernstein"]
CMD ["conduct"]
