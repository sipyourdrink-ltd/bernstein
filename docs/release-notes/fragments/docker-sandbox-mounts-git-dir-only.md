## Docker sandbox sessions mount only the repository's git dir

A `docker` sandbox session used to bind-mount the whole host working tree
read-only at `/host-repo`, so the in-container `git clone` had a source. Now
only the repository's git dir is mounted, read-only, at `/host-repo/.git`. The
clone works as before. The host working tree is no longer visible inside the
container: `.sdd/` (agent identity, tokens, approval records, runtime state),
untracked files and uncommitted edits stay on the host.

For a linked worktree (`.git` is a file), the main repository's git dir is
mounted and the requested branch is checked out by name. A repo path without a
usable git dir now fails session creation before a container starts, instead
of failing at `git clone` inside it.

The clone now also works when the container user is not the owner of the
mounted git dir (a root image, or a uid other than the operator's): git used to
refuse the source as owned by another user, so on Linux hosts a repo session
only worked when the image's uid matched the host's. The clone runs with a
container-local git config (`/tmp/bernstein-clone.gitconfig`) that lists the
two mount paths under `safe.directory`; the image user's own git config is not
touched.

**Behaviour changes.**

- Untracked files in the host checkout were never part of the clone, and that
  is unchanged. Only the path the agent could read them through is gone.

See "What a `docker` session can see" in `docs/architecture/sandbox.md`.
