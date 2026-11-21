# python:3.13-slim
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

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

# This one-shot job reports the seed and fixture assertions through its exit status.
# Compose checks the long-running Gitea service before starting the seeder.
HEALTHCHECK NONE
ENTRYPOINT ["volunteer-rig-seed"]
