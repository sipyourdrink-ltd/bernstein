## An integration index, naming each system the way you name it

Someone evaluating whether Bernstein fits their environment searched the
tracker for `Okta`, `SCIM`, `Vault`, `OpenTelemetry` and found nothing, while
the work was open, milestoned, and in several cases already shipped. A
documented capability that cannot be found by the words people use to look for
it reads as absent.

`docs/integrations.md` maps directories and IdPs, secret stores, policy
engines, workload identity, telemetry, agent protocols and inference endpoints
to what is shipped (with the module and its test), what is open (with the issue
number), and what is deliberately not planned (with the reason).

It carries a column such pages usually omit: **wired** — whether anything in a
running system reaches the module by an import edge, rather than merely naming
it. That distinction is doing real work here. Vault/AWS/1Password credential
injection, generic OIDC, the directory bridge, the SCIM adapter and the
OPA/Cedar policy hook all ship with tests, and none of them is reached: the
references are an alias entry in `core/__init__.py`'s redirect map, prose in a
neighbouring docstring, or a `TYPE_CHECKING` import from another unreachable
module. Listing those as plainly "shipped" is the misreading the page exists to
prevent.

`tests/unit/test_integrations_index.py` resolves every path the page cites, so
a row naming a module that has moved fails rather than quietly becoming a claim
about something that used to be true (#5023).
