# Self-update

`bernstein self-update` is a compatibility alias for the `bernstein self`
update lifecycle. The full operator surface — the signed release feed, the
chain-anchored advisory, the pre-install wheel verification, the pin, and the
receipted rollback — is documented in
[Updates: check, verify, apply](updates.md).

## Usage

```bash
bernstein self-update             # same as `bernstein self update`
bernstein self-update --check     # same as `bernstein self check-update`
bernstein self-update --rollback  # same as `bernstein self rollback`
bernstein self-update -y          # skip the confirmation prompt
```

| Flag | Default | Meaning |
|---|---|---|
| `--check` | off | Verify the signed release feed and seal an advisory; no install. |
| `--rollback` | off | Return to the previous receipted version; ignores `--check`. |
| `--yes`, `-y` | off | Skip the confirmation prompt. |

## What changed

The alias no longer queries PyPI for a version string, no longer prints
truncated GitHub release prose, and no longer installs an unpinned
`bernstein==<latest>`. It dispatches into the verified flow, which means:

- A **release trust root** must be installed, and a **signed release feed**
  configured — otherwise the command refuses rather than trusting a registry.
- No network call happens for update purposes without an explicit command or
  `BERNSTEIN_UPDATE_CHECK=1`; the air-gap profile disables the remote path
  entirely.
- The candidate's wheel hash is verified **before** install; a mismatch aborts.
- Updates refuse while a run is active or tasks are pending.
- Every install and rollback emits a receipt into the audit chain, so the
  rollback target comes from the receipt history rather than a plaintext
  `~/.bernstein/previous-version` breadcrumb.

See [Updates: check, verify, apply](updates.md) for configuration, the feed
format, air-gap operation, and troubleshooting.

## Source

`src/bernstein/cli/commands/self_update_cmd.py` (`self_update_cmd`, registered
as `bernstein self-update`; the group is registered as `bernstein self`).
