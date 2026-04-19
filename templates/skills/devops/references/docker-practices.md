# Docker image hardening

- Start from a slim base (`python:3.12-slim`, `alpine` when libc compat OK).
- Pin base images by digest, not tag.
- Run as a non-root user; create `appuser` in the image.
- Use multi-stage builds; final image contains only runtime artefacts.
- Copy `pyproject.toml` / `uv.lock` separately from source to maximise
  build-cache reuse.
- Do NOT bake secrets — inject via runtime env or a mounted file.
- Add `HEALTHCHECK` instructions for long-running services.
- Scan the final image (`trivy image`, `grype`) in CI.

## Compose / Kubernetes
- Declare readiness and liveness probes separately.
- Set resource requests and limits; missing limits let one container
  starve the node.
- Prefer rolling updates with `maxUnavailable: 0` for stateless services.
