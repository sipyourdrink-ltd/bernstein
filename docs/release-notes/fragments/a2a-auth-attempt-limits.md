## Attempt limits for A2A credentials

`POST /a2a/v1/oauth/token` shares the authentication attempt budget and the
failed-credential backoff with the other login endpoints, including its
`/api/v1` alias. `POST /a2a/v1` applies the failed-credential backoff to
invalid API keys and bearer tokens without counting successful calls.
