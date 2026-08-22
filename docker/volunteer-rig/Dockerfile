FROM python:3.13-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl jq git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY docker/volunteer-rig/seed-gitea.sh /usr/local/bin/volunteer-rig-seed
COPY docker/volunteer-rig/assert.sh /usr/local/bin/volunteer-rig-assert
RUN chmod +x /usr/local/bin/volunteer-rig-seed /usr/local/bin/volunteer-rig-assert

# Fixture project files
COPY docker/volunteer-rig/fixtures/ /fixtures/

# The rig clones into /tmp and reads /fixtures; nothing here needs root.
RUN useradd --create-home --uid 1000 rig
USER rig

ENTRYPOINT ["volunteer-rig-seed"]
