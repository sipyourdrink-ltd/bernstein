"""Tests for bernstein.core.teams.manifest (issue #2248).

Covers the canonical TOML serialization, the content digest, manifest
loading/validation, reference parsing, resolution order, pure expansion
(acceptance criterion 1), and the Ed25519 signature path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.config.seed_config import SeedError
from bernstein.core.skills.catalog.signature import generate_signer_keypair
from bernstein.core.teams.manifest import (
    TeamCoordination,
    TeamManifest,
    TeamManifestError,
    TeamManifestNotFoundError,
    TeamManifestSignatureError,
    TeamManifestValidationError,
    TeamRoleSpec,
    canonical_toml,
    discover_team_manifest_paths,
    expand_manifest,
    load_team_manifest,
    parse_manifest_ref,
    resolve_team_manifest,
    sign_team_manifest,
    verify_team_manifest,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

BASIC_MANIFEST = """\
name = "review-heavy"
version = "1.0.0"

[coordination]
parallelism = 2
review_chain = true

[[roles]]
role = "backend"
response_profile = "terse"

[roles.model_policy]
model = "sonnet"
effort = "high"

[[roles]]
role = "qa"

[role_template_digests]
"backend" = "aa11223344556677889900aabbccddeeff00112233445566778899aabbccddee"
"""


def _write_manifest(tmp_path: Path, body: str = BASIC_MANIFEST, name: str = "review-heavy") -> Path:
    teams_dir = tmp_path / "templates" / "teams"
    teams_dir.mkdir(parents=True, exist_ok=True)
    path = teams_dir / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


def _basic_manifest() -> TeamManifest:
    return TeamManifest(
        name="review-heavy",
        version="1.0.0",
        roles=(
            TeamRoleSpec(
                role="backend",
                model_policy={"model": "sonnet", "effort": "high"},
                response_profile="terse",
            ),
            TeamRoleSpec(role="qa", model_policy={}),
        ),
        coordination=TeamCoordination(review_chain=True, parallelism=2),
        role_template_digests={
            "backend": "aa11223344556677889900aabbccddeeff00112233445566778899aabbccddee",
        },
    )


# ---------------------------------------------------------------------------
# Canonical serialization + digest
# ---------------------------------------------------------------------------


class TestCanonicalSerialization:
    def test_canonical_toml_round_trips_to_same_digest(self, tmp_path: Path) -> None:
        """Writing the canonical form back to disk and reloading is a fixpoint."""
        manifest = load_team_manifest(_write_manifest(tmp_path))
        rendered = canonical_toml(manifest)
        path = tmp_path / "canon.toml"
        path.write_text(rendered, encoding="utf-8")
        reloaded = load_team_manifest(path)
        assert canonical_toml(reloaded) == rendered
        assert reloaded.digest() == manifest.digest()

    def test_digest_is_line_ending_independent(self, tmp_path: Path) -> None:
        """A CRLF checkout hashes identically to an LF checkout."""
        lf = load_team_manifest(_write_manifest(tmp_path))
        crlf_path = tmp_path / "crlf.toml"
        crlf_path.write_bytes(BASIC_MANIFEST.replace("\n", "\r\n").encode("utf-8"))
        crlf = load_team_manifest(crlf_path)
        assert crlf.digest() == lf.digest()

    def test_digest_is_key_order_independent(self, tmp_path: Path) -> None:
        """Reordering keys inside tables does not change the digest."""
        reordered = BASIC_MANIFEST.replace(
            'model = "sonnet"\neffort = "high"',
            'effort = "high"\nmodel = "sonnet"',
        ).replace('name = "review-heavy"\nversion = "1.0.0"', 'version = "1.0.0"\nname = "review-heavy"')
        a = load_team_manifest(_write_manifest(tmp_path))
        b_path = tmp_path / "reordered.toml"
        b_path.write_text(reordered, encoding="utf-8")
        assert load_team_manifest(b_path).digest() == a.digest()

    def test_digest_changes_on_content_change(self, tmp_path: Path) -> None:
        a = load_team_manifest(_write_manifest(tmp_path))
        b_path = tmp_path / "edited.toml"
        b_path.write_text(BASIC_MANIFEST.replace('effort = "high"', 'effort = "max"'), encoding="utf-8")
        assert load_team_manifest(b_path).digest() != a.digest()

    def test_omitted_coordination_defaults_hash_like_explicit_defaults(self, tmp_path: Path) -> None:
        """Canonicalization normalizes absent optional tables to their defaults."""
        implicit = 'name = "t"\nversion = "1"\n\n[[roles]]\nrole = "qa"\n'
        explicit = implicit + "\n[coordination]\nparallelism = 1\nreview_chain = false\n"
        p1 = tmp_path / "implicit.toml"
        p2 = tmp_path / "explicit.toml"
        p1.write_text(implicit, encoding="utf-8")
        p2.write_text(explicit, encoding="utf-8")
        assert load_team_manifest(p1).digest() == load_team_manifest(p2).digest()

    def test_canonical_toml_uses_lf_and_sorted_keys(self) -> None:
        rendered = canonical_toml(_basic_manifest())
        assert "\r" not in rendered
        assert rendered.endswith("\n")
        # Within the model_policy table, keys appear sorted.
        assert rendered.index('effort = "high"') < rendered.index('model = "sonnet"')


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------


class TestLoadValidation:
    def test_load_parses_all_fields(self, tmp_path: Path) -> None:
        manifest = load_team_manifest(_write_manifest(tmp_path))
        assert manifest.name == "review-heavy"
        assert manifest.version == "1.0.0"
        assert [r.role for r in manifest.roles] == ["backend", "qa"]
        assert manifest.roles[0].model_policy == {"model": "sonnet", "effort": "high"}
        assert manifest.roles[0].response_profile == "terse"
        assert manifest.roles[1].response_profile is None
        assert manifest.coordination == TeamCoordination(review_chain=True, parallelism=2)
        assert "backend" in manifest.role_template_digests

    def test_errors_are_seed_errors(self) -> None:
        """Typed manifest errors must surface through SeedError handlers."""
        assert issubclass(TeamManifestError, SeedError)
        assert issubclass(TeamManifestNotFoundError, TeamManifestError)
        assert issubclass(TeamManifestValidationError, TeamManifestError)

    @pytest.mark.parametrize(
        "body",
        [
            'version = "1"\n\n[[roles]]\nrole = "qa"\n',  # missing name
            'name = "t"\n\n[[roles]]\nrole = "qa"\n',  # missing version
            'name = "t"\nversion = "1"\n',  # missing roles
            'name = "t"\nversion = "1"\nroles = []\n',  # empty roles
            'name = "t"\nversion = "1"\n\n[[roles]]\nrole = ""\n',  # empty role
            'name = "t"\nversion = "1"\nbogus = 1\n\n[[roles]]\nrole = "qa"\n',  # unknown key
            'name = "t"\nversion = "1"\n\n[[roles]]\nrole = "qa"\nbogus = 1\n',  # unknown role key
            ('name = "t"\nversion = "1"\n\n[[roles]]\nrole = "qa"\n\n[[roles]]\nrole = "qa"\n'),  # duplicate
            ('name = "t"\nversion = "1"\n\n[[roles]]\nrole = "qa"\n\n[role_template_digests]\nqa = "zz"\n'),
            (  # digest pinned for an undeclared role
                'name = "t"\nversion = "1"\n\n[[roles]]\nrole = "qa"\n\n[role_template_digests]\n'
                'ghost = "aa11223344556677889900aabbccddeeff00112233445566778899aabbccddee"\n'
            ),
            'name = "t"\nversion = "1"\n\n[coordination]\nparallelism = 0\n\n[[roles]]\nrole = "qa"\n',
            'name = "t"\nversion = "1"\n\n[[roles]]\nrole = "qa"\nresponse_profile = 3\n',
        ],
    )
    def test_invalid_manifests_raise_typed_error(self, tmp_path: Path, body: str) -> None:
        path = tmp_path / "bad.toml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(TeamManifestValidationError):
            load_team_manifest(path)

    def test_missing_file_raises_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(TeamManifestNotFoundError):
            load_team_manifest(tmp_path / "absent.toml")

    def test_model_policy_rejects_bool_values(self, tmp_path: Path) -> None:
        body = 'name = "t"\nversion = "1"\n\n[[roles]]\nrole = "qa"\n\n[roles.model_policy]\nmodel = true\n'
        path = tmp_path / "bad.toml"
        path.write_text(body, encoding="utf-8")
        with pytest.raises(TeamManifestValidationError):
            load_team_manifest(path)


# ---------------------------------------------------------------------------
# Reference parsing + resolution
# ---------------------------------------------------------------------------


class TestRefAndResolution:
    def test_parse_plain_name(self) -> None:
        assert parse_manifest_ref("python") == ("python", None)

    def test_parse_name_with_digest(self) -> None:
        digest = "ab" * 32
        assert parse_manifest_ref(f"python@{digest.upper()}") == ("python", digest)

    @pytest.mark.parametrize("ref", ["", "  ", "a/b", "python@zz", "python@abc", "@" + "ab" * 32])
    def test_invalid_refs_raise(self, ref: str) -> None:
        with pytest.raises(TeamManifestValidationError):
            parse_manifest_ref(ref)

    def test_resolve_prefers_workdir_over_builtin(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path, BASIC_MANIFEST.replace('name = "review-heavy"', 'name = "python"'), name="python")
        manifest = resolve_team_manifest("python", workdir=tmp_path)
        assert manifest.source_path is not None
        assert manifest.source_path.is_relative_to(tmp_path)

    def test_resolve_falls_back_to_builtin(self, tmp_path: Path) -> None:
        manifest = resolve_team_manifest("python", workdir=tmp_path)
        assert manifest.name == "python"

    def test_resolve_unknown_name_raises_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(TeamManifestNotFoundError):
            resolve_team_manifest("no-such-team", workdir=tmp_path)

    def test_discover_lists_local_and_builtin(self, tmp_path: Path) -> None:
        _write_manifest(tmp_path)
        paths = discover_team_manifest_paths(tmp_path)
        assert "review-heavy" in paths
        assert "python" in paths
        assert paths["review-heavy"].is_relative_to(tmp_path)


# ---------------------------------------------------------------------------
# Expansion (acceptance criterion 1)
# ---------------------------------------------------------------------------


class TestExpansion:
    def test_expansion_produces_existing_structures(self) -> None:
        expanded = expand_manifest(_basic_manifest())
        assert expanded.team == ["backend", "qa"]
        assert expanded.role_model_policy == {
            "backend": {"model": "sonnet", "effort": "high", "response_style": "terse"},
        }

    def test_expansion_is_pure_and_deterministic(self) -> None:
        manifest = _basic_manifest()
        first = expand_manifest(manifest)
        second = expand_manifest(manifest)
        assert first == second
        # Mutating one expansion must not leak into the next.
        first.team.append("intruder")
        first.role_model_policy["backend"]["model"] = "changed"
        third = expand_manifest(manifest)
        assert third.team == ["backend", "qa"]
        assert third.role_model_policy["backend"]["model"] == "sonnet"


# ---------------------------------------------------------------------------
# Ed25519 signature path (reused from the skills catalog)
# ---------------------------------------------------------------------------


class TestSignature:
    def test_sign_and_verify_round_trip(self, tmp_path: Path) -> None:
        priv, pub = generate_signer_keypair()
        manifest = load_team_manifest(_write_manifest(tmp_path))
        signature = sign_team_manifest(manifest, priv)
        outcome = verify_team_manifest(manifest, signature, pub)
        assert outcome.verified

    def test_signed_manifest_file_verifies_on_load(self, tmp_path: Path) -> None:
        priv, pub = generate_signer_keypair()
        manifest = load_team_manifest(_write_manifest(tmp_path))
        signature = sign_team_manifest(manifest, priv)
        # Signature keys are top-level TOML keys, so they precede any table.
        signed_body = f'signature = "{signature}"\nsigner_pubkey = """{pub}"""\n' + BASIC_MANIFEST
        path = tmp_path / "signed.toml"
        path.write_text(signed_body, encoding="utf-8")
        loaded = load_team_manifest(path)
        # Signature keys are excluded from the canonical form.
        assert loaded.digest() == manifest.digest()

    def test_tampered_signed_manifest_refuses_to_load(self, tmp_path: Path) -> None:
        priv, pub = generate_signer_keypair()
        manifest = load_team_manifest(_write_manifest(tmp_path))
        signature = sign_team_manifest(manifest, priv)
        tampered_body = f'signature = "{signature}"\nsigner_pubkey = """{pub}"""\n' + BASIC_MANIFEST.replace(
            'effort = "high"', 'effort = "max"'
        )
        path = tmp_path / "tampered.toml"
        path.write_text(tampered_body, encoding="utf-8")
        with pytest.raises(TeamManifestSignatureError):
            load_team_manifest(path)

    def test_signature_without_pubkey_refuses_to_load(self, tmp_path: Path) -> None:
        priv, _pub = generate_signer_keypair()
        manifest = load_team_manifest(_write_manifest(tmp_path))
        signature = sign_team_manifest(manifest, priv)
        path = tmp_path / "nokey.toml"
        path.write_text(f'signature = "{signature}"\n' + BASIC_MANIFEST, encoding="utf-8")
        with pytest.raises(TeamManifestSignatureError):
            load_team_manifest(path)
