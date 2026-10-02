## The docs home page states adapter floors, and a test keeps them honest

`docs/index.md` restated the adapter count twice - in its front-matter
`description` (the site's search-engine meta text) and in the "Any agent, any
model" feature bullet - as "40+", and nothing checked either. Both now say
"50+", matching the README header, and
`tests/unit/test_readme_adapter_counts.py` fails if either floor is raised
above what `selectable_adapter_names()` backs - the same floor check the README
header and at-a-glance bullet already have (#6060).
