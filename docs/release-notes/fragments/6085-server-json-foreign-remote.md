## The MCP registry listing can be published again

`server.json` declared the hosted endpoint `https://mcp.bernstein.run/mcp` as a
remote. The registry keys a remote URL to one server name, and that URL is
published by the `bernstein-mcp` listing, so the registry refused the v3.20.0
submission with HTTP 400. The listing now offers the PyPI and OCI packages only,
and a test fails if a `remotes` key returns. The hosted endpoint stays
documented in `docs/mcp/server.md`.
