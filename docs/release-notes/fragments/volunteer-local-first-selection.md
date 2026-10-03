## Volunteer local-first selection now selects a registered adapter, before the claim

With no adapter chosen and a certified local endpoint configured, volunteer mode used to select the adapter id `local`, which is not a registered adapter. The auth-basis gate then refused the run with `unknown auth_basis` - after the claim comment had already been posted, so the exact setup the feature targets ended in a claim followed by a release. Selection now returns `ollama` (a registered adapter whose contract pins `auth.basis: local`), and both selection and the auth-basis gate run before the claim comment, so a refused task leaves nothing public behind.

Related corrections in the same path:

- Certification receipts are looked up under the donor's project root (new `project_root` argument, default the working directory), where `bernstein doctor --endpoint` writes them. They were looked up under the per-task scratch workspace and never found.
- The `/models` probe runs only when no adapter was chosen and only against a local host (loopback, private range, `*.internal`, `*.local`, `*.svc`). `OPENAI_API_KEY` is no longer sent to any other host, over http or https, the probe uses a 5 s timeout outside the run budget, and a certified endpoint on a non-local host is no longer treated as local.
- `discover_default_model` returns `None` for `http.client` errors (truncated body, non-HTTP banner) and for a body that is not valid UTF-8, as its docstring already promised, so `run_claimed_task` no longer raises on a broken endpoint.
- The docs state what selection does: it names the adapter id the auth-basis gate sees; the launched process is still built by the caller's `agent_argv`.

## Autopilot loop: bounded claim scan, stop honoured, dry run releases

`AutopilotLoop` claimed and released the same task forever when a source re-offered a released task, and never looked at `request_stop()` while doing so. The scan now stops when a released task is offered again or a stop is requested. `dry_run=True` released nothing after taking a claim at the source; it now releases it.
