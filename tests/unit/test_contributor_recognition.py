"""The recognition script decides who is listed and how; that logic is pinned here.

Every test is offline: the GitHub client is a fake GraphQL search, and the
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
    *,
    kind: str = "User",
    base: str = "main",
    title: str | None = None,
    files: list[dict] | None = None,
    changed_files: int | None = None,
    additions: int | None = None,
):
    """One PullRequest node as the GraphQL search returns it."""
    files = files if files is not None else [f("src/x.py", 10)]
    return {
        "number": number,
        "title": title or f"pr {number}",
        "mergedAt": merged_at,
        "baseRefName": base,
        "author": {"login": login, "__typename": "Bot" if kind == "Bot" else "User"},
        "changedFiles": changed_files if changed_files is not None else len(files),
        "additions": additions if additions is not None else sum(x["additions"] for x in files),
        "files": {"nodes": files},
    }


class FakeClient:
    """Answers the merged-PR search from a list of nodes, honouring the merged:<from>..<to> range and paging by 50."""

    def __init__(self, nodes: list[dict], issue_count: int | None = None):
        self.nodes = nodes
        self.issue_count = issue_count
        self.queries: list[tuple[str, str | None]] = []

    def graphql(self, query: str, variables: dict):
        q, after = variables["q"], variables.get("after")
        self.queries.append((q, after))
        lo, hi = q.split("merged:")[1].split("..")
        hits = [n for n in self.nodes if n["mergedAt"] and lo <= n["mergedAt"] <= hi]
        offset = int(after or 0)
        page = hits[offset : offset + 50]
        more = offset + 50 < len(hits)
        return {
            "data": {
                "search": {
                    "issueCount": self.issue_count if self.issue_count is not None else len(hits),
                    "pageInfo": {"hasNextPage": more, "endCursor": str(offset + 50) if more else None},
                    "nodes": page,
                }
            }
        }


def registry(*rows: dict, agents: tuple[str, ...] = ()) -> cr.Registry:
    reg = cr.Registry(extra_agents=frozenset(agents))
    for row in rows:
        o = cr.OptIn(**row)
        reg.opt_ins[o.login.casefold()] = o
    return reg


def f(name: str, additions: int) -> dict:
    return {"path": name, "additions": additions}


# ---------------------------------------------------------------- windows and filters


class TestCollectWindows:
    def test_cohort_is_90_days_and_gate_is_30_days(self):
        nodes = [
            pr(1, "ann", "2026-09-20T00:00:00Z", files=[f("src/a.py", 120)]),  # inside both windows
            pr(2, "ann", "2026-08-01T00:00:00Z", files=[f("src/b.py", 999)]),  # inside 90 d, outside 30 d
            pr(3, "bob", "2026-06-01T00:00:00Z"),  # outside 90 d
        ]
        state = cr.collect(FakeClient(nodes), REPO, NOW, registry(), {})
        logins = [c["login"] for c in state["contributors"]]
        assert logins == ["ann"]
        ann = state["contributors"][0]
        assert [p["number"] for p in ann["merged_prs_90d"]] == [1, 2]
        assert ann["added_30d"] == 120 and ann["prs_30d"] == 1

    def test_unmerged_nodes_do_not_count(self):
        client = FakeClient([])
        client.nodes = [pr(1, "ann", None)]
        client.graphql = lambda q, v: {  # a search that leaks an unmerged PR still credits nobody
            "data": {"search": {"issueCount": 1, "pageInfo": {"hasNextPage": False}, "nodes": client.nodes}}
        }
        state = cr.collect(client, REPO, NOW, registry(), {})
        assert state["contributors"] == []

    def test_maintainer_bots_agent_lanes_and_deleted_accounts_are_never_credited(self):
        nodes = [
            pr(1, cr.MAINTAINER_LOGIN, "2026-09-20T00:00:00Z"),
            pr(2, "renovate[bot]", "2026-09-20T00:00:00Z", kind="Bot"),
            pr(3, "sujeito-operator", "2026-09-20T00:00:00Z"),
            pr(4, "lane-42", "2026-09-20T00:00:00Z"),
            pr(5, "carol", "2026-09-20T00:00:00Z"),
            {**pr(6, "x", "2026-09-20T00:00:00Z"), "author": None},
        ]
        state = cr.collect(FakeClient(nodes), REPO, NOW, registry(agents=("lane-42",)), {})
        assert [c["login"] for c in state["contributors"]] == ["carol"]

    def test_a_pr_into_another_branch_is_not_a_main_merge(self):
        nodes = [pr(1, "ann", "2026-09-20T00:00:00Z", base="feat/stack")]
        state = cr.collect(FakeClient(nodes), REPO, NOW, registry(), {})
        assert state["contributors"] == []

    def test_search_is_sliced_into_windows_and_follows_cursors(self):
        nodes = [pr(i, f"u{i % 7}", "2026-09-20T00:00:00Z") for i in range(1, 121)]
        client = FakeClient(nodes)
        state = cr.collect(client, REPO, NOW, registry(), {})
        slices = {q for q, _ in client.queries}
        assert len(slices) == cr.COHORT_DAYS // cr.SLICE_DAYS
        assert sum(len(c["merged_prs_90d"]) for c in state["contributors"]) == 120
        assert ("repo:o/r is:pr is:merged merged:2026-09-16T12:00:00Z..2026-09-26T12:00:00Z", "50") in client.queries

    def test_a_pr_on_a_slice_boundary_is_counted_once(self):
        nodes = [pr(1, "ann", "2026-09-16T12:00:00Z")]
        state = cr.collect(FakeClient(nodes), REPO, NOW, registry(), {})
        assert len(state["contributors"][0]["merged_prs_90d"]) == 1

    def test_a_slice_over_the_search_cap_fails_closed(self):
        with pytest.raises(cr.RecognitionError, match="more than"):
            cr.collect(FakeClient([], issue_count=cr.SEARCH_CAP + 1), REPO, NOW, registry(), {})

    @pytest.mark.parametrize(
        "body",
        [
            {"message": "nope"},
            {"errors": [{"message": "rate limited"}], "data": None},
            {"data": {"search": {"nodes": "x"}}},
            {"data": {"search": {"issueCount": 1, "pageInfo": {"hasNextPage": True}, "nodes": []}}},
        ],
    )
    def test_malformed_payload_fails_closed(self, body):
        class Broken(FakeClient):
            def graphql(self, query, variables):
                return body

        with pytest.raises(cr.RecognitionError):
            cr.collect(Broken([]), REPO, NOW, registry(), {})

    def test_on_slice_runs_after_every_window_so_the_cache_is_saved_as_it_grows(self):
        saved: list[int] = []
        cache: dict = {}
        nodes = [pr(1, "ann", "2026-09-20T00:00:00Z")]
        cr.collect(FakeClient(nodes), REPO, NOW, registry(), cache, on_slice=lambda: saved.append(len(cache)))
        assert len(saved) == cr.COHORT_DAYS // cr.SLICE_DAYS


class TestAdditions:
    def test_generated_paths_are_excluded_from_the_gate_count(self):
        files = [f("src/a.py", 300), f("uv.lock", 5000), f("docs/i18n/README.ru.md", 40), f("web/dist/app.js", 900)]
        state = cr.collect(FakeClient([pr(1, "ann", "2026-09-20T00:00:00Z", files=files)]), REPO, NOW, registry(), {})
        ann = state["contributors"][0]
        assert ann["added_30d"] == 300 and ann["added_30d_raw"] == 6240

    def test_files_beyond_the_first_hundred_count_as_non_generated(self):
        files = [f("uv.lock", 50)] + [f(f"src/m{i}.py", 1) for i in range(99)]
        node = pr(1, "ann", "2026-09-20T00:00:00Z", files=files, changed_files=140, additions=500)
        state = cr.collect(FakeClient([node]), REPO, NOW, registry(), {})
        ann = state["contributors"][0]
        assert ann["added_30d_raw"] == 500
        assert ann["added_30d"] == 99 + (500 - 149)  # listed non-generated + unlisted remainder

    def test_a_pr_without_its_file_list_fails_closed(self):
        node = {**pr(1, "ann", "2026-09-20T00:00:00Z"), "files": None}
        with pytest.raises(cr.RecognitionError, match="file list"):
            cr.collect(FakeClient([node]), REPO, NOW, registry(), {})

    def test_cache_hit_wins_over_the_search_payload(self):
        cache = {"1": {"merged_at": "2026-09-20T00:00:00Z", "counted": 42, "raw": 42, "files": 1}}
        node = pr(1, "ann", "2026-09-20T00:00:00Z", files=[f("src/a.py", 9999)])
        state = cr.collect(FakeClient([node]), REPO, NOW, registry(), cache)
        assert state["contributors"][0]["added_30d"] == 42

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
