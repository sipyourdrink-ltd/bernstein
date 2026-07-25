"""Property tests for the artifact-key grammar (issue #2559).

The claim this file has to make good on is that widening the lineage key space
did not move the line for repo-relative paths. Property tests are the right
instrument: the boundaries are pure functions, and the pre-#2559 rules are
short enough to restate as an oracle, so equivalence can be checked over
generated inputs rather than over a list of cases someone thought of.

Properties:

* **Repo verdicts are byte-identical to the oracle.** For any string that does
  not carry the URI marker, the new decision function accepts exactly what the
  pre-#2559 guards accepted and rejects exactly what they rejected. A single
  divergence is either a path-traversal regression or a refusal of a write that
  used to work.
* **A repo key is never rewritten.** Canonicalisation is the identity function
  on every accepted repo path, so no entry hash computed under the old key can
  drift onto a new one.
* **Canonicalisation is idempotent and closed.** The canonical form of any
  accepted key is itself accepted and canonicalises to itself.
* **Traversal is refused wherever it appears.** For any segment count and
  position, in a repo path or inside a URI.
* **Unknown schemes never reach the chain.** No generated scheme outside the
  closed set is ever accepted.
"""

from __future__ import annotations

from hypothesis import assume, given
from hypothesis import strategies as st

from bernstein.core.lineage.artifact_uri import (
    ARTIFACT_URI_SCHEMES,
    REASON_TRAVERSAL,
    REASON_UNKNOWN_SCHEME,
    artifact_key_rejection_reason,
    canonical_artifact_key,
    is_canonical_artifact_key,
)

# ---------------------------------------------------------------------------
# The pre-#2559 oracle: the two boundary guards, restated verbatim.
# ---------------------------------------------------------------------------


def _legacy_is_unsafe(path: str) -> bool:
    """Whether the pre-#2559 lineage boundaries rejected ``path``.

    Copied from ``recorder._is_unsafe_path`` / ``spine._reject_unsafe_artifact_path``
    as they stood before the artifact-URI namespace landed. Kept as a literal
    restatement rather than an import so that a future edit to the production
    guard cannot quietly move the oracle with it.
    """
    if not path:
        return True
    if path.startswith("/") or (len(path) > 2 and path[1:3] == ":\\"):
        return True
    return any(seg == ".." for seg in path.replace("\\", "/").split("/"))


# ``://`` is the marker that routes a string to the URI branch; the equivalence
# claim is scoped to strings without it, which is every repo-relative path.
_repo_ish = st.text(min_size=0, max_size=60).filter(lambda s: "://" not in s)

_segment = st.text(
    alphabet=st.characters(blacklist_characters="/\\\x00", blacklist_categories=("Cs",)),
    min_size=1,
    max_size=8,
).filter(lambda s: s != "..")


@given(_repo_ish)
def test_repo_verdict_matches_the_pre_feature_oracle(path: str) -> None:
    assert (artifact_key_rejection_reason(path) is not None) == _legacy_is_unsafe(path)


@given(_repo_ish)
def test_accepted_repo_paths_are_never_rewritten(path: str) -> None:
    assume(not _legacy_is_unsafe(path))
    assert canonical_artifact_key(path) == path


@given(st.lists(_segment, min_size=1, max_size=5))
def test_clean_relative_paths_are_accepted(segments: list[str]) -> None:
    path = "/".join(segments)
    assume(not path.startswith("/"))
    assume("://" not in path)
    assume(not (len(path) > 2 and path[1:3] == ":\\"))
    assert artifact_key_rejection_reason(path) is None


@given(st.lists(_segment, max_size=4), st.lists(_segment, max_size=4))
def test_traversal_is_refused_at_every_position(before: list[str], after: list[str]) -> None:
    path = "/".join([*before, "..", *after])
    assume(not path.startswith("/"))
    assert artifact_key_rejection_reason(path) == REASON_TRAVERSAL


#: Segments a well-formed URI may carry, so that a traversal property is not
#: masked by a charset rejection fired first.
_uri_segment = st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEF0123456789._-", min_size=1, max_size=6).filter(
    lambda s: s not in {".", ".."}
)


@given(
    st.sampled_from(sorted(ARTIFACT_URI_SCHEMES)),
    st.lists(_uri_segment, max_size=3),
    st.lists(_uri_segment, max_size=3),
)
def test_traversal_inside_a_uri_is_refused_too(scheme: str, before: list[str], after: list[str]) -> None:
    key = f"{scheme}://host.test/" + "/".join([*before, "..", *after])
    assert artifact_key_rejection_reason(key) == REASON_TRAVERSAL


@given(
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=1),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", max_size=7),
)
def test_unknown_schemes_are_always_refused(head: str, tail: str) -> None:
    scheme = head + tail
    assume(scheme not in ARTIFACT_URI_SCHEMES)
    assume(scheme != "repo")
    assert artifact_key_rejection_reason(f"{scheme}://host.test/a/b/1") == REASON_UNKNOWN_SCHEME


@given(st.text(min_size=1, max_size=12).filter(lambda s: "://" not in s))
def test_no_string_can_smuggle_an_unrecognised_scheme_through(prefix: str) -> None:
    """Whatever precedes ``://``, the result is never silently accepted.

    Either it parses as a known scheme in canonical form, or it is refused.
    There is no third outcome where the chain records a key nobody can resolve.
    """
    key = f"{prefix}://host.test/a/b/1"
    reason = artifact_key_rejection_reason(key)
    if reason is None:
        assert prefix.lower() in ARTIFACT_URI_SCHEMES


@given(
    st.sampled_from(sorted(ARTIFACT_URI_SCHEMES)),
    st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=6),
    st.lists(
        st.text(alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJ0123456789._-", min_size=1, max_size=6),
        min_size=3,
        max_size=5,
    ),
)
def test_canonicalisation_is_idempotent_and_closed(scheme: str, authority: str, segments: list[str]) -> None:
    if scheme == "pr":
        segments = [*segments[:-1], "12"]
    raw = f"{scheme}://{authority}.test/" + "/".join(segments) if scheme in {"pr", "doc"} else None
    if raw is None:
        raw = f"{scheme}://{authority}/" + "/".join(segments)
    assume(artifact_key_rejection_reason(raw) is None)
    once = canonical_artifact_key(raw)
    assert once == raw
    assert canonical_artifact_key(once) == once
    assert is_canonical_artifact_key(once)
