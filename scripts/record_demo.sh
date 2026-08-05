#!/usr/bin/env bash
# Regenerate the README demo recording from a REAL run (issue #3426).
#
# Publishes, side by side under docs/assets/demo-run/:
#
#   demo.cast            asciinema v2 recording of the run shown in the gif
#   demo.gif             rendered from demo.cast with agg
#   run-receipt.json     signed run receipt produced by the recorded run
#   run-receipt.pub.pem  Ed25519 public key that pins the receipt signature
#
# The recording is two real terminal segments joined at render time:
#
#   1. `bernstein demo` - a full orchestration run with mock agents (no API
#      key), audit enabled, and receipt signing configured, so the run being
#      shown is the run that produced the committed receipt.
#   2. `bernstein verify receipt docs/assets/demo-run/run-receipt.json
#      --public-key docs/assets/demo-run/run-receipt.pub.pem` - the same
#      offline check a reader can run against the committed files, held as
#      the closing frame.
#
# Honesty contract: everything inside the terminal is output the real CLI
# emitted. The only editing is the pre-typed prompt line before each command
# (what a shell session shows anyway), playback speed, idle compression, and
# the closing hold - all applied at render time, never to the bytes.
#
# A fresh Ed25519 keypair is minted per regeneration; the private key lives
# in a temp dir and is deleted at exit - only the public key is committed.
#
# Requires: asciinema (`uv tool install asciinema` or `pipx install
# asciinema`), agg (github.com/asciinema/agg/releases), openssl, uv.
# Run from anywhere inside the checkout: scripts/record_demo.sh

set -euo pipefail

for tool in asciinema agg openssl uv python3 git; do
    command -v "$tool" >/dev/null || {
        echo "error: '$tool' is required (see header for install pointers)" >&2
        exit 1
    }
done

ROOT="$(git rev-parse --show-toplevel)"
OUT="$ROOT/docs/assets/demo-run"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$OUT"

# ── 1. Mint the receipt keypair, STAGED. Private key never leaves $WORK, and
# the public key is not published yet either: a failed regeneration must
# never leave $OUT holding a new key next to an old receipt (the pair only
# moves together, after it verifies - see step 4).
KEY="$WORK/demo-receipt.key.pem"
PUB="$WORK/run-receipt.pub.pem"
openssl genpkey -algorithm Ed25519 -out "$KEY"
openssl pkey -in "$KEY" -pubout -out "$PUB"

# ── 2. Segment 1: the real run, recorded. ───────────────────────────────────
# The demo leaves its throwaway project on disk ("Project left at: …"), which
# is where the auto-signed receipt lands (.sdd/ is never committable, so the
# receipt is copied out to the published path below).
#
# The published recording must show a fully successful run - a failing take
# is discarded and re-recorded (the demo has some run-to-run variance), up
# to a bounded number of attempts. The demo's own exit code is the truth
# signal: 0 only when every seeded task succeeded.
cat > "$WORK/session-run.sh" <<SESSION
#!/usr/bin/env bash
set -uo pipefail
cd "$ROOT"
printf '$ bernstein demo\n'
sleep 0.4
BERNSTEIN_AUDIT=1 \
BERNSTEIN_RUN_RECEIPT_SIGNING_KEY_PATH="$KEY" \
BERNSTEIN_RUN_RECEIPT_SIGNING_KID=bernstein-demo-run-key \
uv run --frozen bernstein demo
echo "\$?" > "$WORK/demo-exit-code"
SESSION
chmod +x "$WORK/session-run.sh"

attempt=1
while :; do
    asciinema rec --quiet --overwrite --cols 100 --rows 30 \
        --command "$WORK/session-run.sh" "$WORK/seg1.cast"
    demo_rc="$(cat "$WORK/demo-exit-code" 2>/dev/null || echo 1)"
    [ "$demo_rc" = "0" ] && break
    if [ "$attempt" -ge 3 ]; then
        echo "error: bernstein demo did not fully succeed in $attempt takes (last exit ${demo_rc}); not publishing a failing run" >&2
        exit 1
    fi
    echo "take $attempt ended with exit ${demo_rc}; re-recording" >&2
    attempt=$((attempt + 1))
done

# ── 3. Pull the receipt of exactly that run out of the demo project. ────────
PROJECT="$(python3 - "$WORK/seg1.cast" <<'PY'
import json, re, sys

# Validate each event before use: an asciinema v2 event is
# [number, code, string]. Only OUTPUT events ("o") may contribute to the
# text the project path is parsed from - a malformed line fails with a
# named error instead of a leaked IndexError, and a non-output event
# (e.g. an input echo) can never supply the selected path.
chunks = []
for lineno, line in enumerate(open(sys.argv[1], encoding="utf-8").read().splitlines()[1:], start=2):
    if not line.strip():
        continue
    try:
        event = json.loads(line)
    except ValueError:
        sys.exit(f"error: invalid cast event at line {lineno}: not JSON")
    if not (isinstance(event, list) and len(event) >= 3 and isinstance(event[2], str)):
        sys.exit(f"error: invalid cast event at line {lineno}: expected [time, code, text]")
    if event[1] == "o":
        chunks.append(event[2])
text = "".join(chunks)
m = re.findall(r"Project left at: (\S+)", re.sub(r"\x1b\[[0-9;]*m", "", text))
if not m:
    sys.exit("error: demo output never printed 'Project left at:'")
print(m[-1])
PY
)"
# Build and sign the receipt post-hoc with the CLI's own offline path.
# The auto-written receipt only lands when the orchestrator finalizes
# gracefully, and a fast successful demo is torn down before that happens
# (SIGKILL after the 3s SIGTERM grace); `bernstein verify run` instead
# re-derives the receipt from the always-on journal and lineage spine the
# recorded run left on disk, which is also exactly the flow the README's
# "prove a run" section documents.
# .sdd/runs/ holds the timestamp-named orchestrator run alongside task-*
# per-task journals; a bare `ls | tail -1` picks a task journal (they sort
# after the digits) and signs a one-event receipt for the wrong journal.
RUN_ID="$(find "$PROJECT/.sdd/runs" -maxdepth 1 -mindepth 1 -type d -name '[0-9]*' -printf '%f\n' | sort | tail -1)"
[ -n "$RUN_ID" ] || { echo "error: no timestamp-named run under $PROJECT/.sdd/runs" >&2; exit 1; }
uv run --frozen bernstein verify run "$RUN_ID" \
    -w "$PROJECT" \
    --signing-key-path "$KEY" \
    --signing-key-id bernstein-demo-run-key \
    -o "$WORK/run-receipt.json"

# ── 3b. Verify the STAGED pair, then publish it as one unit. ────────────────
# The receipt and its public key only reach $OUT together, and only after
# the exact offline check a reader will run has succeeded against the
# staged copies - a failure anywhere above leaves the previously committed
# pair untouched (finding: a half-regenerated $OUT held a fresh key beside
# a stale receipt, which cannot verify).
uv run --frozen bernstein verify receipt "$WORK/run-receipt.json" --public-key "$PUB"
cp "$WORK/run-receipt.json" "$OUT/run-receipt.json"
cp "$PUB" "$OUT/run-receipt.pub.pem"

# ── 4. Segment 2: verify the just-published pair offline, on camera. ────────
# This must run against the real committed paths - the command shown in the
# recording is the command the README tells the reader to run.
cat > "$WORK/session-verify.sh" <<SESSION
#!/usr/bin/env bash
set -euo pipefail
cd "$ROOT"
printf '$ bernstein verify receipt docs/assets/demo-run/run-receipt.json \\\\\n    --public-key docs/assets/demo-run/run-receipt.pub.pem\n'
sleep 0.4
uv run --frozen bernstein verify receipt docs/assets/demo-run/run-receipt.json \
    --public-key docs/assets/demo-run/run-receipt.pub.pem
SESSION
chmod +x "$WORK/session-verify.sh"
asciinema rec --quiet --overwrite --cols 100 --rows 30 \
    --command "$WORK/session-verify.sh" "$WORK/seg2.cast"

# ── 5. Join the segments, staged. ───────────────────────────────────────────
python3 - "$WORK/seg1.cast" "$WORK/seg2.cast" "$WORK/demo.cast" <<'PY'
import json, sys

def load(path):
    lines = open(path, encoding="utf-8").read().splitlines()
    header = json.loads(lines[0])
    events = [json.loads(ln) for ln in lines[1:] if ln.strip()]
    return header, events

h1, e1 = load(sys.argv[1])
_, e2 = load(sys.argv[2])
offset = (e1[-1][0] if e1 else 0.0) + 0.7
joined = e1 + [[t + offset, kind, data] for t, kind, data in e2]
with open(sys.argv[3], "w", encoding="utf-8") as out:
    json.dump({"version": 2, "width": h1["width"], "height": h1["height"]}, out)
    out.write("\n")
    for ev in joined:
        json.dump(ev, out)
        out.write("\n")
PY

# ── 6. Render the gif: readable first frame, short loop, hold the verify. ───
# Speed is derived from the cast so one loop stays near the ~12 s budget
# regardless of how long the live run took; the last frame (the verify
# verdict) holds for 3 s so the loop rests on the proof.
SPEED="$(python3 - "$WORK/demo.cast" <<'PY'
import json, sys
events = [json.loads(ln) for ln in open(sys.argv[1], encoding="utf-8").read().splitlines()[1:] if ln.strip()]
duration = events[-1][0] if events else 1.0
print(max(1, round(duration / 9.0)))
PY
)"
agg --theme dracula \
    --font-family "DejaVu Sans Mono" \
    --font-size 28 \
    --idle-time-limit 1 \
    --speed "$SPEED" \
    --fps-cap 24 \
    --last-frame-duration 3 \
    "$WORK/demo.cast" "$WORK/demo.gif"

# Publish the recording last: cast and gif land together, after the render
# succeeded, so a failed render never leaves $OUT with a cast/gif mismatch.
mv "$WORK/demo.cast" "$OUT/demo.cast"
mv "$WORK/demo.gif" "$OUT/demo.gif"

echo
echo "published to docs/assets/demo-run/:"
ls -la "$OUT"
echo
echo "verify what was just published (same command the README shows):"
echo "  uv run bernstein verify receipt docs/assets/demo-run/run-receipt.json --public-key docs/assets/demo-run/run-receipt.pub.pem"
