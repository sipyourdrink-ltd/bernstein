"""``bernstein doctor`` reports secrets once, from the check that works.

Two checks used to answer for secrets. ``_doctor_check_secrets`` reads the
``secrets:`` block straight out of YAML and probes the provider through
``check_provider_connectivity``. ``_doctor_check_secrets_yaml`` re-read the
same block through ``parse_seed`` and called ``check_secrets_connectivity``,
a name that is not defined anywhere in the tree, inside a broad
``except Exception``. Neither of its two outcomes was correct:

* On the seed ``bernstein init`` writes -- ``goal:`` still commented out,
  which is the documented state until the operator fills it in -- ``parse_seed``
  raised and the row read "configuration error: Seed file must contain a
  non-empty 'goal' string" with the remedy "Check bernstein.yaml syntax".
  ``init`` then ``doctor`` is the first thing a new operator runs.
* With a ``secrets:`` block actually configured, the undefined name raised
  ``ImportError``, which the same broad clause reported as "configuration
  error: cannot import name 'check_secrets_connectivity'" -- again blaming the
  YAML -- while the working check sat next to it printing a real verdict.

The redundant check is gone. These tests pin that one row survives, that it
comes from the working check, and that a freshly initialised workspace is not
reported as broken.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bernstein.cli.commands import status_cmd

# The template ``bernstein init`` writes (see ``_default_seed_yaml`` in
# ``cli/run_bootstrap``): every key set except the goal, which stays
# commented out until the operator edits it.
INIT_TEMPLATE = """\
# Bernstein orchestration config
# Uncomment and edit the goal, then run: bernstein

# goal: "Describe what you want the agents to build or improve"

cli: auto  # Bernstein picks the best agent per task
team: auto
budget: "$10"
"""

CONFIGURED_SECRETS = "secrets:\n  provider: vault\n  path: secret/data/bernstein\n"


def _secrets_rows(workdir: Path) -> list[dict[str, Any]]:
    """Every row the secrets check contributes for ``workdir``."""
    rows: list[dict[str, Any]] = []

    def _check(name: str, ok: bool, detail: str, fix: str = "") -> None:
        rows.append({"name": name, "ok": ok, "detail": detail, "fix": fix})

    status_cmd._doctor_check_secrets(workdir, _check)
    return rows


class TestDoctorSecretsRow:
    def test_freshly_initialised_workspace_is_not_reported_broken(self, tmp_path: Path) -> None:
        """The regression: init -> doctor must not flag a correct workspace."""
        (tmp_path / "bernstein.yaml").write_text(INIT_TEMPLATE, encoding="utf-8")

        rows = _secrets_rows(tmp_path)

        assert len(rows) == 1, rows
        assert rows[0]["ok"] is True, rows[0]
        assert rows[0]["detail"] == "not configured (using env vars)"
        # The seed's goal is none of this row's business.
        assert "goal" not in rows[0]["detail"]

    def test_missing_bernstein_yaml_is_not_reported_broken(self, tmp_path: Path) -> None:
        rows = _secrets_rows(tmp_path)

        assert len(rows) == 1
        assert rows[0]["ok"] is True

    def test_configured_provider_gets_a_real_verdict(self, tmp_path: Path, monkeypatch: Any) -> None:
        """A configured block reaches the probe that actually exists."""
        (tmp_path / "bernstein.yaml").write_text(CONFIGURED_SECRETS, encoding="utf-8")
        seen: list[Any] = []

        def _fake_probe(cfg: Any) -> tuple[bool, str]:
            seen.append(cfg)
            return False, "connection refused"

        import bernstein.core.secrets as secrets_mod

        monkeypatch.setattr(secrets_mod, "check_provider_connectivity", _fake_probe)

        rows = _secrets_rows(tmp_path)

        assert seen and seen[0].provider == "vault"
        assert len(rows) == 1
        assert rows[0]["name"] == "Secrets: vault"
        assert rows[0]["ok"] is False
        assert rows[0]["detail"] == "connection refused"

    def test_config_yaml_takes_precedence_over_the_seed(self, tmp_path: Path, monkeypatch: Any) -> None:
        """The surviving check reads ``.sdd/config.yaml`` first; the removed one never did."""
        (tmp_path / ".sdd").mkdir()
        (tmp_path / ".sdd" / "config.yaml").write_text(
            "secrets:\n  provider: aws\n  path: bernstein/prod\n", encoding="utf-8"
        )
        (tmp_path / "bernstein.yaml").write_text(CONFIGURED_SECRETS, encoding="utf-8")

        import bernstein.core.secrets as secrets_mod

        monkeypatch.setattr(secrets_mod, "check_provider_connectivity", lambda cfg: (True, "ok"))

        rows = _secrets_rows(tmp_path)

        assert len(rows) == 1
        assert rows[0]["name"] == "Secrets: aws"


class TestRemovedCheckStaysRemoved:
    def test_no_second_secrets_check_exists(self) -> None:
        """A duplicate row is how the broken check reached operators unnoticed."""
        assert not hasattr(status_cmd, "_doctor_check_secrets_yaml")

    def test_undefined_helper_is_no_longer_referenced(self) -> None:
        """``check_secrets_connectivity`` is defined nowhere; nothing may call it."""
        source = Path(status_cmd.__file__).read_text(encoding="utf-8")
        assert "check_secrets_connectivity" not in source
