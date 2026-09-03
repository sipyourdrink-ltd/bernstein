## Declare the isolation your host already provides, instead of bypassing everything

An operator running the CLI inside a container or VM they control hits a
specific wall with the codex adapter: `--sandbox workspace-write` is built on
bubblewrap, bubblewrap needs an unprivileged user namespace, and a host that
already isolates the process has usually removed exactly that. Every command
the agent issued was refused while the run still exited 0.

The only way to drop that sandbox used to be the escalated dangerous-mode
strategy — one switch that removes the sandbox and the approval surface
together and records no reason for it. There is now a narrower statement:

```bash
bernstein config set host_isolation_tier container
bernstein config set host_isolation_evidence "read-only rootfs, cap-drop ALL, no-new-privileges"
```

Both keys resolve through the normal precedence chain (environment > project >
user > default) and have env vars of their own, `BERNSTEIN_HOST_ISOLATION_TIER`
and `BERNSTEIN_HOST_ISOLATION_EVIDENCE`. `container` and `vm` are a boundary
that replaces what bubblewrap would have supplied, so codex spawns without its
own sandbox; `process` and `none` are not, so it stays. A tier outside those
four is rejected and the sandbox stays on rather than being dropped on a typo.

Every declaration that reaches an adapter is written to the HMAC audit chain as
a `sandbox.host_isolation_declared` event recording the tier, the evidence, the
config layer the declaration came from, and whether the sandbox was
consequently dropped — so the posture a run used can be reconstructed
afterwards from the chain rather than inferred. Adapters with no sandbox of
their own are unaffected. (#5341)
