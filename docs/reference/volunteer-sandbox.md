# Volunteer sandbox profile

Volunteer tasks are hostile input by default. The issue text comes from a
repository the donor does not control, the patch is written by a model reading
that text, and the whole thing executes on a stranger's laptop. The `volunteer`
sandbox profile is the boundary that makes the rest of the program safe to run.

## The profile is derived, not chosen

"The sandbox was hardened" is not a checkable statement. So the profile is a
pure function of the project's [manifest](volunteer-manifest.md) plus the
donor's own limits, and it is content-addressed. Its digest is what a result
receipt records, and it binds the manifest digest it came from:

```
manifest bytes → manifest digest → sandbox profile digest → receipt bundle
```

A maintainer recomputes the manifest digest from the repository at the commit
the submission names, rebuilds the profile from it, and compares. A profile
that does not reproduce means the run was not contained the way the receipt
says it was.

The consequence worth stating plainly: **a donor cannot loosen containment and
still produce a verifying receipt.** There is no flag for it, because a flag
would have to appear in the digest, and a digest that says "egress was open" is
not a digest anybody accepts.

## What the boundary is

| Control | Rule |
|---|---|
| Backend | microVM where the host has one; container-with-user-space-kernel as fallback. A plain container is refused unless the project manifest **and** the donor both accept it, and that acceptance is in the digest. |
| Egress | Deny-all, plus the manifest's `egress_allowlist`, plus the package registries the gates need. |
| Credentials | Nothing from the host environment reaches the sandbox. The environment is built from the profile alone. |
| Resources | CPU, memory and wall clock, floored to the tighter of what the project asks and what the donor allows. |

## Backend selection

Order of preference: `microvm`, then `container-userns`, then `container`.

A project whose manifest says `"sandbox": "microvm"` gets a microVM or gets a
refusal — the field is a floor, and a floor that bends is decoration.

A plain container shares the host kernel. It runs only when both the project
accepts it and the donor has said so. A donor who never said "you may share my
kernel with a stranger's code" has not said it by installing the software.

## Egress, and how far the enforcement actually goes

Naming hosts in a list enforces nothing by itself. Two mechanisms carry it:

**No reachable host at all** becomes a sandbox with no network interface. That
one is complete.

**Some reachable hosts** become a pinned resolver: the allowlisted names are
resolved on the host side and passed as static entries, and the sandbox is
given a resolver that answers nothing. Names outside the list do not resolve.

The limit, stated rather than implied: **a connection to a raw IP address does
not consult DNS**, so DNS pinning does not stop it. Closing that needs
packet-level filtering — the microVM backend's own network policy, or a
filtering proxy the sandbox is forced through. Until that lands, a project
whose threat model includes exfiltration to a hard-coded address should declare
an empty `egress_allowlist` and vendor its dependencies, which turns the
network off completely.

Host-side DNS resolution is the caller's job rather than the profile's: a
profile has to rebuild identically on a machine with different DNS answers, or
its digest would depend on the weather.

## Credentials

The sandbox environment is built from the profile, never filtered from
`os.environ`. A filter over the host environment is only as good as its
denylist, and the thing on the other side of this boundary is reading a
stranger's issue text; an allowlist that starts from nothing has no denylist to
be wrong about.

The permitted names are `BERNSTEIN_VOLUNTEER`, `CI`, `HOME`, `LANG`, `LC_ALL`,
`PATH`, `TMPDIR` — and `HOME` points inside the workspace, not at the donor's
real home, which is where credentials live.

Adapter credentials are absent by construction rather than by exclusion: they
belong to the adapter process, which runs outside this boundary.

## Wall clock

A donor lends their machine for a bounded time, and the bound has to be real.
Exceeding it terminates the **process tree**, not just the process — gate
commands fork workers, and killing only the parent leaves those running on a
stranger's machine with nothing watching them.

The kill is recorded: whether the cap fired, whether SIGTERM was enough, and
whether SIGKILL was needed after the grace period. A command that needs SIGKILL
is trapping signals or stuck uninterruptibly, and a project's maintainer should
know that about their own gates.

On Windows there is no process group to signal, so only the direct child is
terminated. A Windows donor running a gate that forks can leave orphans; that
is a known gap rather than a hidden one.

## Refusals

A refusal is a normal outcome on a mixed donor fleet, not an error condition.
Each carries a stable machine-readable reason so a runner can record it without
parsing prose:

| Reason | Meaning |
|---|---|
| `microvm_required_but_unavailable` | The project demands a microVM; this host has none. |
| `plain_container_needs_dual_opt_in` | Only a plain container is available and the donor has not accepted one. |
| `no_acceptable_backend` | Nothing on this host is acceptable for volunteer work. |
| `wall_clock_below_floor` | The effective time budget is under a minute. |
| `memory_below_floor` | The effective memory ceiling is under 256 MB. |

Refusals need the same structure as successes, or they become log lines nobody
counts and "why did nothing ever run on my machine" becomes unanswerable.

## Source

`src/bernstein/core/volunteer/sandbox_profile.py`,
`src/bernstein/core/volunteer/wall_clock.py`.
