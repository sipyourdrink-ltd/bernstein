## Speak AuthZEN 1.0 at the permission decision boundary

`bernstein.core.security.authzen` expresses a permission request and its answer
in the AuthZEN 1.0 evaluation shape — subject, resource, action and context on
the way in; decision plus obligations on the way out — encoded with the same
RFC 8785 (JCS) canonicaliser the signed surfaces use, so the same request
digests identically on every host. `PolicyHookRegistry.evaluate` normalises
every request through that shape before any engine sees it, and a request the
shape cannot express raises instead of reaching a policy engine. Nothing is
dropped on the way: an unknown context field, an unknown payload field, or a
subject property the internal request cannot carry is refused rather than
silently discarded, because an engine answering over fewer attributes than it
was sent has answered a different question. `HookResponse` now carries
obligations, and a permit that carries one is distinguishable from one that
does not. (#5032)
