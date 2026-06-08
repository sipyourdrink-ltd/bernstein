# Bernstein web UI

`v2.0.0` ships a minimal web UI. It is a side surface; the core orchestrator stays the priority.

## Run it

```bash
bernstein gui serve               # http://127.0.0.1:8052/ui/
bernstein gui serve --dev         # expects `npm run dev` on :5173
bernstein gui serve --minimal     # skip the full /api/v1/* surface
```

The Vite bundle is committed under `src/bernstein/gui/static/`, so wheel installs work without a Node toolchain.

## Surface

![Bernstein web UI — Tasks view](assets/webui-tasks.png)

Top-level tabs:

- **Tasks**
- **Agents**
- **Approvals**
- **Audit**
- **Costs**
- **Fleet**
- **Settings**

Per-task drawer:

- **Summary**
- **Logs** - SSE + ANSI + virtualised + search + level filters
- **Diff** - split / unified, syntax highlight, copy + `.patch`
- **Gates** - status buckets, auto-expand failures, polling
- **Deps** - upstream / downstream graph
- **Trace** - `.sdd/traces/` timeline + filter chips + search

## Release notes

Full v2.0.0 release notes: [docs/release-notes/v2.0.0.md](release-notes/v2.0.0.md).

## Contributing

Tracked in [issue #1262](https://github.com/sipyourdrink-ltd/bernstein/issues/1262). Contributions are welcome.
