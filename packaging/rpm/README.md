# RPM packaging

The RPM channel builds `bernstein` from `packaging/rpm/bernstein.spec` and
publishes it to the
[Copr project](https://copr.fedorainfracloud.org/coprs/alexchernysh/bernstein/).
The build is driven by `scripts/build_copr_srpm.py` and watched by
`scripts/copr_build_watch.py`; the release operations are documented in
`docs/operations/release.md`.

## Chroot gating policy

Copr reports one state for the whole build, and that state is `failed` when
any single chroot failed. `scripts/copr_build_watch.py` re-reads a `failed`
aggregate per chroot and lets only the *gating* chroots decide the release
verdict. A chroot is non-gating when its name matches a marker in
`NON_GATING_CHROOT_MARKERS`; a failure confined to non-gating chroots passes
the job with a `::notice::` naming the failed chroot(s) and does not hold the
release.

### Decision: fedora-45 is non-gating

`fedora-45` is treated as non-gating (advisory), the same as `fedora-rawhide`.

**Rationale.** fedora-45 is a pre-release Fedora whose interpreter (Python
3.15) lacks wheels for `cbor2` and `grpcio` on PyPI. Its build failure is
upstream churn, not a release defect. This matches the existing rawhide
pattern: pre-release chroot breakage is expected and costs more to chase than
it catches.

**When to revisit.** When fedora-45 goes stable and wheels for `cbor2` and
`grpcio` are available on PyPI, remove `fedora-45` from
`NON_GATING_CHROOT_MARKERS` in `scripts/copr_build_watch.py` so it becomes a
gating chroot again.

### Options considered

1. **Fix forward** — make `cbor2` and `grpcio` build from source on Python
   3.15. Higher risk: it adds build-time source compilation to the RPM closure
   and pins the channel to a pre-release interpreter.
2. **Scope chroots** — treat pre-release failures as advisory, matching the
   existing rawhide pattern.

Scope chroots was chosen: it is lower risk, it matches the established rawhide
pattern, and the breakage is temporary and upstream.
