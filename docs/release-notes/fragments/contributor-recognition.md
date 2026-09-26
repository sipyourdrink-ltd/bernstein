## Contributor recognition workflow

Adds `scripts/contributor_recognition.py` and the `Contributor recognition`
workflow: a deterministic collector for outside contributors with pull
requests merged in the last 90 days, a soft gate on added lines over 30 days
with generated files excluded, an opt-in registry
(`community/recognition/opt-ins.toml`) fed by one email with a fixed subject,
alphabetical drafts for the periodic GitHub comment and the LinkedIn post,
and a publisher that posts the pinned issue with one collapsible translation
comment per language linked from under the title. The operator page is
`docs/community/recognition.md`; `community/outreach/` holds the maintainer
quote template and its log.
