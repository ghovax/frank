"""Why a request did or did not hit the provider's prompt cache.

A cache hit is reported as one number — how many tokens of the prompt the provider already
had — and that number cannot be argued with, only explained. Explaining it needs the thing
nobody keeps: what the *previous* request looked like. A provider caches the longest prefix it
recognises, so a hit of 5,632 tokens against a 51,000-token request is not "the cache is bad",
it is "the request stopped matching 5,632 tokens in", and the only useful question is what sits
at that offset.

So each call is cut into the segments the wire is built from, each with its own digest and token
count, and compared against the call before it. That yields two facts the raw figure cannot give:
how much of the prefix was *reachable* — unchanged, and therefore cacheable — and, when it was
not, exactly which segment moved.

A segment is described by fields, never by a formatted label: its kind, its position, and the
role it carries. Anything that wants a sentence can compose one; nothing that wants to count how
often the tool schemas move, or which role tends to be rewritten, can parse one back out.

All of it rides on the usage event, which is already recorded per model call and replayed with
the transcript. That is deliberate: the question this answers ("what happened on call four of
that session last week") is asked long afterwards and only stored data can answer it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Sequence

from frank.base.tuning import count_tokens

#: The kinds of thing a request is made of, in the order every provider reads them.
INSTRUCTIONS = "instructions"
TOOLS = "tools"
ITEM = "item"


@dataclass(frozen=True)
class Piece:
    """One addressable part of a request, before it is measured.

    ``position`` is the index within the conversation for an item and ``-1`` for the parts
    there is only ever one of. ``role`` is whatever the wire calls that item — a message role,
    or a Responses item type — and is empty where the part has none.
    """

    kind: str
    text: str
    position: int = -1
    role: str = ""


@dataclass(frozen=True)
class Segment:
    """A measured piece: what it is, and what it hashed and counted to."""

    kind: str
    position: int
    role: str
    digest: str
    tokens: int

    def identity(self) -> dict[str, object]:
        """The part a consumer identifies it by, without the measurement."""
        return {"kind": self.kind, "position": self.position, "role": self.role}


@dataclass
class RequestTrace:
    """One request's segments, in wire order."""

    segments: list[Segment] = field(default_factory=list)

    @property
    def tokens(self) -> int:
        return sum(segment.tokens for segment in self.segments)


def trace(pieces: Sequence[Piece]) -> RequestTrace:
    """Measure a request's pieces into segments."""
    return RequestTrace([
        Segment(
            kind=piece.kind, position=piece.position, role=piece.role,
            digest=hashlib.blake2b(piece.text.encode(), digest_size=8).hexdigest(),
            tokens=count_tokens(piece.text),
        )
        for piece in pieces
    ])


def diagnose(current: RequestTrace, previous: Optional[RequestTrace]) -> dict[str, object]:
    """What this request kept from the last one, as fields to record beside the cache figure.

    ``reachable_tokens`` is the ceiling: the tokens of prefix that did not change and could
    therefore have been served from cache. Counted with this harness's tokenizer rather than
    the provider's, so it is an estimate and named as one.

    ``prefix_intact`` answers the question that decides who is at fault. True with a cache read
    of zero means the bytes were identical and the provider missed anyway — routing, not
    something a different request would fix. False carries a ``divergence`` naming the segment
    that moved, on both sides, and whether it stayed in place and was rewritten.
    """
    if previous is None:
        return {"prefix_intact": False, "reachable_tokens": 0, "segments": len(current.segments),
                "shared_segments": 0, "divergence": None}
    shared = 0
    for mine, theirs in zip(previous.segments, current.segments):
        if mine.digest != theirs.digest:
            break
        shared += 1
    common = {
        "reachable_tokens": sum(segment.tokens for segment in current.segments[:shared]),
        "segments": len(current.segments),
        "shared_segments": shared,
    }
    if shared == len(previous.segments):
        return {"prefix_intact": True, "divergence": None, **common}
    here = current.segments[shared] if shared < len(current.segments) else None
    there = previous.segments[shared]
    return {
        "prefix_intact": False,
        "divergence": {
            "index": shared,
            "current": here.identity() if here else None,
            "previous": there.identity(),
            # Same place, same identity, different digest: the piece did not move, its contents changed.
            "rewritten": bool(here and here.identity() == there.identity()),
        },
        **common,
    }


__all__ = ["INSTRUCTIONS", "ITEM", "TOOLS", "Piece", "RequestTrace", "Segment", "diagnose", "trace"]
