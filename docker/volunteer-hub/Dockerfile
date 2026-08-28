# Volunteer hub image — bernstein for the hub role
#
# Build context must be the repository root:
#   docker build -f docker/volunteer-hub/Dockerfile -t bernstein-hub .
#
# Stage 1: build the bernstein wheel
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a AS build

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir hatchling==1.29.0 && \
    python -m hatchling build

# Stage 2: runtime
FROM python:3.13-slim@sha256:ffb752e139c0a19692a43af8d8523b274222dd68eebad5d583b45c2201c6e30a

WORKDIR /workspace

COPY --from=build /app/dist/*.whl /tmp/

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl jq git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir /tmp/*.whl \
    && rm /tmp/*.whl \
    && useradd -m -u 1000 rig \
    && chown -R rig:rig /workspace

USER rig

# Ports
EXPOSE 8052 8053

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl --fail --silent --show-error http://127.0.0.1:8052/health || exit 1

# State directory
VOLUME ["/workspace/.sdd"]

ENTRYPOINT ["bernstein"]
CMD ["conduct"]
