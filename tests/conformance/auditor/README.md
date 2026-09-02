# Auditor conformance suite

One recorded run, one exported bundle, and 21 questions that must be
answerable **from the bundle alone** by someone who never had access to
the machine that produced it.

The score is `n/21`. It is a progress instrument, not a claim: twenty of
the twenty-one questions have no vector yet, and the score says so.

## The scenario

> A person starts agent A. A delegates part of the work to sub-agent B.
> B calls a tool served over MCP. That tool reads a file marked
> sensitive. B sends content to an external model endpoint. The endpoint
> returns output. A uses that output to take an action that changes the
> repository.

## What is here

| Path | What it is |
|---|---|
| `recorder.py` | Drives the scenario through the production writers and exports the bundle. |
| `fixture/bundle/` | The recorded export. All a vector is allowed to read. |
| `fixture/trust/` | The operator's public key, held **outside** the bundle because an auditor receives it out of band. |
| `bundle.py` | `BundleReader` - every lookup is containment-checked, so a vector cannot reach `.sdd/`. |
| `offline.py` | Runs `verify_cli/` in a subprocess with no `bernstein` on the path and an audit hook that denies sockets. |
| `questions.py` | The 21 questions. The score's denominator. |
| `scoreboard.py` | Pytest plugin that records which questions the run answered. |
| `test_vectors.py` | The vectors. One today: question 17. |
| `test_harness.py` | Holds the instrument honest; answers no question. |

## Commands

```
# Re-record the scenario and rewrite fixture/ (never edit it by hand)
uv run python scripts/auditor_conformance.py regenerate

# Run the vectors and print the score
uv run python scripts/auditor_conformance.py score
#   -> auditor conformance: 1/21

# Run the whole suite, harness included
uv run pytest tests/conformance/auditor -q
```

## Why the fixture is committed

The point of the instrument is that the bundle is checked away from the
run that produced it. Committing the export makes every vector read the
same bytes an auditor would be handed, and makes a change to the export
visible in review.

The run receipt is byte-identical across recordings - the projection it
signs excludes wall-clock fields, the spine timestamps are pinned, and
the signing key is a fixed seed - so
`test_committed_fixture_is_a_recording_of_the_scenario` compares it byte
for byte against a fresh recording. The HMAC audit chain stamps real
wall-clock time, so the audit receipt and the Article 12 pack differ
between recordings in their timestamps; those are compared structurally.

The key material here is fixed test material. It is not, and must never
become, operator key material.

## The remaining twenty

Each group of questions lands in its own slice: attribution (1, 2, 7,
14), authority (3, 4, 5, 21), policy and approval (6, 11, 12, 13), data
and endpoints (8, 9, 10), integrity and independence (15, 16, 18, 19,
20). A slice adds vectors to `test_vectors.py` and the score moves. A
weak assertion that passes is worse than an honest failure: it hides
exactly the gap this suite exists to measure.
