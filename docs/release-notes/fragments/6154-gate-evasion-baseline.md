## Gate-evasion regressions now block affected merges

The gate-evasion benchmark has a committed, content-addressed baseline of
actual caught cases and tool versions. CI now rejects reductions in catch rate,
loss of any previously caught class, and unverified or mismatched measurements;
intentional baseline changes require an explicit, reviewed update (#6154).
