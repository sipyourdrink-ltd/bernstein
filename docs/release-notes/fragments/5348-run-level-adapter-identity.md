## The run-level adapter instance survives a registry-key lookup

Resolving the spawn path's adapter name to the registry key made every lookup
of the run-level adapter miss its cache, which was seeded only under the
adapter's display name -- and 44 of the 53 registered adapters have a display
name that is not their key (`agy` displays as "Antigravity"). On a miss the
spawner built a second instance out of the registry, so the instance the
caller injected was dropped along with its host-isolation declaration and its
caching wrap; an injected adapter with no registry entry at all -- a test
double, a third-party adapter -- failed the spawn instead. The cache is now
seeded under both the display name and the registry key, and the key is
resolved once at construction, before the caching wrap, because `CachingAdapter`
is not itself registered and asking the wrapper reports a registered adapter as
unregistered.

The key is resolved by adapter identity, never by folding a display name back
to a key: `agy`'s display name is exactly the registry key `antigravity`, which
is an alias for the Gemini adapter, so a name-string fold routes an `agy` spawn
onto a different vendor's CLI. A regression test pins the collision.

The admission gate's exemption set is also documented accurately again: `mock`
is the only exempt adapter. `generic` stopped being exempt in #4752, and both
the operator guide and the spawner docstring still promised otherwise (#5348).
