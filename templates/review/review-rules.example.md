# Review rules (example)

Copy this file to `.bernstein/review-rules.md`, or point a pipeline at it with
a `rules:` key, to hold every reviewer to one written standard.  Bullets under
a heading that starts with `Raise` or `Guard` become rules; everything else on
the page is prose the parser ignores, so the file stays readable.

The digest of the parsed rule set is bound into every review receipt, so a
verdict names the standard it was produced under.  Reordering the bullets
leaves the digest alone; editing one moves it.

## Raise

Defect classes a review must flag.

- A bare `except:` that swallows the traceback.
- A `subprocess` call with no timeout.
- A new public function without a docstring stating its arguments and returns.
- A test that asserts on a mock's call count and nothing else.

## Guard

Findings a review must not raise, because an operator already rejected them.
Without this half, every unattended pass re-reports the same false positive
and the fix pass chases it.

- `assert` inside `tests/` is not a security finding.
- The vendored parser under `third_party/` is exempt from the style rules.
- A missing type annotation on a pytest fixture is not a defect.
