## An `opa eval` failure keeps the error that explains it

`_run_opa_eval` reports a failed policy evaluation by raising with stderr, then
stdout, then a generic string. Every Rego test monkeypatched the function away,
so that fallback chain had never executed under test and a regression
collapsing it to the generic `"opa eval failed"` would have shipped green --
taking the Rego compile or runtime error the operator needs with it. The three
branches and the temp-file cleanup are now covered (#5951).
