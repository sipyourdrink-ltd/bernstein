# Regenerating the README demo recording

The moving image on the README front page is a recording of a **real
`bernstein demo` run**, published together with the evidence that it is
one. Everything lives in `docs/assets/demo-run/`:

| File | What it is |
|---|---|
| `demo.cast` | asciinema v2 recording of the run shown in the gif |
| `demo.gif` | rendered from `demo.cast` with [agg](https://github.com/asciinema/agg) |
| `run-receipt.json` | the signed run receipt produced by **that exact run** |
| `run-receipt.pub.pem` | the Ed25519 public key that pins the receipt signature |

`tests/unit/test_demo_receipt_fixture.py` verifies the committed receipt
against the committed public key on every CI push - and demonstrates
that a tampered copy fails with exit 2 - so the published evidence
cannot silently rot.

## One command

```bash
scripts/record_demo.sh
```

Run it from anywhere inside the checkout. It:

1. mints a fresh Ed25519 keypair (the private key lives in a temp dir
   wiped at exit; only the public key is published);
2. records a real `bernstein demo` orchestration under asciinema with
   `BERNSTEIN_AUDIT=1` and receipt signing configured, so the run being
   filmed is the run that signs the receipt;
3. copies that run's `run-receipt.json` out of the demo project (the
   `.sdd/` path it lands in is never committable);
4. records the offline verification of the just-published receipt as
   the closing segment, so the frame the gif loop rests on is the
   proof;
5. joins the casts and renders the gif with agg (derived playback
   speed keeps one loop near 12 s, idle compression, 3 s hold on the
   last frame).

## Prerequisites

- `asciinema` - `uv tool install asciinema` (or `pipx install asciinema`)
- `agg` - a single static binary from
  [github.com/asciinema/agg/releases](https://github.com/asciinema/agg/releases)
- `openssl`, `uv`, `git` - already required for development

## The honesty contract

Everything **inside** the terminal is output the real CLI emitted. The
only editing is the pre-typed `$ …` prompt line before each command
(what any recorded shell session shows), playback speed, idle
compression, and the closing hold - all applied at render time, never
to the recorded bytes. Window chrome and theme are rendering concerns;
nothing drawn inside the terminal is synthesised. If the CLI's real
output changes, regenerating the demo reruns the real CLI, so the
change shows up in the diff instead of drifting silently.

After regenerating, commit the four files in `docs/assets/demo-run/`
together, and run the fixture test locally:

```bash
uv run python -m pytest tests/unit/test_demo_receipt_fixture.py -q
```
