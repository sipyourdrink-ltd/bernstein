## Runtime key material at the repository root is now gitignored

A run whose root is the checkout itself, rather than `.sdd/`, writes its
state into `auth/`, `a2a/` and `default/` at the repository root. `.gitignore`
covered the `.sdd/` forms and not these, so `auth/agent_identity_jwt_secret`
and the private half of the a2a lineage signing keypair sat in the working
tree where `git add -A` would stage them.

`auth/` is already in `_MERGE_DENY_PREFIXES`, so the project had decided
these paths must never reach `main` and enforced it at merge time. The merge
gate is the last line, though: a key committed to a branch and pushed is
published before any gate sees it, and a history rewrite does not fully
retract it. The three root-level directories are now ignored, anchored with a
leading `/` so nested directories of the same name are untouched.
