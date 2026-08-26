"""Choosing which of the assistant's sentences to keep.

Assistant prose is 5.3% of a conversation's bytes -- small, but the only part
where there is genuinely too much to keep whole. So it is the one place
Compact selects rather than quotes everything, and selection here means
**choosing existing sentences, never writing new ones.** Nothing in this module
can produce a word that was not already in the conversation.

The method is TF-IDF over sentences: a sentence scores for using terms that are
distinctive to this conversation rather than common throughout it, with a small
boost for the things a reader is actually looking for -- a path, an identifier,
a number, a word like "because" or "instead".

**TextRank is deliberately not used.** It builds a similarity matrix over every
pair of sentences, so it is quadratic; a large conversation here is thousands
of sentences, and that is seconds of pure Python for a marginal gain. TF-IDF is
arithmetic over a bag of words and needs no model, no corpus and no dependency.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence

__all__ = ["sentences", "top"]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]+")

_INTERESTING = re.compile(
    r"[\\/][\w.\-]+\.\w{1,6}"  # a path
    r"|`[^`]+`"  # something quoted as code
    r"|\b\d+\b"  # a number
    r"|\b(?:because|instead|chose|decided|refused|turned out|the reason|"
    r"which is why|so that|rather than)\b",
    re.IGNORECASE,
)
"""What makes a sentence worth more than its word statistics say.

A ranker scoring term frequency alone prefers whichever sentence repeats the
conversation's favourite nouns. These are the shapes that carry the things a
person reads a handoff *for*: what was touched, what it was called, how many,
and why.
"""

_BOOST = 0.35
_MIN_WORDS = 4
_MAX_WORDS = 60


def sentences(text: str) -> list[str]:
    """Prose split into sentences, with the shapeless ones dropped.

    A fragment of four words is a heading or a list marker, and a run of sixty
    is a pasted table -- neither reads as a sentence in a document, and both
    crowd out ones that do.
    """
    found: list[str] = []
    for part in _SENTENCE_SPLIT.split(text):
        line = " ".join(part.split())
        if not line or line.startswith(("#", "|", "```")):
            continue
        if _MIN_WORDS <= len(line.split()) <= _MAX_WORDS:
            found.append(line)
    return found


def top(pool: Sequence[str], *, limit: int) -> tuple[str, ...]:
    """The ``limit`` most distinctive sentences in ``pool``, in original order.

    Args:
        pool: Assistant messages, whole.
        limit: How many sentences to keep. Zero or fewer keeps none.

    Returns:
        Sentences, verbatim, in the order they were said. Order is restored
        after ranking because a handoff is read start to finish -- a list of
        sentences shuffled into score order reads as a shredded document.
    """
    if limit <= 0:
        return ()

    candidates: list[str] = []
    for message in pool:
        candidates.extend(sentences(message))
    if not candidates:
        return ()
    if len(candidates) <= limit:
        return tuple(candidates)

    tokenised = [[word.lower() for word in _WORD.findall(line)] for line in candidates]

    document_frequency: dict[str, int] = {}
    for words in tokenised:
        for word in set(words):
            document_frequency[word] = document_frequency.get(word, 0) + 1

    total = len(candidates)
    scored: list[tuple[float, int]] = []
    for index, words in enumerate(tokenised):
        if not words:
            scored.append((0.0, index))
            continue
        counts: dict[str, int] = {}
        for word in words:
            counts[word] = counts.get(word, 0) + 1
        score = 0.0
        for word, n in counts.items():
            idf = math.log(total / document_frequency[word])
            score += (n / len(words)) * idf
        # Longer sentences accumulate more terms; dividing by the square root
        # of the length leaves them an advantage without letting the longest
        # sentence win by being long.
        score /= math.sqrt(len(words))
        if _INTERESTING.search(candidates[index]):
            score += _BOOST * score + _BOOST / total
        scored.append((score, index))

    # Ties break on position, so the same conversation always yields the same
    # sentences. Without it two equally scored sentences could swap between
    # runs and the determinism test would fail intermittently.
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    chosen = sorted(index for _, index in scored[:limit])
    return tuple(candidates[index] for index in chosen)
