# Community Spotlight — [Month] [Year]

> A monthly series celebrating the developers who make Bernstein possible.

---

## 🌟 Featured Contributors

<!-- For each contributor, include: GitHub username, what they built, 2-3 sentences about impact, and the PR link -->

### @username — [What they built]

[2-3 sentences describing their contribution and its impact on the project.]

- **PR:** [#NNN](https://github.com/chernistry/bernstein/pull/NNN)
- **Category:** [Feature | Bug Fix | Documentation | Infrastructure | Security]

---

### @username — [What they built]

[2-3 sentences describing their contribution and its impact on the project.]

- **PR:** [#NNN](https://github.com/chernistry/bernstein/pull/NNN)
- **Category:** [Feature | Bug Fix | Documentation | Infrastructure | Security]

---

## 📊 By the Numbers

<!-- Auto-generate these from git logs where possible -->

| Metric | This Month | Change |
|--------|-----------|--------|
| New Contributors | X | +/-Y |
| Merged PRs | X | +/-Y |
| Closed Issues | X | +/-Y |
| Stars Added | X | +/-Y |
| Forks Added | X | +/-Y |

## 🔥 Hot PRs

<!-- Top 3-5 most impactful merged PRs this month -->

1. **[PR Title]** by [@author](https://github.com/author) — [#NNN](https://github.com/chernistry/bernstein/pull/NNN)
   [One-line description of impact]

2. **[PR Title]** by [@author](https://github.com/author) — [#NNN](https://github.com/chernistry/bernstein/pull/NNN)
   [One-line description of impact]

3. **[PR Title]** by [@author](https://github.com/author) — [#NNN](https://github.com/chernistry/bernstein/pull/NNN)
   [One-line description of impact]

## 🏆 Contributor of the Month

<!-- Highlight one contributor who went above and beyond -->

**[@username](https://github.com/username)**

[2-3 paragraphs about why this contributor stands out: consistency, quality of work, helpfulness in reviews, going beyond the issue scope, mentoring others, etc.]

Notable contributions this month:
- [Contribution 1]
- [Contribution 2]
- [Contribution 3]

## 🎯 Areas of Impact

<!-- Categorize contributions by area -->

| Area | PRs | Contributors |
|------|-----|-------------|
| Core Agent | X | @a, @b |
| Security | X | @c |
| Documentation | X | @d |
| Infrastructure/CI | X | @e |
| Integrations | X | @f |

## 📝 Shoutouts

<!-- Quick thanks to people who helped in other ways -->

- **@username** — Excellent code review feedback on PR #NNN
- **@username** — Helped triage and reproduce issue #NNN
- **@username** — First-time contributor, welcome!

## 🚀 Coming Next Month

<!-- Tease upcoming features or areas where contributions are especially welcome -->

- [Area 1]: Looking for help with [specific task]
- [Area 2]: New feature in development, contributors welcome
- [Area 3]: Documentation improvements needed

---

## How to Generate This Report

This template can be auto-generated from git history using the following approach:

```bash
# Get contributors this month
git log --since="2026-04-01" --until="2026-04-30" --format="%an" | sort | uniq -c | sort -rn

# Get merged PRs
gh pr list --state merged --base main --limit 50 --json number,title,author,mergedAt

# Get new contributors
git log --since="2026-04-01" --format="%ae" | sort -u > /tmp/this_month.txt
git log --until="2026-04-01" --format="%ae" | sort -u > /tmp/prev_month.txt
comm -23 /tmp/this_month.txt /tmp/prev_month.txt
```

For a fully automated version, see the companion script: `scripts/generate-spotlight.sh`
