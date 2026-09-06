"""Write a UCS conversation back out as a Codex rollout.

Unlike the Claude Code adapter, this one always **rebuilds** — there is no
verbatim replay path, and that is a deliberate choice rather than a missing
feature. A single rollout reached 53 MB on the probe machine and six
conversations totalled 121 MB, of which roughly a third is the interface's own
echo of turns the canonical records already hold. Copying all of it into every
bundle to preserve token counters and window bookkeeping is a poor trade.

What *is* copied verbatim is the header. Codex validates ``session_meta``
strictly: ``base_instructions`` and ``context_window` are objects rather than
scalars, and a wrong shape makes it reject the whole file with "does not start
with session metadata" — the conversation vanishes, no error, no partial read.
Carrying the real header costs about 40 KB per conversation and removes any
need to guess that schema. Everything below it is rebuilt from UCS.

Verified end to end: a rollout written this way was accepted by Codex's own
migration scanner ("1 eligible, 0 failed") and resumed by
``codex exec resume <id>``, which printed the session id and replayed the turn.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID, uuid5

from ferry import __version__
from ferry.ucs import Attachment, Conversation, Message

__all__ = [
    "REBUILD_NOTES",
    "SYNTHETIC_HEADER_FIELDS",
    "Rebuild",
    "rollout_lines",
    "synthesize_session_meta",
    "rollout_stamp",
    "session_meta_for",
]

_TURN_NAMESPACE: Final = UUID("b7f3c1a4-2e5d-4f6a-9b8c-1d2e3f4a5b6c")
"""Namespace for turn ids, so re-importing a conversation produces the same
turns rather than a fresh set each time."""

REBUILD_NOTES = (
    "records rebuilt from UCS: token counts, window state, turn context and "
    "compaction history are not carried",
    "encrypted reasoning is not restored: it holds no readable text and cannot "
    "be reconstructed from the thinking that was kept",
)
"""What a rebuilt rollout does not contain. Reported, never absorbed."""


@dataclass
class Rebuild:
    """Where the conversation is being written to."""

    cwd: str
    thread_id: UUID
    images: dict[UUID, tuple[Attachment, bytes]] = field(default_factory=dict)


def rollout_stamp(moment: datetime) -> str:
    """``YYYY-MM-DDTHH-MM-SS`` — ISO with the colons removed, as the filename needs."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H-%M-%S")


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SYNTHETIC_HEADER_FIELDS: Final = (
    "id",
    "session_id",
    "timestamp",
    "cwd",
    "cli_version",
    "originator",
    "source",
    "model_provider",
)
"""The header Codex accepts for a conversation that never had one.

**Measured 2026-09-06 against Codex 0.147.** Four fields (identity, time and
cwd) were refused outright with *No saved session found*. These eight loaded.
Fifteen -- these plus `context_window`, `history_mode`, `thread_source`,
`dynamic_tools` and nulls for `base_instructions` and `git` -- were refused
again, so **more is not safer here**: every field beyond the set that works is
another thing to be wrong about.

`model_provider` is in the list because Codex asked for it by name, failing with
*Model provider `` not found*. Worth recording on its own: the refusal this
adapter carried said a wrong header is discarded **silently**, which was true at
0.98 and is not true here. Rejection at this version is loud and names the
field.
"""


def synthesize_session_meta(conversation: Conversation, rebuild: Rebuild) -> dict[str, Any]:
    """A header for a conversation that arrived from another tool.

    Every value is derived from the conversation or the machine. Nothing is
    copied from someone else's session: a header borrowed wholesale would carry
    another conversation's git state and instructions, and describe work that
    never happened here.
    """
    return {
        "id": str(rebuild.thread_id),
        "session_id": str(rebuild.thread_id),
        "timestamp": _iso(conversation.created_at),
        "cwd": rebuild.cwd,
        # The version doing the writing, not a version invented to look native.
        "cli_version": f"ferry-{__version__}",
        "originator": "codex_cli_rs",
        "source": "cli",
        "model_provider": "openai",
    }


def session_meta_for(conversation: Conversation, rebuild: Rebuild) -> dict[str, Any] | None:
    """The header record: the conversation's own where there is one, else built.

    A Codex conversation carries its header in ``source_raw`` and it is replayed
    with only the machine-specific fields re-pointed -- nothing is re-derived, so
    nothing is lost in re-deriving it.

    A conversation from **another tool** has no header, and until 2026-09-06
    that meant the import was refused: an invented header was believed to be
    discarded silently, and a silent loss is worse than a refusal. Measured
    again at Codex 0.147, both halves of that turned out to be wrong -- a header
    of eight derived fields is accepted, and a bad one is refused loudly by
    name. See :data:`SYNTHETIC_HEADER_FIELDS`.
    """
    raw = conversation.source_raw or {}
    header = raw.get("session_meta")
    if not isinstance(header, dict):
        return synthesize_session_meta(conversation, rebuild)
    payload = dict(header)
    previous = payload.get("id")
    payload["id"] = str(rebuild.thread_id)
    # `session_id` is NOT a second copy of `id`. On a subagent thread it holds
    # the *parent* thread's id -- the one case in the probed data where the two
    # differ, and overwriting it reparented the subagent to itself. It is only
    # updated when it was genuinely the same identity being renamed.
    if payload.get("session_id") == previous:
        payload["session_id"] = str(rebuild.thread_id)
    payload["cwd"] = rebuild.cwd
    # The header's own timestamp is left alone. It records when the session
    # began, which importing does not change; rewriting it made source_raw
    # differ across a round trip that had moved nothing.
    return payload


def _content_blocks(message: Message, rebuild: Rebuild) -> list[dict[str, Any]]:
    """UCS blocks as Codex message content.

    The text block type depends on direction: what the user sent is
    ``input_text``, what the model produced is ``output_text``. Emitting the
    wrong one produces a file that parses and renders incorrectly.
    """
    text_type = "output_text" if message.role == "assistant" else "input_text"
    blocks: list[dict[str, Any]] = []
    for block in message.content:
        if block.type == "text":
            blocks.append({"type": text_type, "text": block.text})
        elif block.type == "image":
            found = rebuild.images.get(block.attachment_id)
            if found is None:
                continue
            attachment, data = found
            encoded = base64.b64encode(data).decode("ascii")
            blocks.append(
                {
                    "type": "input_image",
                    "detail": "high",
                    "image_url": f"data:{attachment.mime_type};base64,{encoded}",
                }
            )
    return blocks


def _ui_event(message: Message, stamp: str | None) -> dict[str, Any] | None:
    """The record the Codex interface draws a turn from.

    **Measured 2026-09-06 against Codex 0.147, after a session rebuilt without
    these opened completely empty.** A rollout holds two parallel accounts of
    the same conversation: ``response_item`` is what is sent to the model, and
    ``event_msg`` is what the screen shows. Ferry wrote only the first, so every
    word was present in a file whose transcript rendered as nothing -- the exact
    false success this milestone exists to prevent, and invisible to any check
    that reads the file back rather than opening it.

    ``{type, message}`` and nothing else, because that is the shape that
    rendered. A variant carrying the optional keys real records also have --
    ``images``, ``text_elements``, ``phase``, ``memory_citation`` -- as nulls
    rendered **one** line of two, so one of those values is worse than its
    absence. The smallest proven shape is the one written.
    """
    text = " ".join(
        block.text for block in message.content if block.type == "text" and block.text.strip()
    )
    if not text:
        return None
    if message.role == "user":
        payload: dict[str, Any] = {"type": "user_message", "message": text}
    else:
        # `phase` decides whether the desktop app shows the message at all.
        #
        # Measured across this machine's own sessions: 1,387 agent messages
        # carry `{memory_citation, message, phase, type}` with a phase of
        # `commentary` or `final_answer`, and 50 older ones carry `{message,
        # type}` alone. The CLI renders both shapes, so a rebuilt conversation
        # read correctly there while the desktop app showed **only the user's
        # side** -- every reply present in the file and absent from the screen.
        #
        # `final_answer` because that is what a completed reply is. Ferry is not
        # replaying a turn in progress: the conversation being migrated is over.
        # `memory_citation` is null in all 1,437 real events, so null is what
        # Codex itself writes rather than a value invented to fill the key.
        payload = {
            "type": "agent_message",
            "message": text,
            "phase": "final_answer",
            "memory_citation": None,
        }
    record: dict[str, Any] = {"type": "event_msg", "payload": payload}
    if stamp:
        record["timestamp"] = stamp
    return record


def _records_for(
    message: Message,
    rebuild: Rebuild,
    ordinal: int,
    *,
    shown: bool = True,
    turn_id: str = "",
    when: datetime | None = None,
) -> list[dict[str, Any]]:
    """One UCS message as the Codex records it came from."""
    # **Every record carries a timestamp, always.** A UCS message need not have
    # one -- a Copilot conversation carries none at all -- and this used to omit
    # the field when it was missing, which produced a rollout Codex would not
    # render: the assistant's side was absent in both the CLI and the desktop
    # app while every word sat in the file.
    #
    # It took five rounds of testing to find, because a conversation *with*
    # timestamps rendered perfectly and every spike had them. The difference
    # between the working spike and the failing conversation was eight records
    # missing one key.
    #
    # `when` is the turn's own time, and the conversation's before that. Not a
    # time invented to fill the field: the best thing actually known about when
    # this message happened.
    moment = message.timestamp or when
    stamp = _iso(moment) if moment else None
    out: list[dict[str, Any]] = []

    def emit(record_type: str, payload: dict[str, Any]) -> None:
        record: dict[str, Any] = {"type": record_type, "payload": payload}
        if stamp:
            record["timestamp"] = stamp
        out.append(record)

    content = _content_blocks(message, rebuild)
    if content:
        emit(
            "response_item",
            {
                "type": "message",
                "id": f"msg_ferry_{ordinal}",
                "role": message.role if message.role in {"user", "assistant"} else "user",
                "content": content,
                # A reply is bound to its turn, and marked as a finished one.
                # Without both, the desktop app has a message belonging to
                # nothing it can draw, and shows the user talking to nobody.
                **(
                    {
                        "phase": "final_answer",
                        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                    }
                    if message.role == "assistant" and turn_id
                    else {}
                ),
            },
        )
        # And the same turn again, as the interface reads it. Both accounts or
        # neither: `response_item` alone is a conversation the model can see and
        # the person cannot.
        #
        # `shown=False` for one case only: the first user turn of a conversation
        # that carries its own typed marker. Codex writes `user_message` for
        # what the person typed and not for the screens of context it injects as
        # user turns, and `rollout_lines` replays that marker. Emitting a second
        # one here would title the conversation after the injected text -- the
        # bug the marker exists to prevent.
        event = _ui_event(message, stamp) if shown else None
        if event is not None:
            out.append(event)

    for block in message.content:
        if block.type == "thinking":
            # Back to event_msg, which is where it was read from. response_item
            # reasoning is encrypted and cannot be reconstructed.
            emit("event_msg", {"type": "agent_reasoning", "text": block.text})
        elif block.type == "tool_use":
            emit(
                "response_item",
                {
                    "type": "function_call",
                    "id": f"fc_ferry_{ordinal}",
                    "call_id": block.id or f"call_ferry_{ordinal}",
                    "name": block.name,
                    "arguments": json.dumps(block.input, ensure_ascii=False),
                },
            )
        elif block.type == "tool_result":
            emit(
                "response_item",
                {
                    "type": "function_call_output",
                    "id": f"fco_ferry_{ordinal}",
                    "call_id": block.tool_use_id or f"call_ferry_{ordinal}",
                    "output": block.output,
                },
            )
    return out


def _turns(conversation: Conversation) -> list[tuple[int, int]]:
    """The conversation as exchanges: each user turn and the replies to it.

    A turn opens at a user message and runs to just before the next one. A
    conversation that opens with a reply -- odd, but it is still someone's
    conversation -- gets a turn from the start rather than losing its first
    messages to a turn that never opened.
    """
    starts = [i for i, message in enumerate(conversation.messages) if message.role == "user"]
    if not starts or starts[0] != 0:
        starts.insert(0, 0)
    ends = [*starts[1:], len(conversation.messages)]
    return list(zip(starts, ends, strict=True))


def _last_reply(conversation: Conversation, start: int, end: int) -> str:
    """The reply a turn ended on, which `task_complete` repeats."""
    for message in reversed(conversation.messages[start:end]):
        if message.role == "assistant":
            for block in message.content:
                if block.type == "text" and block.text.strip():
                    return block.text
    return ""


def rollout_lines(conversation: Conversation, rebuild: Rebuild) -> bytes | None:
    """The whole rollout as JSONL, or ``None`` if no header was available."""
    header = session_meta_for(conversation, rebuild)
    if header is None:
        return None

    records: list[dict[str, Any]] = [
        {
            "type": "session_meta",
            "timestamp": _iso(conversation.created_at),
            "payload": header,
        }
    ]

    # Codex emits this event for a turn the person typed, as against the screens
    # of context it injects as user messages. The distinction has no UCS field,
    # so it is carried in source_raw and replayed here -- without it the title of
    # a rebuilt conversation becomes "<permissions instructions>".
    #
    # It goes *after* the message it echoes, which is where Codex puts it. Placed
    # before any message, a reader has nothing to attribute it to and treats it
    # as a turn of its own, duplicating the first user message.
    typed = (conversation.source_raw or {}).get("first_typed")
    marker_pending = isinstance(typed, str) and bool(typed)

    # Whether Ferry may say "the person typed this" about a user turn at all.
    #
    # Codex writes `user_message` for what someone typed and not for the screens
    # of context it injects as user turns, so the event is a *claim*, not a
    # rendering detail. A rollout that carried no such event made no such claim,
    # and Ferry adding one would assert something about the original it cannot
    # know -- and would title the conversation after injected text.
    #
    # A conversation from another tool is the opposite case: there is no
    # original claim to contradict, and its user messages are the person's.
    own_header = isinstance((conversation.source_raw or {}).get("session_meta"), dict)
    may_show_user = marker_pending or not own_header

    for start_at, end_at in _turns(conversation):
        # **The turn is what makes a reply visible.** Measured 2026-09-06: a
        # rebuilt conversation read correctly in the Codex CLI and showed only
        # the user's side in the desktop app, every reply present in the file.
        # Real sessions wrap each exchange in `task_started` / `task_complete`
        # sharing a `turn_id`, and a reply belonging to no turn is one the
        # interface cannot place.
        #
        # Only what Ferry can know goes in. Real events also carry
        # `duration_ms` and `time_to_first_token_ms` -- how long a model took
        # to answer, on a day this machine was not there for. The experiment
        # covered a turn with those and a turn without, and both rendered, so
        # nothing is invented here.
        turn_id = str(uuid5(_TURN_NAMESPACE, f"{conversation.id}:{start_at}"))
        opened = conversation.messages[start_at].timestamp or conversation.created_at
        closed = conversation.messages[end_at - 1].timestamp or opened
        records.append(
            {
                "type": "event_msg",
                "timestamp": _iso(opened),
                "payload": {
                    "type": "task_started",
                    "turn_id": turn_id,
                    "started_at": int(opened.timestamp()),
                },
            }
        )

        for ordinal in range(start_at, end_at):
            message = conversation.messages[ordinal]
            records.extend(
                _records_for(
                    message,
                    rebuild,
                    ordinal,
                    turn_id=turn_id,
                    when=opened,
                    shown=(may_show_user and not (marker_pending and message.role == "user"))
                    if message.role == "user"
                    else True,
                )
            )
            if marker_pending and message.role == "user":
                records.append(
                    {
                        "type": "event_msg",
                        "timestamp": _iso(message.timestamp or conversation.created_at),
                        "payload": {
                            "type": "user_message",
                            "message": typed,
                            "images": [],
                            "local_images": [],
                            "text_elements": [],
                        },
                    }
                )
                marker_pending = False

        records.append(
            {
                "type": "event_msg",
                "timestamp": _iso(closed),
                "payload": {
                    "type": "task_complete",
                    "turn_id": turn_id,
                    # Codex's own duplication of the reply, not Ferry's.
                    "last_agent_message": _last_reply(conversation, start_at, end_at),
                    "started_at": int(opened.timestamp()),
                    "completed_at": int(closed.timestamp()),
                },
            }
        )

    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode(
        "utf-8"
    )
