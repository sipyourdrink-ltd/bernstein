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

# ── 1. Mint the receipt keypair. Private key never leaves $WORK. ────────────
KEY="$WORK/demo-receipt.key.pem"
openssl genpkey -algorithm Ed25519 -out "$KEY"
openssl pkey -in "$KEY" -pubout -out "$OUT/run-receipt.pub.pem"

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
text = "".join(
    json.loads(line)[2]
    for line in open(sys.argv[1], encoding="utf-8").read().splitlines()[1:]
    if line.strip()
)
m = re.findall(r"Project left at: (\S+)", re.sub(r"\x1b\[[0-9;]*m", "", text))
if not m:
    sys.exit("error: demo output never printed 'Project left at:'")
print(m[-1])
PY
)"
RECEIPT="$(find "$PROJECT/.sdd/runs" -name run-receipt.json | sort | tail -1)"
[ -n "$RECEIPT" ] || { echo "error: no run-receipt.json under $PROJECT/.sdd/runs - was receipt signing configured?" >&2; exit 1; }
cp "$RECEIPT" "$OUT/run-receipt.json"

# ── 4. Segment 2: verify the committed receipt offline, on camera. ──────────
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

# ── 5. Join the segments into the published cast. ───────────────────────────
python3 - "$WORK/seg1.cast" "$WORK/seg2.cast" "$OUT/demo.cast" <<'PY'
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
SPEED="$(python3 - "$OUT/demo.cast" <<'PY'
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
    "$OUT/demo.cast" "$OUT/demo.gif"

echo
echo "published to docs/assets/demo-run/:"
ls -la "$OUT"
echo
echo "verify what was just published (same command the README shows):"
echo "  uv run bernstein verify receipt docs/assets/demo-run/run-receipt.json --public-key docs/assets/demo-run/run-receipt.pub.pem"
