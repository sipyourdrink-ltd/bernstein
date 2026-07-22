# Stage 1: build
# python:3.13-slim
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91 AS build

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir hatchling==1.29.0 && \
    python -m hatchling build

# Stage 2: runtime
# python:3.13-slim
FROM python:3.13-slim@sha256:6771159cd4fa5d9bba1258caf0b82e6b73458c694d178ad97c5e925c2d0e1a91

LABEL org.opencontainers.image.title="bernstein" \
      org.opencontainers.image.description="Declarative agent orchestration for engineering teams" \
      org.opencontainers.image.source="https://github.com/bernstein-ai/bernstein" \
      io.modelcontextprotocol.server.name="io.github.sipyourdrink-ltd/bernstein"

# Install git (required for git_ops) and curl (healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

COPY --from=build /app/dist/*.whl /tmp/

RUN pip install --no-cache-dir /tmp/*.whl && rm /tmp/*.whl && \
    useradd -m -u 1000 bernstein && chown bernstein:bernstein /workspace
USER bernstein

# Bernstein state directory (mount a volume here for persistence)
VOLUME ["/workspace/.sdd"]

# Task server HTTP + gRPC ports
EXPOSE 8052 50051

# Probe the task server health endpoint. Override via docker-compose / Helm for
# components that do not expose HTTP (e.g. worker-only deployments) by passing
# `--health-cmd=NONE` or replacing this with the component-specific check.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8052/health || exit 1

# Default: run the task server in the foreground so PID 1 stays alive and the
# container can host a long-lived central/coordinator node whose /health
# endpoint (probed above) stays reachable. Bind all interfaces and enable
# cluster endpoints via env, e.g.:
#   docker run -e BERNSTEIN_BIND_HOST=0.0.0.0 -e BERNSTEIN_CLUSTER_ENABLED=1 \
#     -p 8052:8052 ghcr.io/sipyourdrink-ltd/bernstein
# Override CMD in docker-compose / Helm to run other components, e.g.:
#   Orchestrated run: bernstein run plan.yaml --remote
ENTRYPOINT ["bernstein"]
CMD ["serve"]
