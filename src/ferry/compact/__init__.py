"""Compact: a conversation reduced to what is worth carrying forward.

One function, :func:`compact`, turning a UCS conversation into a markdown
document. **No LLM, no network, no API key, no cost**, and nothing leaves the
machine -- which is not a compromise but what the data turned out to justify.

Measured over the human's own four stores: ``tool_result`` is 79.3% of a
conversation's bytes and ``tool_use`` another 11.5%, while everything the user
actually typed is **1.7%**. Ninety-one percent of a conversation is machinery,
so the compaction target is met by deleting tool bodies before any language
processing happens at all -- and there is no compression pressure left on the
part that carries the meaning. That is why the user's words are kept whole and
never summarised: rewriting them could only lose detail, and would save nothing.

What is given up is stated plainly rather than glossed over. An LLM can write
*"we tried X, it failed because Y, so we switched to Z."* This can quote all
three facts and cannot write the sentence joining them. **Compact produces a
dossier, not a narrative.**

The pipeline, four stages, each its own module and each testable alone:

======================================  ====================================
:mod:`ferry.compact.prune`              tool bodies to facts -- the 91%
:mod:`ferry.compact.paste`              a person's words from what they pasted
:mod:`ferry.compact.extract`            the ledgers
:mod:`ferry.compact.rank`               which assistant sentences survive
:mod:`ferry.compact.render`             the document
======================================  ====================================

:mod:`ferry.compact.catalogue` sits underneath all of it, holding the one thing
that differs between tools: where each of them records what a call did.
"""

from __future__ import annotations

from ferry.compact.extract import Digest, digest
from ferry.compact.render import LENGTHS, SHAPES, quotations, render
from ferry.ucs import Conversation

__all__ = ["LENGTHS", "SHAPES", "Digest", "compact", "digest", "quotations", "render"]


def compact(
    conversation: Conversation,
    *,
    shape: str = "handoff",
    length: str = "standard",
    version: str = "",
) -> str:
    """One conversation as a markdown document.

    Args:
        conversation: Any UCS conversation, from any of the four tools.
        shape: ``handoff`` (everything), ``said`` (only the user's words), or
            ``done`` (only what the tools did).
        length: ``brief``, ``standard`` or ``full``.
        version: Ferry's version, for the header line.

    Returns:
        Markdown. The same conversation always produces the same bytes: this
        function reads no files, makes no network call and never asks the clock.
    """
    return render(digest(conversation), shape=shape, length=length, version=version)
