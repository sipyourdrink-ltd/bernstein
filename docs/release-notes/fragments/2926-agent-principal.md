## One agent principal type behind JWT and Ed25519 credential formats

:class:`bernstein.core.identity.AgentPrincipal` is the single identity an authority decision is checked against. Two adapters project the JWT-store record (:func:`principal_from_agent_identity`) and the signed capability card (:func:`principal_from_identity_card`) onto it. Both resolve to principals with the same id and can be joined with :meth:`AgentPrincipal.merge`. :class:`CredentialRef` carries a format-tagged reference (jti, token hash, or card hash) — never the secret. #2926
