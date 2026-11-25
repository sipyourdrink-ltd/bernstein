Harden atomic file-write permissions and volunteer GitHub preflight checks:

- `write_atomic_bytes` now always stages files at `0o600`, validates any wider
  explicit mode via `fchmod`/`chmod`, and rejects group/world-writable or
  special-bit modes (`0o022` / outside `0o777`).
- `ScheduleStore` and `ScheduleSupervisor` now persist schedule specs and fire
  receipts at owner-only `0o600` instead of `0o666`, and
  `a2a_cmd._reissue_expired_card` clamps preserved mode bits to `0o644`.
- Volunteer open-source preflight now parses GitHub repository URLs with a
  strict anchored HTTPS `github.com/<owner>/<repo>[.git]` check (rejecting
  extra segments, dot segments, queries, fragments, userinfo, and custom ports)
  and routes both visibility and license API requests through
  `ensure_public_http_url` and `StrictHTTPRedirectHandler`.
