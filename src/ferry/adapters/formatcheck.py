"""Noticing when a tool's storage format has actually changed.

Ferry reads four undocumented formats by inspection, so it needs to say
something when one of them changes. The obvious signal is the version number,
and it is the wrong one.

**A version number changing is not a format changing.** These applications
update constantly -- VS Code monthly, Antigravity more often -- and almost
every release leaves storage exactly as it was. An adapter that warns whenever
the version differs from the one it was verified against starts warning a few
weeks after it ships and never stops. The user learns to scroll past it, and it
is still there, unread, on the day the format really does change. That is worse
than saying nothing, because it converts a real signal into furniture.

So Ferry checks the format instead. Before each scan it reads one real
conversation per tool and confirms the things the adapter depends on are still
there. If they are, nothing is said no matter what the version reads. If they
are not, it says **what** did not match -- which is information the version
number never carried.

The check is a sample, not a proof. It reads the most recently written
conversation, because a format change shows up in new data first: the old files
were written by the old version and will keep parsing perfectly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["FormatCheck"]


@dataclass(frozen=True)
class FormatCheck:
    """Whether a tool still stores conversations the way an adapter expects.

    ``findings`` are phrased as what was looked for and not found, in the
    user's terms rather than the format's -- "no assistant text was found in 40
    of 42 steps" rather than "field 20.1 absent".
    """

    checked: bool = False
    """Whether anything was actually examined.

    ``False`` when there was no conversation to read. That is not a pass and
    not a failure: it means the question could not be asked, and the caveat
    says so rather than implying the format was verified.
    """

    findings: list[str] = field(default_factory=list)
    """What did not match. Empty when the format is as expected."""

    @property
    def ok(self) -> bool:
        return self.checked and not self.findings

    def caveats(self, tool: str, version: str | None) -> list[str]:
        """What to tell the user, which is usually nothing.

        Silent on a format that still matches, **whatever version reports**.
        That silence is the point: it is what keeps the warning meaningful for
        the release that breaks something.
        """
        if self.ok:
            return []

        # The screen prints the tool's name in front of a caveat, so these
        # must not repeat it. ``tool`` is still taken, because a caveat quoted
        # anywhere else needs to say what it is about.
        named = f"your version ({version})" if version else "your version"
        if not self.checked:
            return [
                f"could not check whether {named} still stores conversations the way Ferry "
                "expects - there was nothing to read. Check an exported conversation looks "
                "right before relying on it."
            ]

        detail = "; ".join(self.findings)
        return [
            f"{named} appears to store conversations differently than {tool} did when this "
            f"was built: {detail}. An export may be incomplete - please report this."
        ]
