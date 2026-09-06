## `ADOPTERS.md`: who runs this, on what, and since when

Nothing in the repository said who depends on Bernstein. An evaluator deciding
whether to put a governance layer in front of regulated workloads had to read
the issue tracker and guess from usernames, and absent evidence reads as absent
adopters — a stronger negative signal than a short list would be.

`ADOPTERS.md` carries two sections, Production and Evaluation / Pilot, because a
pilot and a production dependency are different claims and should not need a
squint to tell apart. Both start empty, which is the correct state: a padded
list is worse than none.

Entries are self-reported by pull request. Nobody is added by a maintainer on
someone else's behalf, and usage that could be inferred from public activity is
not listed — inference is not consent. A contact handle is required and the use
case has to be specific; `tests/unit/test_adopters_page.py` enforces both, plus
the `Since` month format, since those are the fields that decay first and that
nobody can reconstruct later. `CONTRIBUTING.md` says how to add yourself (#5027).
