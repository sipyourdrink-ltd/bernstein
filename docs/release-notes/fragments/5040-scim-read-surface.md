## Agent principals are readable over SCIM 2.0

`GET /identities` answered only for a caller that already speaks this server's
own API, so an identity system that wanted to read which agent principals exist
needed an adapter written for it first. The server now serves the read half of a
SCIM 2.0 service provider under `/scim/v2` (and its `/api/v1` mirror):
`ServiceProviderConfig`, `Schemas`, `ResourceTypes`, and `GET /Users` with the
single-resource `GET` for each, projecting the existing agent identity store into
SCIM `User` resources. The discovery documents report only what is mounted —
`patch`, bulk and `/Groups` are absent rather than advertised, and an unsupported
`filter` is refused with `501` instead of being silently ignored — so a client
that follows them is never sent into a request that can only fail. Access is
scoped separately from every other route: reads need `scim:read`, granted to
`admin`, and `scim:write` is declared but held by no role, so a write route added
later cannot inherit read authority. No create, update or delete is mounted
yet (#5040).
