## Agent identity has one type and one module namespace

`agent_identity.py` existed twice — under `core/agents/` holding the JWT-backed
identity store, under `core/security/` holding the Ed25519-signed card — with
unrelated types that both answered "who is this agent", so an authentication
event and a delegation hop shared no id space. Both modules move to
`core/identity/agent_jwt.py` and `core/identity/agent_card.py`, and both
credential formats now resolve to one `AgentPrincipal`; a delegation receipt
records its issuer and subject in that same id space. The old import paths are
tombstoned for one release: importing either raises `ImportError` naming the
successor (#5097).
