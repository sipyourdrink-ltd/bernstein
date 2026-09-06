## Uncalled-security-control guard recognizes same-module callers

The static analysis guard in 	est_security_controls_are_wired.py previously only
scanned for external references (st.Attribute or import references), reporting
functions that were called internally within their own module via bare st.Name calls
as uncalled. It now indexes intra-module st.Name references, accurately proving
which functions have internal callers and shrinking the stale exemptions list (#5336).
