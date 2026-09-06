# Adopters

Who runs Bernstein, on what, and since when.

This page exists for three reasons, none of them promotion:

- **An evaluator cannot calibrate risk without it.** Someone deciding whether to
  put a governance layer in front of regulated workloads asks who else depends
  on it and for what. Absent evidence reads as absent adopters, which is a
  stronger negative signal than a short list.
- **Contributors cannot see where their work lands.** Someone who fixed
  something in the identity plane has no way to learn that it mattered to an
  air-gapped install. That feedback is most of what keeps unpaid contributors
  returning.
- **We cannot tell which surfaces are load-bearing.** Prioritisation runs on
  intuition about what people use. A use-case column turns that into something
  readable.

## Production

Systems people depend on.

| Organization or project | Domain | What they use it for | Since | Contact |
|---|---|---|---|---|
<!-- Add your row here. Keep the table sorted by the first column. -->

## Evaluation / Pilot

Trials, proofs of concept, and installs not yet depended on. A pilot and a
production dependency are different claims and should not need a squint to tell
apart.

| Organization or project | Domain | What they use it for | Since | Contact |
|---|---|---|---|---|
<!-- Add your row here. Keep the table sorted by the first column. -->

## How to add yourself

Open a pull request adding one row to the section that describes your install.
That is the whole process — no form, no approval queue.

The rules that keep this page worth reading:

- **Self-reported, by pull request.** An entry is added by the adopter, never by
  a maintainer on someone's behalf. A list a maintainer writes about their own
  users is a claim; a list adopters sign is evidence. Usage that could be
  inferred from public activity is not listed: **inference is not consent.**
- **A contact handle is required.** A GitHub handle, an email, or a project
  URL — something a reader could follow up with. An entry nobody will stand
  behind does not go in.
- **The use case is specific.** "Uses Bernstein" is not a row. "Deterministic
  replay for regulated evidence in an air-gapped environment" is a row. The
  column is what makes the page useful to the next evaluator, and to us when we
  are deciding what to work on.
- **Individuals and projects are welcome**, not only companies. Most real usage
  here is not corporate, and a list that accepted only logos would be both empty
  and dishonest.
- **`Since` is a month and year** (`2026-03`), the point you started depending
  on it — not the day you first cloned it.

Nothing about scale, revenue, or growth belongs on this page, and neither do
logos, testimonials, or case-study prose. Rows and links.

## Pruning

An entry is removed when it stops being true. In practice:

- The contact has been unreachable for two release cycles and the row cannot be
  confirmed.
- The adopter asks for removal — always, immediately, no discussion.
- The described use case no longer exists.

Anyone may open a pull request removing their own row for any reason or none.
Removal is never treated as a signal about the project, and no one is asked to
justify it.

An empty section is a correct state for this page. A padded one is worse than
none, and it is the failure mode a page like this invites.
