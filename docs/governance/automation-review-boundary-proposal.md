# PROPOSED: automation review boundary

This document is an unaccepted design proposal. It grants no merge or review authority and changes no current rule.


## Root cause

The review charter forbids approval by someone who authored, co-authored or materially pushed a PR. The checker’s automation branch instead tests maintainer approval without excluding known writers. A Draft returns before either question is decided. Checker acceptance therefore cannot authorize self-review.

## Boundary

Preserve NoSelf regardless of account, protected ownership, current-base roster/CODEOWNERS, approval thresholds, action pins, standing objections and the normal merge queue. Automatic enforcement can use only known provenance; this proposal introduces no cross-account identity resolver. Operators remain bound by the charter even where automation cannot prove a relationship.

## Undecided options

1. **Preserve the existing boundary:** reject known writer approvals consistently and retain unresolved PRs as held. For future sensitive bot PRs, keep the sole eligible protected-path reviewer out of material commits; the bot or an independent contributor makes fixes.
2. **Decide whether another independent process should exist:** any alternative needs an explicit, separately reviewed protected governance decision. No alternate authority, delegation, roster change, lower quorum or exemption is selected here. Neither option presently unlocks #6730.

## Seven future properties — not implemented or executed

1. `test_nonwriter_maintainer_review_counts_for_protected_bot_change`: eligible current-revision approval counts; missing or stale approval does not.
2. `test_known_writer_approval_is_rejected_for_automation_and_workflow_authors`: known material authors/coauthors/pushers cannot supply their own required review.
3. `test_ordinary_writer_review_rules_remain_unchanged`: preserve ordinary author/contributor exclusions and existing quorum requirements.
4. `test_base_only_merge_exception_requires_unchanged_diff_proof`: allow only the existing proven fingerprint exception; unavailable provenance fails closed.
5. `test_protected_rules_are_loaded_from_the_base`: retain reserved paths, roster/CODEOWNERS ownership and pins; head-controlled policy cannot grant eligibility.
6. `test_governance_window_objections_and_queue_remain_required`: preserve 72 hours, standing changes-requested blocks and queued-state checks.
7. `test_draft_neutral_verdict_does_not_establish_readiness`: a Draft’s early PASS cannot claim review completion or authorize arming.

## Evidence and scope

Current base `90da5687560d215d2f8da9d07c6808907827da09`: review charter §3 and checker lines928–1000. Bounded duplicate searches found no exact tracker; #5757 covers routing and #5738 covers reserved-path/audit documentation.

Existing #6730 stays unchanged, Draft and held. Its final3/3 validation evidence, original author, history, body and credits remain intact. The manual sweep cap is a scope constraint, not a new checker feature. No proxy vote, self-approval, expired adoption, replacement PR, fourth attempt or code amendment is proposed. A legitimate independent review path remains a maintainer design decision.

Tracking issue: #6761. This proposal does not amend the current charter.
