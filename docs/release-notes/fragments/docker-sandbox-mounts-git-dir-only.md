## Docker sandbox sessions mount only the repository's git dir

A `docker` sandbox session used to bind-mount the whole host working tree
read-only at `/host-repo`, so the in-container `git clone` had a source. Now
only the repository's git dir is mounted, read-only, at `/host-repo/.git`. The
clone works as before. The host working tree is no longer visible inside the
container: `.sdd/` (agent identity, tokens, approval records, runtime state),
untracked files and uncommitted edits stay on the host.

Every session container now also starts with `cap_drop=["ALL"]` and
`no-new-privileges:true`. An image that runs as root inside the container is
then still bound by host file modes on the mount.

For a linked worktree (`.git` is a file), the main repository's git dir is
mounted and the requested branch is checked out by name. A repo path without a
usable git dir now fails session creation before a container starts, instead
of failing at `git clone` inside it.

**Behaviour changes.**

- Anything that needs a Linux capability inside the session container stops
  working, for example `apt-get install` at run time. Bake those packages into
  the image.
- Untracked files in the host checkout were never part of the clone, and that
  is unchanged. Only the path the agent could read them through is gone.
- A new `user` backend option sets the container user. By default the image's
  own `USER` applies (`bernstein`, uid 1000, in the project image).

See "What a `docker` session can see" in `docs/architecture/sandbox.md`.
