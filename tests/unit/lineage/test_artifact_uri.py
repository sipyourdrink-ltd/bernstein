"""Tests for the canonical artifact-key grammar (issue #2559).

The lineage layer's value rests on a key meaning exactly one thing. These tests
pin the three properties that keeps true:

* **Closed scheme set.** An unknown or malformed scheme is rejected at the
  boundary, never passed through as if it were a filename. A permissive parser
  would let a record be keyed by something nobody can resolve.
* **Path safety does not regress.** Every traversal, absolute-path and
  scheme-confusion input that the pre-#2559 guards rejected is still rejected,
  and the new URI branch is held to the same standard.
* **Determinism.** Canonicalisation is a pure function of the input string, so
  the same declared output yields the same key and the same content address on
  any host.
"""

from __future__ import annotations

import pytest

from bernstein.core.lineage.artifact_uri import (
    ARTIFACT_URI_SCHEMES,
    REASON_ABSOLUTE,
    REASON_EMPTY,
    REASON_MALFORMED_URI,
    REASON_NON_CANONICAL,
    REASON_TRAVERSAL,
    REASON_UNKNOWN_SCHEME,
    ArtifactURIError,
    artifact_key_rejection_reason,
    canonical_artifact_key,
    canonical_artifact_pattern,
    external_reference_bytes,
    external_reference_content_hash,
    is_canonical_artifact_key,
    match_artifact_pattern,
    parse_artifact_key,
)

# ---------------------------------------------------------------------------
# Accepted keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        # Repo scheme (implicit) -- what every historical entry carries.
        "src/bernstein/core/lineage/spine.py",
        "a",
        "a/b/c/d/e.txt",
        ".sdd/evidence/bundles/task-1.json",
        # Repo paths the pre-#2559 guards accepted and must keep accepting:
        # spaces, glob characters, colons, backslashes are all legal filename
        # bytes and none of them is a traversal.
        "docs/my file.md",
        "build/a*b.txt",
        "weird:name.txt",
        "dir\\file.txt",
        "a/./b.txt",
        # External schemes.
        "pr://github.com/acme/widget/2559",
        "pr://gitlab.example.test/group/subgroup/project/12",
        "pr://git.example.test:8443/acme/widget/7",
        "pkg://pypi/bernstein/3.9.0",
        "pkg://npm/@scope/pkg/1.0.0",
        "pkg://oci/registry.example/image/sha256-abc",
        "deploy://prod/docs-site",
        "deploy://staging/api/edge",
        "doc://example.test/lineage/artifacts",
    ],
)
def test_canonical_keys_are_accepted(key: str) -> None:
    assert artifact_key_rejection_reason(key) is None
    assert canonical_artifact_key(key) == key
    assert is_canonical_artifact_key(key)


def test_repo_key_parses_as_the_implicit_scheme() -> None:
    parsed = parse_artifact_key("src/a/b.py")
    assert parsed.is_repo
    assert parsed.scheme == "repo"
    assert parsed.authority == ""
    assert parsed.segments == ("src", "a", "b.py")
    # A repo key is reproduced verbatim: canonicalisation never rewrites the
    # string a historical entry hash was computed over.
    assert parsed.canonical == "src/a/b.py"


def test_external_key_exposes_its_parts() -> None:
    parsed = parse_artifact_key("pkg://pypi/bernstein/3.9.0")
    assert parsed.scheme == "pkg"
    assert parsed.authority == "pypi"
    assert parsed.segments == ("bernstein", "3.9.0")
    assert not parsed.is_repo


def test_scheme_set_is_closed() -> None:
    assert frozenset({"pr", "pkg", "deploy", "doc"}) == ARTIFACT_URI_SCHEMES


# ---------------------------------------------------------------------------
# Adversarial: path safety must not regress
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "reason"),
    [
        ("", REASON_EMPTY),
        # Absolute-path escapes.
        ("/etc/passwd", REASON_ABSOLUTE),
        ("/", REASON_ABSOLUTE),
        ("C:\\Windows\\system32", REASON_ABSOLUTE),
        ("Z:\\secrets", REASON_ABSOLUTE),
        # Traversal, at every position.
        ("..", REASON_TRAVERSAL),
        ("../etc/passwd", REASON_TRAVERSAL),
        ("a/../../etc/passwd", REASON_TRAVERSAL),
        ("a/b/..", REASON_TRAVERSAL),
        # Backslash-separated traversal: lineage canonical is POSIX, so ``\``
        # is treated as a separator for the safety check too.
        ("..\\..\\etc\\passwd", REASON_TRAVERSAL),
        ("a\\..\\b", REASON_TRAVERSAL),
        # Traversal inside a URI path is rejected the same way.
        ("pkg://pypi/../secrets/1.0", REASON_TRAVERSAL),
        ("doc://example.test/a/../../b", REASON_TRAVERSAL),
        ("pr://github.com/acme/../evil/1", REASON_TRAVERSAL),
    ],
)
def test_unsafe_keys_are_rejected(key: str, reason: str) -> None:
    assert artifact_key_rejection_reason(key) == reason


@pytest.mark.parametrize(
    "key",
    [
        # Symlink-ish and shell-ish inputs that must not be interpreted.
        "~/.ssh/id_ed25519",
        "~root/.bashrc",
        "./../outside",
        "a/b/../../../../../../etc/shadow",
    ],
)
def test_symlinkish_and_traversal_inputs_never_escape(key: str) -> None:
    """A key is a name, never a path the process resolves.

    ``~`` is not expanded (it stays an ordinary first segment), and anything
    that carries a ``..`` segment is refused outright, so no input can name a
    location outside the repo.
    """
    reason = artifact_key_rejection_reason(key)
    if ".." in key.split("/"):
        assert reason == REASON_TRAVERSAL
    else:
        # ``~/...`` is accepted as a literal relative name; crucially it is
        # *not* expanded to the home directory by anything downstream, because
        # the key is only ever hashed and used as a dict key.
        assert reason is None
        assert canonical_artifact_key(key) == key


# ---------------------------------------------------------------------------
# Adversarial: scheme confusion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "ftp://evil.test/payload",
        "http://evil.test/payload",
        "https://evil.test/payload",
        "file:///etc/passwd",
        "javascript://x/y",
        "data://text/plain",
        "a://b/c",
        "PKG2://pypi/x/1",
    ],
)
def test_unknown_schemes_are_rejected_not_stored_as_paths(key: str) -> None:
    """The pre-#2559 hole: an unknown scheme sailed through as a filename.

    ``ftp://evil/x`` has no leading ``/``, no drive prefix and no ``..``
    segment, so the old repo-path guards accepted it and the chain recorded a
    key whose scheme meant nothing. It is now parsed and refused.
    """
    assert artifact_key_rejection_reason(key) == REASON_UNKNOWN_SCHEME


@pytest.mark.parametrize(
    "key",
    [
        # ``://`` present but no parseable scheme -- not a repo path that
        # happens to contain punctuation.
        "://nothing",
        "1pkg://pypi/x/1",
        "-pkg://pypi/x/1",
        "a b://c/d",
        "docs/a://b",
        # Nested scheme markers.
        "pkg://pypi/x/http://evil.test",
        "repo://src/pkg://pypi/x/1",
        # Percent-encoding: two encodings of one byte would be two keys for
        # one artifact.
        "pkg://pypi/x/%41",
        "doc://example.test/%2e%2e/etc",
        # Query strings, fragments, userinfo, wildcards in a concrete key.
        "pr://github.com/acme/widget/1?x=1",
        "doc://example.test/page#frag",
        "pr://user@github.com/acme/widget/1",
        # Backslash inside a URI.
        "doc://example.test/a\\b",
        # Control characters and whitespace.
        "doc://example.test/a\x00b",
        "doc://example.test/a b",
        "doc://example.test/a\nb",
    ],
)
def test_malformed_uris_are_rejected(key: str) -> None:
    assert artifact_key_rejection_reason(key) == REASON_MALFORMED_URI


@pytest.mark.parametrize(
    ("key", "expected_canonical"),
    [
        # Case folding on scheme and authority only.
        ("PR://github.com/acme/widget/1", "pr://github.com/acme/widget/1"),
        ("pr://GitHub.COM/acme/widget/1", "pr://github.com/acme/widget/1"),
        ("PKG://PyPI/Bernstein/3.9.0", "pkg://pypi/Bernstein/3.9.0"),
        # A single trailing slash is dropped.
        ("doc://example.test/lineage/", "doc://example.test/lineage"),
        # ``repo://`` is an input alias whose canonical form is the bare path.
        ("repo://src/a.py", "src/a.py"),
    ],
)
def test_non_canonical_spellings_canonicalise_but_are_refused_on_the_wire(
    key: str,
    expected_canonical: str,
) -> None:
    """Canonicalise for the operator; refuse at the boundary.

    Accepting both spellings would fork one artifact's tip set across two
    chains. Rewriting silently would change the key an entry hash was already
    computed over. So the boundary refuses and the caller fixes the key.
    """
    assert canonical_artifact_key(key) == expected_canonical
    assert artifact_key_rejection_reason(key) == REASON_NON_CANONICAL
    assert not is_canonical_artifact_key(key)


@pytest.mark.parametrize(
    "key",
    [
        "pr://github.com//acme/1",
        "doc://example.test//a",
        "pkg://pypi/x//1.0",
        "doc://example.test/a//",
        "pkg:///x/1.0",
        "pkg://",
    ],
)
def test_empty_authority_or_segment_is_malformed(key: str) -> None:
    """``//`` is a malformed key, not a key to silently repair.

    Collapsing it would make two distinct strings canonicalise together via a
    repair rule rather than a naming rule, which is how one artifact ends up
    with two identities.
    """
    assert artifact_key_rejection_reason(key) == REASON_MALFORMED_URI


@pytest.mark.parametrize(
    "key",
    [
        "pr://github.com/acme/1",  # project path + number needs 3 segments
        "pkg://pypi/bernstein",  # name + version needs 2
        "deploy://prod",
        "doc://example.test",
        "pr://github.com/acme/widget/0",  # request numbers start at 1
        "pr://github.com/acme/widget/01",  # no leading zeros: one number, one key
        "pr://github.com/acme/widget/x",
        "pr://github.com/acme/widget/1x",
    ],
)
def test_per_scheme_shape_is_enforced(key: str) -> None:
    assert artifact_key_rejection_reason(key) == REASON_MALFORMED_URI


def test_overlong_uri_is_rejected() -> None:
    assert artifact_key_rejection_reason("doc://example.test/" + "a" * 4096) == REASON_MALFORMED_URI


def test_overlong_segment_is_rejected() -> None:
    assert artifact_key_rejection_reason("doc://example.test/" + "a" * 300) == REASON_MALFORMED_URI


def test_parse_raises_with_the_reason_attached() -> None:
    with pytest.raises(ArtifactURIError) as excinfo:
        parse_artifact_key("ftp://evil.test/x")
    assert excinfo.value.reason == REASON_UNKNOWN_SCHEME


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "src/a.py",
        "pr://github.com/acme/widget/2559",
        "PKG://PyPI/x/1.0",
        "doc://example.test/a/",
        "repo://src/a.py",
    ],
)
def test_canonicalisation_is_idempotent(key: str) -> None:
    once = canonical_artifact_key(key)
    assert canonical_artifact_key(once) == once
    assert is_canonical_artifact_key(once)


def test_canonicalisation_is_pure(tmp_path, monkeypatch) -> None:
    """No filesystem, no environment, no cwd dependence.

    Determinism is what lets a verdict computed on one host be recomputed on
    another, so canonicalisation must not consult anything ambient.
    """
    key = "pkg://pypi/bernstein/3.9.0"
    first = canonical_artifact_key(key)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PWD", str(tmp_path))
    assert canonical_artifact_key(key) == first


# ---------------------------------------------------------------------------
# Declared-output patterns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("pattern", "key", "expected"),
    [
        # Exact keys.
        ("src/a.py", "src/a.py", True),
        ("src/a.py", "src/b.py", False),
        # ``*`` does not cross a separator.
        ("dist/*.whl", "dist/bernstein-3.9.0.whl", True),
        ("dist/*.whl", "dist/nested/x.whl", False),
        ("dist/*.whl", "dist/x.tar.gz", False),
        # ``?`` matches one non-separator character.
        ("dist/v?.whl", "dist/v1.whl", True),
        ("dist/v?.whl", "dist/v10.whl", False),
        ("dist/v?.whl", "dist/v/.whl", False),
        # ``**`` crosses separators; ``/**/`` also matches zero segments.
        ("docs/**/*.md", "docs/a.md", True),
        ("docs/**/*.md", "docs/guides/a.md", True),
        ("docs/**/*.md", "docs/guides/deep/a.md", True),
        ("docs/**/*.md", "other/a.md", False),
        # Patterns over external schemes.
        ("pkg://pypi/bernstein/*", "pkg://pypi/bernstein/3.9.0", True),
        ("pkg://pypi/bernstein/*", "pkg://pypi/other/3.9.0", False),
        ("pr://github.com/acme/widget/*", "pr://github.com/acme/widget/2559", True),
        ("deploy://prod/**", "deploy://prod/api/edge", True),
        ("deploy://prod/**", "deploy://staging/api/edge", False),
        # A scheme-qualified pattern never matches a repo key, and vice versa.
        ("pkg://pypi/**", "src/a.py", False),
        ("dist/**", "pkg://pypi/bernstein/3.9.0", False),
        # A bare ``**`` is the deliberate match-everything pattern.
        ("**", "pkg://pypi/bernstein/3.9.0", True),
        ("**", "src/a.py", True),
    ],
)
def test_pattern_matching(pattern: str, key: str, expected: bool) -> None:
    assert match_artifact_pattern(pattern, key) is expected


def test_pattern_authority_must_be_concrete() -> None:
    """Globbing the authority is refused.

    ``pkg://*/name/1.0`` would let one declaration cover artifacts published to
    registries the operator never named. The set a declaration covers should be
    obvious from reading it.
    """
    with pytest.raises(ArtifactURIError) as excinfo:
        canonical_artifact_pattern("pkg://*/bernstein/3.9.0")
    assert excinfo.value.reason == REASON_MALFORMED_URI


def test_pattern_matching_canonicalises_both_sides() -> None:
    assert match_artifact_pattern("PKG://PyPI/bernstein/*", "pkg://pypi/bernstein/3.9.0")


def test_patterns_relax_arity_but_not_safety() -> None:
    # A pattern names a set, so the per-scheme arity minimum does not apply...
    assert canonical_artifact_pattern("pkg://pypi/*") == "pkg://pypi/*"
    # ...but traversal and unknown schemes are refused exactly as before.
    with pytest.raises(ArtifactURIError) as excinfo:
        canonical_artifact_pattern("pkg://pypi/../*")
    assert excinfo.value.reason == REASON_TRAVERSAL
    with pytest.raises(ArtifactURIError) as excinfo:
        canonical_artifact_pattern("ftp://evil.test/*")
    assert excinfo.value.reason == REASON_UNKNOWN_SCHEME


def test_glob_metacharacters_are_refused_in_a_concrete_uri() -> None:
    """A concrete URI key must name one artifact, not a set."""
    assert artifact_key_rejection_reason("pkg://pypi/bernstein/*") == REASON_MALFORMED_URI


# ---------------------------------------------------------------------------
# External referenced content
# ---------------------------------------------------------------------------


def test_external_reference_is_deterministic_across_hosts(tmp_path, monkeypatch) -> None:
    uri = "pkg://pypi/bernstein/3.9.0"
    digest = "sha256:" + "ab" * 32
    first = external_reference_content_hash(uri, digest=digest)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert external_reference_content_hash(uri, digest=digest) == first
    assert first.startswith("sha256:")


def test_external_reference_bytes_carry_no_host_state() -> None:
    payload = external_reference_bytes("pr://github.com/acme/widget/1", digest="sha1:" + "cd" * 20)
    assert payload == (b'{"artifact_uri":"pr://github.com/acme/widget/1","digest":"sha1:' + b"cd" * 20 + b'","v":1}')


def test_external_reference_folds_equivalent_spellings() -> None:
    """Two spellings of one artifact anchor to one content address."""
    a = external_reference_content_hash("PKG://PyPI/bernstein/3.9.0", digest="sha256:" + "0f" * 32)
    b = external_reference_content_hash("pkg://pypi/bernstein/3.9.0", digest="SHA256:" + "0F" * 32)
    assert a == b


def test_external_reference_distinguishes_digests() -> None:
    uri = "deploy://prod/docs-site"
    a = external_reference_content_hash(uri, digest="sha256:" + "11" * 32)
    b = external_reference_content_hash(uri, digest="sha256:" + "22" * 32)
    assert a != b


@pytest.mark.parametrize(
    "digest",
    [
        "abc",  # no algorithm named
        "deadbeef" * 8,  # bare hex is ambiguous
        "md5:" + "ab" * 16,  # unsupported algorithm
        "sha256:" + "ab" * 16,  # wrong length
        "sha256:" + "zz" * 32,  # not hex
        "sha256:",
    ],
)
def test_malformed_digests_are_refused(digest: str) -> None:
    with pytest.raises(ArtifactURIError):
        external_reference_content_hash("pkg://pypi/x/1.0", digest=digest)


def test_repo_artifacts_are_not_anchored_by_reference() -> None:
    """A repo artifact's bytes are reachable, so a reference anchor would be a
    second, weaker identity for content the chain can hash directly."""
    with pytest.raises(ArtifactURIError):
        external_reference_content_hash("src/a.py", digest="sha256:" + "ab" * 32)


def test_external_reference_extractor_absent_keeps_legacy_address() -> None:
    """A reference with no extraction step must serialise the extractor as
    ABSENT, not as an empty string -- an empty-string default would silently
    change its content address and is the most likely way to get this wrong."""
    uri = "pkg://pypi/bernstein/3.9.0"
    digest = "sha256:" + "ab" * 32
    assert external_reference_bytes(uri, digest=digest) == (
        b'{"artifact_uri":"pkg://pypi/bernstein/3.9.0","digest":"sha256:' + b"ab" * 32 + b'","v":1}'
    )
    assert external_reference_content_hash(uri, digest=digest) == external_reference_content_hash(
        uri, digest=digest, extractor=None
    )


def test_external_reference_extractor_is_deterministic() -> None:
    """Same URI + same digest + same extractor => same content address."""
    uri = "doc://example.test/lineage"
    digest = "sha256:" + "cd" * 32
    a = external_reference_content_hash(uri, digest=digest, extractor="html-readability@2.4.1")
    b = external_reference_content_hash(uri, digest=digest, extractor="html-readability@2.4.1")
    assert a == b


def test_external_reference_extractor_version_changes_address() -> None:
    """Changing only the extractor version => different content address."""
    uri = "doc://example.test/lineage"
    digest = "sha256:" + "cd" * 32
    a = external_reference_content_hash(uri, digest=digest, extractor="html-readability@2.4.1")
    b = external_reference_content_hash(uri, digest=digest, extractor="html-readability@2.5.0")
    assert a != b


def test_external_reference_extractor_changes_address_from_absent() -> None:
    """Adding an extractor to a reference that had none changes the address."""
    uri = "doc://example.test/lineage"
    digest = "sha256:" + "cd" * 32
    plain = external_reference_content_hash(uri, digest=digest)
    extracted = external_reference_content_hash(uri, digest=digest, extractor="html-readability@2.4.1")
    assert plain != extracted


def test_external_reference_extractor_joins_canonical_document() -> None:
    """The extractor identity lands inside the hashed document (sorted keys)."""
    payload = external_reference_bytes(
        "pr://github.com/acme/widget/1", digest="sha1:" + "cd" * 20, extractor="git@1.0.0"
    )
    assert payload == (
        b'{"artifact_uri":"pr://github.com/acme/widget/1","digest":"sha1:'
        + b"cd" * 20
        + b'","extractor":"git@1.0.0","v":1}'
    )


def test_external_reference_empty_extractor_is_refused() -> None:
    """An empty-string extractor is refused: absent and empty must not
    collapse into the same serialised document."""
    with pytest.raises(ArtifactURIError):
        external_reference_bytes("pkg://pypi/x/1.0", digest="sha256:" + "ab" * 32, extractor="")
