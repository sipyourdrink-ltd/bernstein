# Volunteer issue text

A volunteer task starts from an issue on a project the donor does not control.
Its title and body become an agent's prompt. The
[manifest](volunteer-manifest.md) already names this threat for the adjacent
case — a gate command from a repository the donor does not control is never
handed to a shell — and the issue text is the same category of input arriving
at a different point in the run.

There is no shell anywhere in that path, so escaping is not the job. The job is
closing the gap between **what a reviewer read** and **what the model
receives**, then framing what is left so its boundary cannot be moved from
inside it.

`sanitize_issue_text(title, body)` in `core/volunteer/issue_sanitize.py` returns
one delimited block. It is pure: no clock, no environment, no randomness, no
network, and the same arguments produce the same bytes on every machine.

## Three channels, and only one of them is obvious

| Channel | What the reader saw | What the raw body carries |
|---|---|---|
| HTML comment | nothing | anything, including instructions |
| Invisible and bidirectional characters | one word, in reading order | two words, or a different order |
| Lookalike characters | `Ignore` | `Ｉｇｎｏｒｅ` |

**HTML comments** are removed in both spellings. A closed `<!-- ... -->` across
any number of lines, and an unterminated `<!--`, which is not a stray tag: it
opens a CommonMark HTML block whose end condition is never met, so the block
runs to the end of the document and the rendered page shows none of what
follows.

That second rule is deliberately broader than the renderer's. A mid-paragraph
`<!--` with no closer is inline raw HTML, stays visible, and is still cut here
along with everything after it. An issue whose prose mentions those four
characters outside a fenced block loses its remainder. The trade is taken
because the failure directions are not symmetric — over-stripping truncates a
block a maintainer can see, under-stripping passes text nobody saw.

**Invisible and bidirectional characters are removed explicitly, not by
normalising.** This is the part worth stating plainly, because the instinct is
that NFKC handles it: of the 170 Unicode `Cf` format characters, NFKC removes
**none**. U+200B ZERO WIDTH SPACE, U+FEFF, and U+202E RIGHT-TO-LEFT OVERRIDE all
survive it unchanged. So `Cc` control, `Cf` format, and `Cs` surrogate
characters are dropped outright — newline and tab excepted — and `\r\n` and a
lone `\r` are folded to `\n` first, since deleting a carriage return as a
control character would glue the lines it separated into one word.

The dropping happens *before* normalising. No non-`Cf` codepoint's NFKC output
contains a `Cf` character, so nothing can be reintroduced, and the returned
block is itself in NFKC form for anything downstream that hashes it.

**Lookalikes** are what NFKC is for, and it does that part well: fullwidth forms
fold to ASCII, a non-breaking space becomes a space.

## The fence is derived from the text it wraps

The block looks like this:

```
----- BEGIN UNTRUSTED-ISSUE-TEXT 4f2a9c81e3b7d605 -----
title: Parser drops trailing commas

Steps to reproduce ...
----- END UNTRUSTED-ISSUE-TEXT 4f2a9c81e3b7d605 -----
```

The token is derived from the digest of the normalised content, then checked
against that content and re-derived until it does not occur there. Two
properties follow, and both were the reason for choosing this over the simpler
options.

**Deterministic**, which a random nonce would not be. The same title and body
produce the same block in every process and on every machine, so a caller
hashing the prompt gets a stable value and replay stays byte-identical.

**Unforgeable**, which a fixed `<<<ISSUE_TEXT>>>` marker would not be. An author
who wants their text to contain the fence has to find a body whose own digest
appears inside itself. The occurrence check makes that a guarantee by
construction rather than a probability argument, and the loop terminates
because each round lengthens the token until it is longer than the payload.

## What this does not do

**It does not make a model obey the frame.** A delimiter is a boundary the text
cannot move, not obedience. The sentence instructing an agent to treat the
block as quoted data belongs to the prompt template that composes the block,
not to the function that builds it — and no test in this area asserts model
behaviour, because "the agent ignored it" is not a property a string transform
can hold.

**It does not prove sanitized text never reaches a shell, the environment, or
the network.** That is a property of which subprocess calls the task runner
makes and with what environment. The function holds up its end by having no
route to any of them: the module imports `hashlib`, `re`, and `unicodedata`,
and nothing else, which a test asserts against an exact allowlist.

## Reusing the normalisation without the fence

Issue text is not the only place the program quotes a repository it does not
control. `normalize_untrusted_text(text)` is the same transform without the
prompt fence, for callers that are not building a prompt.

```python
from bernstein.core.volunteer import normalize_untrusted_text, sanitize_issue_text

prompt_block = sanitize_issue_text(issue.title, issue.body)
comment_text = normalize_untrusted_text(issue.body)
```

## Tests

```
uv run python -m pytest tests/unit/volunteer/test_issue_sanitize.py -q
```

Every test is named for the property it protects and every assertion is a plain
string comparison on the returned block.
