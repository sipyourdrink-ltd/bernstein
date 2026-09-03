"""The 21 questions the auditor suite asks of one exported bundle.

The inventory lives here rather than in the test files so that the
scoreboard has a denominator that does not move when a vector slice
lands: a question is registered the moment it is asked, and a slice that
adds tests can only change how many are *answered*.

Each vector test declares which question it answers with
``@pytest.mark.auditor_question(N)``; the marker is validated against
this table by ``conftest.py``, so a typo cannot silently invent a
twenty-second question or double-book an existing one.
"""

from __future__ import annotations

from types import MappingProxyType

#: Question number -> the question, as an auditor would ask it.
QUESTIONS: MappingProxyType[int, str] = MappingProxyType(
    {
        1: "Which principal initiated the run?",
        2: "Which agent performed each recorded action?",
        3: "Was the sub-agent authorized to act, and by whom?",
        4: "What exactly was the sub-agent permitted to do?",
        5: "Did the sub-agent stay inside that permission?",
        6: "Which policy, at which version, allowed the tool call?",
        7: "Which identity was presented to the tool?",
        8: "Was the file that was read marked sensitive, and does the record say so?",
        9: "Which model and endpoint received content?",
        10: "Was that endpoint one the installation is permitted to use?",
        11: "Was human approval required for the final action?",
        12: "If required, who approved it and when?",
        13: "Which policy version was in force at decision time, not export time?",
        14: "Which agent code, config and tool set actually ran?",
        15: "Does the evidence say what it does not cover?",
        16: "Can each decision be recomputed from its recorded inputs?",
        17: "Can the bundle be verified with no network and no bernstein install?",
        18: "Can the verifier tell a genuine bundle from one re-signed with another key?",
        19: "Can the run be replayed, and does replay diverge detectably?",
        20: "Can the evidence show the record was not edited after the fact?",
        21: "Which other principals hold authority derived from the same grant?",
    },
)

#: The denominator every scoreboard prints against.
QUESTION_COUNT = len(QUESTIONS)


def question_text(number: int) -> str:
    """Return the question *number* asks.

    Args:
        number: A question number in ``1..21``.

    Returns:
        The question text.

    Raises:
        KeyError: *number* is not one of the registered questions.
    """
    try:
        return QUESTIONS[number]
    except KeyError:
        raise KeyError(
            f"question {number!r} is not one of the {QUESTION_COUNT} registered questions (valid: 1..{QUESTION_COUNT})",
        ) from None


__all__ = ["QUESTIONS", "QUESTION_COUNT", "question_text"]
