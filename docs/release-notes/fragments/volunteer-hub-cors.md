## The volunteer hub no longer accepts every browser origin

`bernstein volunteer hub` installed CORS middleware with `allow_origins=["*"]`
and `allow_credentials=True` on every hub. The hub binds wherever `--host` says
and its authenticator is optional — with none configured, every auth-gated
endpoint is open — so any page a volunteer happened to open could drive an
unauthenticated hub from their browser.

CORS is now off unless the operator names the origins their volunteer web UI is
served from:

```
bernstein volunteer hub --allow-origin https://volunteers.example
```

With none named, no CORS middleware is installed at all, so a browser refuses a
cross-origin call and only same-origin and non-browser clients reach the API.
There is no wildcard.
