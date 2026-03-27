# Bernstein demo tape — generates docs/assets/demo.gif
# Run with: vhs docs/assets/demo.tape  (from project root)
# Requires: vhs (brew install vhs)

Output docs/assets/demo.gif

# ── Display settings ──────────────────────────────────────────────────────────
Set Shell         "bash"
Set FontSize      14
Set FontFamily    "JetBrains Mono"
Set Width         860
Set Height        480
Set Theme         "Dracula"
Set Framerate     24
Set PlaybackSpeed 0.85
Set TypingSpeed   50ms
Set WindowBar     "Rings"
Set Padding       16
Set BorderRadius  8

# ── Hidden setup — alias bernstein and set clean prompt ───────────────────────
Hide
Type `export PS1="$ " && alias bernstein="bash docs/assets/demo-runner.sh"`
Enter
Sleep 300ms
Show

Sleep 500ms

# User types the goal command
Type `bernstein -g "Add auth, tests, and docs"`
Sleep 800ms
Enter

# Let the full demo play out (~12s with all sleeps)
Sleep 15s

# Pause at summary so viewers can read it
Sleep 2s
