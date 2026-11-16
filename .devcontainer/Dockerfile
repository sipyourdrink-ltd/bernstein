FROM mcr.microsoft.com/devcontainers/python:3-3.13-trixie

ARG TARGETARCH
RUN set -eux; \
    case "$TARGETARCH" in \
        amd64) uv_arch=x86_64; uv_sha=c5221ff41b7892bfc223543883600125f6a47fe845093e0f5bdff4f359196d83 ;; \
        arm64) uv_arch=aarch64; uv_sha=71279f8d3c67241e807b9d8db872d1bf541579fb9f08b7d1eb1682196d1e57dc ;; \
        *) echo "Unsupported devcontainer architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    uv_archive="uv-${uv_arch}-unknown-linux-gnu"; \
    curl -fsSL "https://github.com/astral-sh/uv/releases/download/0.13.7/${uv_archive}.tar.gz" -o /tmp/uv.tar.gz; \
    echo "$uv_sha  /tmp/uv.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/uv.tar.gz -C /usr/local/bin --strip-components=1 "$uv_archive/uv" "$uv_archive/uvx"; \
    rm /tmp/uv.tar.gz
