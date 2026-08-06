"""The one model failure the harness causes, and can therefore recognise without being told.

A turn that overruns the context window is not like the other provider failures. A rate limit, a
dead connection, a rejected key — those are things that happen *to* the request, and the only
source of truth is the provider. An overlong request is something the harness *built*, out of
messages it holds, against a window it already knows. It can be measured here.

That distinction is the whole design, and the first attempt got it backwards. It classified the
failure by looking for phrases like ``"exceeds the context window"`` in whatever prose the provider
returned — which is guesswork dressed as detection: it breaks when a provider rewords its message,
breaks outright in any other language, and quietly stops working without anything failing. The
harness was reading tea leaves about a fact it could have computed.

So the order is: **measure before sending** (:func:`over_context_window`, provider-independent and
exact about what it knows), and if a request still comes back refused, recognise that only from
machine-readable signals — ``litellm.exceptions.ContextWindowExceededError``, which already carries
each provider's own quirks, or the ``code`` field a provider puts beside its message. No prose is
matched anywhere.
"""
from __future__ import annotations

from typing import Optional


class ContextWindowExceeded(Exception):
    """The request was larger than the model would accept.

    Carries what the interface needs to be specific: which model, how large its window is, and how
    many tokens were sent. ``tokens`` is set when the harness measured the request itself and left
    ``None`` when a provider refused something we had judged to fit — the difference between "this
    is 310,000 tokens and the window is 272,000" and "the provider says this did not fit", both of
    which are honest and only one of which can quote a number."""

    def __init__(self, message: str, *, model: str = "", context_window: int = 0,
                 tokens: Optional[int] = None) -> None:
        super().__init__(message)
        self.model = model
        self.context_window = context_window
        self.tokens = tokens


# What a provider calls this failure in the machine-readable ``code`` beside its message.
CONTEXT_OVERFLOW_CODES = frozenset({
    "context_length_exceeded",
    "context_length_error",
    "input_too_large",
    "string_above_max_length",
    "request_too_large",
})


def over_context_window(tokens: int, context_window: int) -> bool:
    """Whether a request of ``tokens`` cannot fit a ``context_window``.

    Deliberately a strict comparison with no safety margin subtracted, because the count is a
    proxy: it is taken with one general tokenizer standing in for every model's own, so it is
    approximate in both directions. Reserving headroom on top of an approximation would refuse
    requests that would have worked, and the cost of the two mistakes is not symmetric — failing
    to catch an overlong request means the provider catches it a moment later and the failure is
    still reported properly, while a false positive means the harness refuses to send something
    the model would have accepted, and no amount of retrying gets past it.

    A window of zero means "not known yet" and never means "no room": a cold catalogue must not
    make every request look overlong."""
    return context_window > 0 and tokens > context_window
