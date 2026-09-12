## `key ~` is now a selector syntax error

A govern selector term of the form `key ~` compiled an empty regex. An empty pattern matches at every position, so the term constrained nothing while reading as a constraint - and a selector decides which nodes a reconcile lane or an audit pass acts on, so one that silently stops filtering aims the operation at the whole inventory rather than at none of it. `region ~$REGION` with the variable unset expands to exactly this.

**Before:** `key ~` was accepted and matched every value.
**Now:** it raises `SelectorSyntaxError`, naming the alternative - omit the filter to match everything.

The grammar already refused `{}` with "empty set for key" for the same reason. Unanchored `search` semantics are unchanged: `~us` still matches `eu-us-west`.
