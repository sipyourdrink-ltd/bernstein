---
pr: 6316
kind: fix
area: templates
---

- Role task prompts, the completion-contract and prompt partials, and the manager system prompt no longer hardcode `http://127.0.0.1:8052`. They use `{{SERVER_URL}}`, which the spawner fills from `BERNSTEIN_SERVER_URL` or the run's port file, so agents on remote workers or non-default ports call the right server. The manager task-API skill reference now points to the base URL in the manager's system prompt.
