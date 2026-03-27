# Bernstein — 2-Minute YouTube Demo Script

**Title:** Bernstein: One Command, Multiple AI Agents — Multi-Agent Orchestration for CLI Tools
**Upload to:** YouTube, then update the badge URL in README.md

---

## Screen recording checklist

- [ ] Terminal: dark theme, 14pt monospaced font, ~100 cols wide
- [ ] Run a real project: use `examples/quickstart/` (Flask app with pre-configured `bernstein.yaml`)
- [ ] Have Claude Code (or Codex CLI) installed and authenticated
- [ ] Run `bernstein init` first to show setup, then `bernstein -g "Add JWT auth, tests, and docs"`
- [ ] Keep `bernstein live` open in a split pane to show the TUI dashboard

---

## Voiceover

### 0:00–0:15 — Hook

> "One command. Multiple AI agents. Your code ships while you take a break."
>
> "This is Bernstein. You give it a goal — it breaks the work into tasks, hires agents to run in
> parallel, and commits the result. Let me show you."

*[Screen: terminal. Type `bernstein -g "Add JWT auth, tests, and docs"`]*

---

### 0:15–0:45 — Live run with dashboard

> "Bernstein parses the goal and creates three tasks automatically — backend, QA, docs."
>
> "It spawns one agent per task. Here they're all running at the same time."

*[Screen: split pane — left terminal output, right `bernstein live` TUI dashboard showing
agents as active rows with live status updates]*

> "The orchestrator is pure Python — no LLM on coordination. No tokens wasted deciding who does what."

---

### 0:45–1:15 — Results

> "Forty-seven seconds later, the janitor runs verification."

*[Screen: janitor output — '✓ Tests pass (12/12)', '✓ No regressions']*

> "All tests pass. No regressions against the 124 existing tests. Changes committed."

*[Screen: `git log --oneline -5` showing three new commits]*

> "Here's the actual diff — JWT middleware, 12 tests, API docs. All written by agents."

*[Screen: `git diff HEAD~3` or GitHub diff view — real code, not a mockup]*

---

### 1:15–1:45 — Cost and agent choice

> "Total cost: forty-two cents. Heavy model on architecture, cheap model on tests and docs."

*[Screen: `bernstein cost` output — per-task breakdown]*

> "Bernstein works with any CLI agent — Claude Code, Codex, Gemini, Qwen. Mix them in a single run.
> No API key plumbing, no SDK wrappers. If it runs in a terminal, Bernstein can use it."

---

### 1:45–2:00 — Install CTA

> "Install it now:"

*[Screen: terminal]*

```
pipx install bernstein
bernstein init
```

> "One command. Ship faster."

*[Screen: fade to logo + GitHub URL]*

---

## Upload notes

1. Thumbnail: dark terminal with the completion banner `✓ 3 tasks done  $0.42  47s`
2. Description: include `pip install bernstein` and the GitHub URL
3. Tags: `ai coding`, `multi-agent`, `claude code`, `codex`, `developer tools`, `python`
4. After uploading, replace the badge URL in `README.md`:
   ```
   <!-- TODO: replace the URL below with the actual YouTube link once uploaded -->
   ```
