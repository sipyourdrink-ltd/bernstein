# Contributor recognition

Roughly once every couple of weeks, the project's LinkedIn page names, in one
consolidated post, the contributors who want to be named, and says what they
built. This page is the operator side of that: what is automated, what the
maintainer does by hand, and where the record lives. The community-facing
explanation is the pinned issue (source text in
[`community/recognition/ISSUE.md`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/community/recognition/ISSUE.md));
the history is discussions
[#5524](https://github.com/sipyourdrink-ltd/bernstein/discussions/5524) (the
LinkedIn page, the first opt-ins) and
[#6229](https://github.com/sipyourdrink-ltd/bernstein/discussions/6229) (the
first consolidated thank-you).

## The flow

```
rolling 90 days -> outside contributors with a merged PR -> opt-in from the registry
  -> added lines in merged PRs, last 30 days, generated files excluded -> soft gate (~1,000)
  -> alphabetical -> two drafts -> maintainer reads -> publish
```

| Step | Who or what | Where |
|---|---|---|
| Collect the facts | `scripts/contributor_recognition.py collect` | public REST API, fails closed |
| Render the drafts | `… render` (pure function) | `period-comment.md`, `linkedin-draft.md` |
| Read, write the sentence per person | the maintainer | the LinkedIn draft |
| Post the periodic GitHub comment | `… publish-comment --yes`, or the workflow with `publish=true` | the pinned issue |
| Post on LinkedIn | the maintainer, by hand | the project page |
| Record an opt-in | `… parse-reply` on the mail, paste into the registry | `community/recognition/opt-ins.toml` |

The workflow
[`contributor-recognition.yml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/.github/workflows/contributor-recognition.yml)
runs weekly and only renders: the drafts land in the job summary and an
artifact. Nothing is posted until somebody re-runs it with `publish=true` and
the issue number. Cadence is a goal, not a promise the workflow makes.

## What the numbers are

- **Cohort:** every account that is not the maintainer, a bot or a known
  agent lane, with at least one pull request merged into `main` in the last
  90 days.
- **Soft gate:** added lines across that person's pull requests merged in the
  last 30 days, from the pull request file lists, with generated and vendored
  paths excluded (`GENERATED_PATHS` in the script: lockfiles, translated
  READMEs, the module map, generated API docs, protobuf stubs, bundles,
  snapshots). Default threshold 1,000; it filters one LinkedIn flow and
  measures nothing else. Reviews, security work, docs and release plumbing
  regularly land at forty lines; the exception path for those is a mail, and
  the registry has an `exception` flag for the maintainer's answer.
- **Order:** alphabetical by login, case-insensitive, everywhere a list of
  people appears. Never by volume.
- **Metric source:** the REST API only (`pulls`, `pulls/{n}/files`). A merged
  pull request's file list never changes, so `collect --cache` keeps the
  per-PR count between local runs. The workflow runs uncached; a weekly run
  costs a few hundred requests.

What the script does not do: write a sentence about anyone (the LinkedIn
draft lists each person's pull requests and leaves the sentence to the
maintainer), decide who counts, or post anything without `--yes`.

## The mail and the registry

Opt-ins arrive by email so that nobody has to open a pull request to be
named. The mail is routed by its subject:

```
To:      forte@bernstein.run
Subject: BRNSTN-PR-LNKD

GITHUB=<login>
OPT_IN=YES
RECOMMENDATION=NO
```

`RECOMMENDATION=YES` asks for a LinkedIn recommendation from the maintainer
instead of, or as well as, the post; `NOTE=` carries anything else, one line.
Keys are case-insensitive, one per line, first occurrence wins, quoted lines
are ignored. `parse-reply` reads a mail body and prints the
`[[contributor]]` block; the maintainer pastes it into the registry and the
next render picks it up. The six people who said yes in #5524 are seeded in
the registry with their comment links and were not asked again; one of them
asked to see the wording first, which is the `review_before_publish` flag,
and the LinkedIn draft names who to send it to.

## Publishing the issue

```bash
python3 scripts/contributor_recognition.py publish-issue            # dry-run: plan, deadline, body preview
python3 scripts/contributor_recognition.py publish-issue --yes      # create, post 8 translation comments, link them
python3 scripts/contributor_recognition.py reply-by --published 2026-10-01T09:00:00Z
```

The publisher creates the issue with the switcher pointing at the issue
itself, posts one collapsible `<details>` comment per language in the order
of `LANGUAGES`, then edits the body so each entry under the title links to
its comment's `#issuecomment-<id>` anchor. Those anchors are stable once a
comment exists, which is why the body is patched after the comments and not
before. Pin the issue by hand afterwards (pinning is not on the REST API).
The reply deadline is computed at publication: fourteen days later at 20:00
Israel time, also shown for India, Central Europe and UTC.

If the English text changes in substance after publication, change the
source, change every translation in the same pull request, and edit the
issue and its comments; two versions of one announcement are worse than one
late one.

## Outreach, the light version

The other half of this is [`community/outreach/`](https://github.com/sipyourdrink-ltd/bernstein/tree/main/community/outreach):
a template for the short maintainer quote another project asks for, and a
log of the final wording once it is public. It is a folder, not a programme.
