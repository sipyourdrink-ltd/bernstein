Declare `quality_gates: {enabled: false}` in the `bernstein demo` and
`bernstein demo --flask-todo` (`quickstart`) project seeds (both bundled
templates and inline wheel fallbacks) so `run_started` journals the demo's
actual merge-gate policy and `bernstein trace export` can compute
`policy.bundle_hash` out of the box without relaxing fail-closed refusal on
unconfigured runs.
