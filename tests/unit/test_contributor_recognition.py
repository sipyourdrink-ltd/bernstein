"""The recognition script decides who is listed and how; that logic is pinned here.

Every test is offline: the GitHub client is a fake keyed by path, and the
render functions are pure. The invariants that matter to a person are the
ones tested first: windows, merged-only, alphabetical order, who is never
credited, and that nothing in the drafts is invented.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "contributor_recognition.py"
_spec = importlib.util.spec_from_file_location("contributor_recognition", SCRIPT)
assert _spec is not None and _spec.loader is not None
cr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cr  # dataclasses resolve annotations through sys.modules
_spec.loader.exec_module(cr)

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
REPO = "o/r"


def pr(
    number: int,
    login: str,
    merged_at: str | None,
    updated_at: str | None = None,
    *,
    kind: str = "User",
    base: str = "main",
    title: str | None = None,
):
    return {
        "number": number,
        "title": title or f"pr {number}",
        "merged_at": merged_at,
        "updated_at": updated_at or merged_at or "2026-09-26T00:00:00Z",
        "user": {"login": login, "type": kind},
        "base": {"ref": base},
    }


class FakeClient:
    """Answers ``repos/o/r/pulls`` and ``repos/o/r/pulls/<n>/files`` from dicts."""

    def __init__(self, pulls: list[dict], files: dict[int, list[dict]] | None = None):
        self.pulls = pulls
        self.files = files or {}
        self.calls: list[str] = []

    def get(self, path: str, params: dict[str, str] | None = None):
        self.calls.append(path)
        page = int((params or {}).get("page", "1"))
        if path == f"repos/{REPO}/pulls":
            items = self.pulls[(page - 1) * 100 : page * 100]
            return items, {}
        if path.startswith(f"repos/{REPO}/pulls/") and path.endswith("/files"):
            number = int(path.split("/")[-2])
            items = self.files.get(number, [])[(page - 1) * 100 : page * 100]
            return items, {}
        raise AssertionError(f"unexpected path {path}")


def registry(*rows: dict, agents: tuple[str, ...] = ()) -> cr.Registry:
    reg = cr.Registry(extra_agents=frozenset(agents))
    for row in rows:
        o = cr.OptIn(**row)
        reg.opt_ins[o.login.casefold()] = o
    return reg


def f(name: str, additions: int) -> dict:
    return {"filename": name, "additions": additions}


# ---------------------------------------------------------------- windows and filters


class TestCollectWindows:
    def test_cohort_is_90_days_and_gate_is_30_days(self):
        pulls = [
            pr(1, "ann", "2026-09-20T00:00:00Z"),  # inside both windows
            pr(2, "ann", "2026-08-01T00:00:00Z"),  # inside 90 d, outside 30 d
            pr(3, "bob", "2026-06-01T00:00:00Z", "2026-06-02T00:00:00Z"),  # outside 90 d
        ]
        files = {1: [f("src/a.py", 120)], 2: [f("src/b.py", 999)]}
        state = cr.collect(FakeClient(pulls, files), REPO, NOW, registry(), {})
        logins = [c["login"] for c in state["contributors"]]
        assert logins == ["ann"]
        ann = state["contributors"][0]
        assert [p["number"] for p in ann["merged_prs_90d"]] == [1, 2]
        assert ann["added_30d"] == 120 and ann["prs_30d"] == 1

    def test_closed_but_unmerged_prs_do_not_count(self):
        pulls = [pr(1, "ann", None, "2026-09-25T00:00:00Z")]
        state = cr.collect(FakeClient(pulls), REPO, NOW, registry(), {})
        assert state["contributors"] == []

    def test_maintainer_bots_and_agent_lanes_are_never_credited(self):
        pulls = [
            pr(1, cr.MAINTAINER_LOGIN, "2026-09-20T00:00:00Z"),
            pr(2, "renovate[bot]", "2026-09-20T00:00:00Z", kind="Bot"),
            pr(3, "sujeito-operator", "2026-09-20T00:00:00Z"),
            pr(4, "lane-42", "2026-09-20T00:00:00Z"),
            pr(5, "carol", "2026-09-20T00:00:00Z"),
        ]
        files = {n: [f("src/x.py", 10)] for n in range(1, 6)}
        state = cr.collect(FakeClient(pulls, files), REPO, NOW, registry(agents=("lane-42",)), {})
        assert [c["login"] for c in state["contributors"]] == ["carol"]

    def test_a_pr_into_another_branch_is_not_a_main_merge(self):
        pulls = [pr(1, "ann", "2026-09-20T00:00:00Z", base="feat/stack")]
        state = cr.collect(FakeClient(pulls), REPO, NOW, registry(), {})
        assert state["contributors"] == []

    def test_paging_stops_once_updates_predate_the_window(self):
        old = [pr(1000 + i, "zed", "2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z") for i in range(100)]
        recent = [pr(1, "ann", "2026-09-20T00:00:00Z")]
        client = FakeClient(recent + old, {1: [f("src/a.py", 1)]})
        cr.collect(client, REPO, NOW, registry(), {})
        assert client.calls.count(f"repos/{REPO}/pulls") == 1  # page 1's oldest update already predates the window

    def test_malformed_payload_fails_closed(self):
        class Broken(FakeClient):
            def get(self, path, params=None):
                return {"message": "nope"}, {}

        with pytest.raises(cr.RecognitionError):
            cr.collect(Broken([]), REPO, NOW, registry(), {})


class TestAdditions:
    def test_generated_paths_are_excluded_from_the_gate_count(self):
        pulls = [pr(1, "ann", "2026-09-20T00:00:00Z")]
        files = {
            1: [f("src/a.py", 300), f("uv.lock", 5000), f("docs/i18n/README.ru.md", 40), f("web/dist/app.js", 900)]
        }
        state = cr.collect(FakeClient(pulls, files), REPO, NOW, registry(), {})
        ann = state["contributors"][0]
        assert ann["added_30d"] == 300 and ann["added_30d_raw"] == 6240

    def test_cache_hit_skips_the_files_call(self):
        pulls = [pr(1, "ann", "2026-09-20T00:00:00Z")]
        cache = {"1": {"merged_at": "2026-09-20T00:00:00Z", "counted": 42, "raw": 42, "files": 1}}
        client = FakeClient(pulls, {1: [f("src/a.py", 9999)]})
        state = cr.collect(client, REPO, NOW, registry(), cache)
        assert state["contributors"][0]["added_30d"] == 42
        assert not any(c.endswith("/files") for c in client.calls)

    @pytest.mark.parametrize(
        "path",
        ["uv.lock", "package-lock.json", "web/package-lock.json", "docs/api/openapi.json", "a/b/x_pb2.py", "logo.svg"],
    )
    def test_generated_patterns(self, path):
        assert cr.is_generated(path)

    def test_source_paths_are_not_generated(self):
        assert not cr.is_generated("src/bernstein/core/x.py")
        assert not cr.is_generated("docs/operations/foo.md")


# ---------------------------------------------------------------- ordering and registry


def test_sort_is_case_insensitive_and_deterministic():
    assert cr.sort_logins(["bob", "Ann", "carol", "ann2", "Bob"]) == ["Ann", "ann2", "Bob", "bob", "carol"]


def test_registry_rejects_duplicates_and_bad_logins(tmp_path: Path):
    p = tmp_path / "o.toml"
    p.write_text('[[contributor]]\nlogin = "Ann"\nopt_in = true\n[[contributor]]\nlogin = "ann"\nopt_in = false\n')
    with pytest.raises(cr.RecognitionError, match="twice"):
        cr.load_registry(p)
    p.write_text('[[contributor]]\nlogin = "not a login!"\nopt_in = true\n')
    with pytest.raises(cr.RecognitionError, match="invalid login"):
        cr.load_registry(p)


def test_registry_reads_flags_and_exclusions(tmp_path: Path):
    p = tmp_path / "o.toml"
    p.write_text(
        '[exclusions]\nagent_logins = ["lane-1"]\n'
        '[[contributor]]\nlogin = "ann"\nopt_in = true\nrecommendation = true\nreview_before_publish = true\n'
        'source = "email BRNSTN-PR-LNKD 2026-09-30"\n'
    )
    reg = cr.load_registry(p)
    assert reg.extra_agents == frozenset({"lane-1"})
    ann = reg.get("ANN")
    assert ann and ann.opt_in and ann.recommendation and ann.review_before_publish


def test_shipped_registry_loads_and_seeds_the_5524_opt_ins():
    reg = cr.load_registry(Path(__file__).resolve().parents[2] / "community" / "recognition" / "opt-ins.toml")
    for login in ("Phoenix1504e", "PARZIVAL7498", "thegoodengineer", "Maqbool61", "atirna", "Silentpartnercoding"):
        rec = reg.get(login)
        assert rec and rec.opt_in and "5524" in rec.source
    assert reg.get("Silentpartnercoding").review_before_publish


# ---------------------------------------------------------------- render


def state_fixture() -> dict:
    return {
        "schema": 1,
        "repo": REPO,
        "generated_at": "2026-09-26T12:00:00Z",
        "cohort_since": "2026-06-28T12:00:00Z",
        "gate_since": "2026-08-27T12:00:00Z",
        "gate_days": 30,
        "cohort_days": 90,
        "contributors": [
            {
                "login": "Ann",
                "merged_prs_90d": [{"number": 1, "title": "fix(x): a", "merged_at": "2026-09-20T00:00:00Z"}],
                "added_30d": 1500,
                "added_30d_raw": 1600,
                "prs_30d": 1,
            },
            {
                "login": "bob",
                "merged_prs_90d": [{"number": 2, "title": "docs: b", "merged_at": "2026-09-21T00:00:00Z"}],
                "added_30d": 40,
                "added_30d_raw": 40,
                "prs_30d": 1,
            },
            {
                "login": "carol",
                "merged_prs_90d": [{"number": 3, "title": "feat: c", "merged_at": "2026-09-22T00:00:00Z"}],
                "added_30d": 2000,
                "added_30d_raw": 2000,
                "prs_30d": 1,
            },
        ],
    }


class TestRender:
    def test_period_comment_is_alphabetical_and_marked(self):
        reg = registry({"login": "carol", "opt_in": True})
        text = cr.render_period_comment(state_fixture(), reg)
        assert text.startswith("<!-- tool:recognition-period:2026-09-26:v1 -->")
        assert text.index("@Ann") < text.index("@carol")
        assert "@bob (1)" in text  # below the gate, still named
        assert "| @Ann | not yet |" in text and "| @carol | yes |" in text

    def test_period_comment_carries_the_exception_path_and_the_exact_subject(self):
        text = cr.render_period_comment(state_fixture(), registry())
        assert cr.EMAIL_ADDRESS in text and "`BRNSTN-PR-LNKD`" in text

    def test_linkedin_draft_names_only_opted_in_people_above_the_gate_or_with_an_exception(self):
        reg = registry(
            {"login": "carol", "opt_in": True},
            {"login": "bob", "opt_in": True, "exception": True},
            {"login": "Ann", "opt_in": False},
        )
        text = cr.render_linkedin_draft(state_fixture(), reg)
        assert "@bob" in text and "@carol" in text and "@Ann" not in text
        assert text.index("@bob") < text.index("@carol")
        assert "<one sentence, from: fix(x)" not in text  # Ann is out entirely
        assert "<one sentence, from: feat: c>" in text  # facts, no invented praise

    def test_linkedin_draft_flags_review_before_publish(self):
        reg = registry({"login": "carol", "opt_in": True, "review_before_publish": True})
        assert "Send the final wording before publishing to: @carol" in cr.render_linkedin_draft(state_fixture(), reg)

    def test_render_is_deterministic(self):
        reg = registry({"login": "carol", "opt_in": True})
        a = cr.render_period_comment(state_fixture(), reg) + cr.render_linkedin_draft(state_fixture(), reg)
        b = cr.render_period_comment(json.loads(json.dumps(state_fixture())), reg) + cr.render_linkedin_draft(
            state_fixture(), reg
        )
        assert a == b

    def test_empty_state_renders_without_crashing(self):
        st = state_fixture()
        st["contributors"] = []
        assert "Nobody cleared the gate" in cr.render_period_comment(st, registry())
        assert "No opted-in contributor cleared the gate" in cr.render_linkedin_draft(st, registry())


# ---------------------------------------------------------------- replies


class TestParseReply:
    def test_minimal_valid_reply(self):
        parsed = cr.parse_reply("GITHUB=Ann-Dev\nOPT_IN=YES\n")
        assert parsed["ok"] and parsed["fields"] == {
            "GITHUB": "Ann-Dev",
            "OPT_IN": "YES",
            "RECOMMENDATION": "NO",
            "NOTE": "",
        }

    def test_case_quotes_and_at_sign_are_tolerated_and_first_occurrence_wins(self):
        text = "Hi!\n> GITHUB=old\ngithub = @Ann\nopt_in = yes\nOPT_IN=NO\nrecommendation=YES\nNOTE= show me the wording first \n"
        parsed = cr.parse_reply(text)
        assert parsed["ok"]
        assert parsed["fields"]["GITHUB"] == "Ann"  # the quoted template line is skipped
        assert parsed["fields"]["OPT_IN"] == "YES"
        assert parsed["fields"]["RECOMMENDATION"] == "YES"
        assert parsed["fields"]["NOTE"] == "show me the wording first"

    def test_missing_or_invalid_fields_are_reported_not_guessed(self):
        parsed = cr.parse_reply("OPT_IN=MAYBE\n")
        assert not parsed["ok"]
        assert any("GITHUB" in e for e in parsed["errors"]) and any("OPT_IN" in e for e in parsed["errors"])

    def test_registry_entry_is_valid_toml(self):
        import tomllib

        parsed = cr.parse_reply('GITHUB=ann\nOPT_IN=YES\nNOTE=he said "hi"\n')
        block = cr.registry_entry(parsed, "2026-09-30")
        data = tomllib.loads(block)
        assert data["contributor"][0]["login"] == "ann" and data["contributor"][0]["opt_in"] is True
        assert "BRNSTN-PR-LNKD" in data["contributor"][0]["source"]

    def test_exact_subject_constant(self):
        assert cr.EMAIL_SUBJECT == "BRNSTN-PR-LNKD"
        assert cr.EMAIL_ADDRESS == "forte@bernstein.run"


# ---------------------------------------------------------------- translations and publishing


class TestTranslations:
    def test_one_collapsible_comment_per_language_in_fixed_order(self):
        codes = [c for c, *_ in cr.LANGUAGES]
        assert codes == ["hi", "zh-Hans", "vi", "es", "pt", "ru", "uk", "pl"]
        assert len(codes) == len(set(codes))
        body = cr.translation_comment("pl", "Cześć.\n")
        assert body.startswith(
            "<!-- tool:recognition-translation:pl:v1 -->\n<details>\n<summary>🇵🇱 Polskie tłumaczenie</summary>"
        )
        assert body.rstrip().endswith("</details>")
        assert "Cześć." in body

    def test_unknown_language_is_refused(self):
        with pytest.raises(cr.RecognitionError):
            cr.translation_comment("xx", "…")

    def test_switcher_links_every_language_and_uses_anchors_when_known(self):
        before = cr.language_switcher(None)
        assert before.count("](#)") == len(cr.LANGUAGES)
        after = cr.language_switcher({"pl": "#issuecomment-123"})
        assert "[🇵🇱 Polski](#issuecomment-123)" in after and "[🇷🇺 Русский](#)" in after

    def test_placeholders_are_replaced(self):
        body = "{{LANGUAGE_SWITCHER}}\n\nhello\n\n{{REPLY_BY}}\n"
        out = cr.apply_placeholders(body, "SW", "RB")
        assert out == "SW\n\nhello\n\nRB\n"

    def test_shipped_issue_body_carries_both_placeholders_and_the_email_block(self):
        text = (Path(__file__).resolve().parents[2] / "community" / "recognition" / "ISSUE.md").read_text(
            encoding="utf-8"
        )
        assert cr.SWITCHER_PLACEHOLDER in text and cr.REPLY_BY_PLACEHOLDER in text
        assert "forte@bernstein.run" in text and "`BRNSTN-PR-LNKD`" in text
        assert "OPT_IN=YES" in text


class TestReplyBy:
    def test_fourteen_days_at_20_israel_in_four_zones(self):
        times = cr.reply_by(datetime(2026, 10, 1, 9, 0, tzinfo=UTC))
        assert times["Israel"].endswith("20:00") and "15 Oct 2026" in times["Israel"]
        assert times["UTC"].endswith("17:00")  # IDT is UTC+3 in mid-October
        assert set(times) == {"Israel", "India", "Central Europe", "UTC"}


class TestPublish:
    def test_publish_issue_dry_run_posts_nothing(self):
        calls = []
        plan = cr.publish_issue(
            REPO,
            "t",
            "{{LANGUAGE_SWITCHER}}\nx\n{{REPLY_BY}}",
            {"pl": "Cześć"},
            NOW,
            ["pinned"],
            yes=False,
            runner=lambda *a, **k: calls.append(a),
        )
        assert calls == [] and plan["languages"] == ["pl"]

    def test_publish_issue_creates_comments_then_patches_the_switcher_with_real_anchors(self):
        calls = []

        def runner(args, payload=None):
            calls.append((args, payload))
            if args[:2] == ["-X", "POST"] and args[2].endswith("/issues"):
                return {"number": 7, "html_url": "https://github.com/o/r/issues/7"}
            if "comments" in args[2]:
                return {"id": 900 + len(calls), "html_url": "https://github.com/o/r/issues/7#issuecomment-9"}
            return {}

        plan = cr.publish_issue(
            REPO,
            "t",
            "{{LANGUAGE_SWITCHER}}\nx\n{{REPLY_BY}}",
            {"pl": "Cześć", "ru": "Привет"},
            NOW,
            ["pinned"],
            yes=True,
            runner=runner,
        )
        kinds = [a[2].rsplit("/", 1)[-1] for a, _ in calls]
        assert kinds == ["issues", "comments", "comments", "7"]  # create, two comments, patch
        posted_codes = [p["body"].split(":")[2] for a, p in calls[1:3]]
        assert posted_codes == ["ru", "pl"]  # fixed order from LANGUAGES: ru precedes pl
        final_body = calls[-1][1]["body"]
        assert (
            "#issuecomment-" in final_body
            and "{{LANGUAGE_SWITCHER}}" not in final_body
            and "{{REPLY_BY}}" not in final_body
        )
        assert plan["number"] == 7

    def test_publish_comment_is_idempotent_per_period(self):
        body = cr.render_period_comment(state_fixture(), registry())
        existing = [{"body": body}]
        res = cr.publish_comment(
            REPO,
            7,
            body,
            yes=True,
            runner=lambda args, payload=None: existing if payload is None else {"html_url": "u"},
        )
        assert "skipped" in res


def _c(login: str, added: int, prs_30d: int) -> dict:
    return {
        "login": login,
        "added_30d": added,
        "added_30d_raw": added,
        "prs_30d": prs_30d,
        "merged_prs_90d": [{"number": 1, "title": "t", "merged_at": "2026-09-01T00:00:00Z"}],
    }


def test_mentions_ping_the_gate_the_opt_ins_and_recent_mergers_only() -> None:
    reg = cr.Registry(opt_ins={"optedin": cr.OptIn(login="optedin", opt_in=True)})
    above = [_c("big", 2000, 3)]
    below = [_c("recent", 10, 1), _c("old", 10, 0), _c("optedin", 10, 0)]
    assert cr.mention_set(above, below, reg) == {"big", "recent", "optedin"}
    state = {
        "repo": REPO,
        "generated_at": "2026-09-26T00:00:00Z",
        "cohort_since": "2026-06-28T00:00:00Z",
        "gate_since": "2026-08-27T00:00:00Z",
        "gate_days": 30,
        "contributors": above + below,
    }
    body = cr.render_period_comment(state, reg)
    assert "@big" in body and "@recent" in body and "@optedin" in body
    assert "@old" not in body and "[old](https://github.com/old)" in body


def test_mentions_fall_back_to_gate_and_opt_ins_above_the_cap() -> None:
    reg = cr.Registry(opt_ins={"optedin": cr.OptIn(login="optedin", opt_in=True)})
    above = [_c("big", 2000, 3)]
    below = [_c(f"u{i:03d}", 10, 1) for i in range(cr.MENTION_CAP)] + [_c("optedin", 10, 0)]
    assert cr.mention_set(above, below, reg) == {"big", "optedin"}
