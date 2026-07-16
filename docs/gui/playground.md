---
title: Zero-cost playground
description: Use bernstein run --idle plus a sibling fixture directory to develop the GUI without burning LLM tokens.
tags:
  - gui
  - dev
  - mock
---

# Zero-cost playground

Goal: run the GUI against a real Bernstein server with realistic task / agent data, without spending API tokens.

## Why

The mock adapter (`src/bernstein/adapters/mock.py`) supports an **idle mode** that sleeps each spawned agent for a randomized interval instead of calling any LLM. Combined with a sibling fixture repo, this gives you a continuous stream of mock tasks and agents that keep the GUI populated.

## Layout

```
<your workspace>/
├── bernstein/             ← this repo (source checkout)
└── bernstein-playground/   ← sibling fixture directory (mock state lives here)
```

The fixture directory is any empty directory next to your checkout: `mkdir ../bernstein-playground` is enough. `bernstein init` inside it creates the runtime state on first run.

The playground is a separate working tree so the mock orchestration writes to its own `.sdd/` and never pollutes the source repo's runtime state.

## Dev loop

Two terminals.

### Terminal 1 - mock orchestration

```bash
cd ../bernstein-playground
bernstein run --idle
```

Effect:

- Forces every agent spawn through the mock adapter.
- Sets `BERNSTEIN_MOCK_IDLE=1`. Each mock agent sleeps `BERNSTEIN_MOCK_IDLE_MIN_S..MAX_S` seconds (default 15–120) and exits, then the orchestrator spawns a fresh one.
- Mutually exclusive with `--dry-run`. Defined in `src/bernstein/cli/run_bootstrap.py` (`--idle` option, lines ~842–1030).
- Cost: zero. No LLM calls.

### Terminal 2 - Vite dev server with HMR

```bash
cd <your checkout>/bernstein/web
npm install        # first time
npm run dev
```

Vite serves the SPA on `http://127.0.0.1:5173` and proxies `/api/*` to FastAPI on `:8052`.

If you'd rather hit the built bundle (no HMR), run `bernstein gui serve --dev` in this terminal instead and open `http://127.0.0.1:8052/ui/`.

## Tuning the idle interval

| Variable                    | Default | Effect                                       |
|-----------------------------|---------|----------------------------------------------|
| `BERNSTEIN_MOCK_IDLE`       | unset   | Set to `1` by `--idle`. Forces idle path.    |
| `BERNSTEIN_MOCK_IDLE_MIN_S` | `15`    | Lower bound of per-spawn sleep, in seconds.  |
| `BERNSTEIN_MOCK_IDLE_MAX_S` | `120`   | Upper bound of per-spawn sleep, in seconds.  |

For a denser task stream while iterating on the queue UI:

```bash
BERNSTEIN_MOCK_IDLE_MIN_S=2 BERNSTEIN_MOCK_IDLE_MAX_S=8 bernstein run --idle
```

## What you get

- Tasks page populated with rotating running / queued / done rows.
- Agents page shows live token meters from the mock adapter's synthetic counters.
- Approvals page exercises the queue + diff layout (mock approval requests if the playground seed includes them).
- Costs page reads zeros - mock adapter records no spend. To exercise the cost layout, switch one task off `--idle` and run a real adapter briefly.
