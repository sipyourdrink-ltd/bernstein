## The governance panel lists what a run did not cover

The `/governance` panel now shows a second section, "not covered in this run", drawn from a checked-in `governance-not-covered.json` list. Each gap states its consequence and links to the GitHub issue that would close it; a gap whose fix is a setting points at `settings` instead of a dead link, and a resolved gap renders struck-through rather than vanishing. An empty list renders an explicit marker so "nothing known to be uncovered" is never mistaken for "nothing was checked" (#5069).
