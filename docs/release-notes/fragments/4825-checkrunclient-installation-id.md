## `CheckRunClient` no longer takes an `installation_id`

The constructor accepted an `installation_id` it stored and never used:
authentication is delegated to the `gh` CLI, and since #4816 `_configured`
ignores it too. A parameter that looks load-bearing but isn't had already
misled a caller once. It is gone from the constructor now; the two
`volunteer-verify.yml` call sites that passed `None` were updated, and the
docstring states the real contract — repo slug in, auth from the `gh`
environment (#4825).
