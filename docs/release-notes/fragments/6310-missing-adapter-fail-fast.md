---
pr: 6310
kind: fix
area: orchestrator
---

- A run started with no adapter (no `--cli`, no `BERNSTEIN_ADAPTER`, no `bernstein.yaml`) now exits non-zero at startup with the "no adapter configured" message, instead of the watchdog restarting the orchestrator and logging the FATAL every tick.
