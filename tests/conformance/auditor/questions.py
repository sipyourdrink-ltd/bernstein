"""The 21 questions asked about a run after the fact.

The registry is the score's denominator. It is deliberately independent
of how many vectors exist: a suite that implements one question out of
twenty-one reports ``1/21``, not ``1/1``. Adding a vector never moves
the denominator, so the score cannot flatter itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

#: Question groups, in the order the instrument presents them.
ATTRIBUTION: Final = "attribution"
AUTHORITY: Final = "authority"
POLICY: Final = "policy"
DATA: Final = "data"
INTEGRITY: Final = "integrity"


@dataclass(frozen=True, slots=True)
class Question:
    """One question an auditor asks of a finished run.

    Attributes:
        number: Stable 1-based question number. Vectors are named for it.
        group: Which family of evidence the question interrogates.
        text: The question, as asked.
    """

    number: int
    group: str
    text: str


QUESTIONS: Final[tuple[Question, ...]] = (
    Question(1, ATTRIBUTION, "which principal initiated the run"),
    Question(2, ATTRIBUTION, "which agent performed each action"),
    Question(3, AUTHORITY, "was the sub-agent authorized, and by whom"),
    Question(4, AUTHORITY, "what exactly was it permitted to do"),
    Question(5, AUTHORITY, "did it stay inside that"),
    Question(6, POLICY, "which policy, at which version, allowed the tool call"),
    Question(7, ATTRIBUTION, "which identity was presented to the tool"),
    Question(8, DATA, "was the file marked sensitive, and does the record say so"),
    Question(9, DATA, "which model and endpoint received content"),
    Question(10, DATA, "was that endpoint permitted"),
    Question(11, POLICY, "was human approval required"),
    Question(12, POLICY, "who approved it and when"),
    Question(13, POLICY, "which policy version was in force at decision time"),
    Question(14, ATTRIBUTION, "which code, config and tool set actually ran"),
    Question(15, INTEGRITY, "does the evidence state what it does not cover"),
    Question(16, INTEGRITY, "can each decision be recomputed from its inputs"),
    Question(17, INTEGRITY, "can it be verified with no network and no install"),
    Question(18, INTEGRITY, "can a re-signed bundle be told from a genuine one"),
    Question(19, INTEGRITY, "can the run be replayed, and does divergence show"),
    Question(20, INTEGRITY, "can it be shown the record was not edited afterwards"),
    Question(21, AUTHORITY, "who else holds authority from the same grant"),
)

#: The score's denominator.
TOTAL_QUESTIONS: Final[int] = len(QUESTIONS)

_BY_NUMBER: Final[dict[int, Question]] = {q.number: q for q in QUESTIONS}


def question(number: int) -> Question:
    """Return the question numbered *number*.

    Args:
        number: 1-based question number.

    Returns:
        The registered :class:`Question`.

    Raises:
        KeyError: No question carries that number.
    """
    return _BY_NUMBER[number]


__all__ = [
    "ATTRIBUTION",
    "AUTHORITY",
    "DATA",
    "INTEGRITY",
    "POLICY",
    "QUESTIONS",
    "TOTAL_QUESTIONS",
    "Question",
    "question",
]
