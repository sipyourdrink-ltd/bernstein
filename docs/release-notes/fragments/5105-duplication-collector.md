## A standing count of capabilities with more than one implementation

A guard test answers "did this PR introduce a second implementation". It
cannot answer "how many are there now, and did that number move since last
week". `bernstein.core.govern.duplication_audit.collect_duplication` walks
the tree and reports that as findings — a stable `check_id`, the count
against its expected count, and the offending paths, so a finding read a
week later is still the same finding.

**"Not yet measurable" is its own verdict.** A check whose subject does
not exist yet is not passing, and reporting it as one is what makes an
aggregate look like coverage it does not have. Four of the six checks say
so today, pending their own issues.

**No score, no grade, no percentage.** `core/security/security_posture.py`
computes an A-F letter from weighted metrics and has zero callers — the
proof this report has been built once already and never wired. A count
against an expected count is auditable; a letter is a number nobody can
reconstruct. A test fails the build if `score`, `grade`, `percent` or
`rating` ever appears in the output (#5105).
