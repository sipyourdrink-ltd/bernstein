## The nightly mutation job measures `lineage_tips` again

The `lineage_tips` baseline ran the whole of `tests/unit/lineage/` — 706 tests,
~92s — to validate mutations in one file, and it did not finish inside the
180s subprocess timeout. The nightly reported `BASE-FAIL` and exited 2, so no
mutation score was produced for the tip tracker at all.

The budget was not the problem. Of the 89s the directory spends, 76s is
per-test autouse-fixture teardown — about 108ms on every test — against 11.3s
of actual test work. The baseline was paying for 548 tests that cannot kill a
mutant in `tips.py`, once for the baseline and again for every mutant run.

The module now names the seven files that can: the five that empirically fail
when `compute_tips`, `detect_forks` or `_group_by_path` are seeded with
degenerate returns, plus the two that import `tips` without asserting on it,
kept as headroom. 158 tests, ~11s, and the same mutants die.

`python3 scripts/mutmut_critical.py --only lineage_tips` now reports
`93.8% 75% PASS 15/16 killed` in 63s, against a 600s budget.
