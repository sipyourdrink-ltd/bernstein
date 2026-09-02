# The auditor conformance suite

One recorded scenario, one exported bundle, and 21 questions that must be
answerable **from the bundle alone**, by a process with no access to the
machine that produced it.

Most questions do not have an answer yet. Each one that does not is a strict
`xfail` naming the work that would change that, so this directory doubles as a
dependency map over the open evidence work. A weak assertion that passes is
worse than an honest failure: it hides exactly the gap this exists to measure.

## The scenario

> A person starts agent A. A delegates part of the work to sub-agent B. B calls
> a tool served over MCP. That tool reads a file marked sensitive. B sends
> content to an external model endpoint. The endpoint returns output. A uses
> that output to take an action that changes the repository.

`scenario.py` performs it through the writers production uses - `AuditLog` and
`emit_run_audit_event` for the HMAC chain, `EventJournal` for the run journal,
`LineageSpine` and `LineageWriter` for lineage - and exports the bundle through
`build_receipt`, `build_run_receipt` and `assemble_from_run`. No evidence here
is hand-written.

## The score

```
uv run python scripts/auditor_scoreboard.py
```

Prints `asked n/21, answered m/21` and the per-question outcome. Its exit code
is pytest's, so a question that used to be answered and no longer is fails the
run.

## Regenerating an inspectable fixture

The suite rebuilds the scenario per session into a temporary directory; nothing
is checked in. A committed bundle would keep answering questions about the code
that produced it rather than the code at HEAD, and the whole instrument is a
claim about HEAD.

To get a copy you can open:

```
uv run python -m tests.integration.conformance.auditor.scenario --out /tmp/auditor-bundle --force
```

`/tmp/auditor-bundle/workspace` is the machine the run happened on;
`/tmp/auditor-bundle/bundle` is everything the auditor is handed.

## Rules for a vector

- Read **only** the exported bundle, through `BundleReader`. It refuses any
  path that resolves outside the bundle, so a vector cannot reach `.sdd/`, the
  source tree, or the workspace even by accident.
- Verification leaves the pytest process: shell out to `verify_cli/` under the
  `isolated_python` interpreter, which has `cryptography` and `cbor2`, no
  `bernstein`, and no sockets.
- Declare the question with `@pytest.mark.auditor_question(N)`. `N` must be
  registered in `questions.py`; two vectors cannot claim the same question.
- Name the test for the question, not the mechanism.
- If the answer cannot be produced today: `xfail(strict=True)`, a reference to
  the issue that would fix it, and a note naming the missing field. Do **not**
  add evidence fields to make a vector pass - the honest `xfail` is the
  deliverable.

## Layout

| File | What is there |
|---|---|
| `questions.py` | the 21 questions; the scoreboard's denominator |
| `scenario.py` | the run, the export, and the regeneration command |
| `bundle_reader.py` | the bundle-only reader and its boundary |
| `isolation.py` | the stranger's interpreter: no `bernstein`, no network |
| `conftest.py` | session fixtures and the question bookkeeping |
| `test_harness.py` | the instrument's own guarantees (no question numbers) |
| `test_vectors_integrity.py` | question 17, the worked example |
| `test_scoreboard.py` | what `n/21` means |
