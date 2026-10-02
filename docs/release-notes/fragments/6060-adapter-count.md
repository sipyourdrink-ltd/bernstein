## README states the adapter count in one place

The front-page header and the at-a-glance bullet each carried their own adapter
count, and neither tracked the adapter registry: they said "40+", then 52, then
53 while the supported-agents section said 54 selectable adapters and 56
wired-in rows. Two counts for one surface read as a stale claim.

The header and bullet now give a lower bound ("50+", "More than 50") that stays
true as adapters are added. The exact numbers appear only in the
supported-agents section, where `tests/unit/test_readme_adapter_counts.py`
recomputes them from the adapter registry, and the same test now fails if a
lower bound in the header or bullet overtakes the registry (#6060).
