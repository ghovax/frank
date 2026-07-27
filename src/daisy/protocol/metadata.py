"""The harness's own per-turn metadata on the A2A wire, and the envelope kinds it sends itself.

A2A's convention is that an extension places its attributes under a single URI-namespaced
key in a message's ``metadata`` map rather than as bare top-level keys, so they can never
collide with another extension's. Everything Daisy adds to a turn lives under one such key.
"""

from __future__ import annotations

from a2a.types import DataPart, Part

# See https://a2a-protocol.org/latest/topics/extensions — "extensions should place custom
# attributes in the metadata map … using this URI-namespaced convention".
DAISY_METADATA_KEY = "urn:daisy:ext:turn:v1"

# DataPart discriminator: every structured part declares its kind in `data.kind`.
PART_KIND = "kind"

# Opens an on-demand compaction turn. It runs no model turn — it summarizes older history
# and emits the compaction parts — so it is modelled like an autonomous wake.
COMPACTION_KIND = "compaction_request"

# Opens an autonomous wake turn. A2A has no "system/harness" message role — only `user` and
# `agent` — so a harness-initiated turn is modelled honestly as an *agent* message (the
# session resumed itself) carrying this single, prose-less part. It renders as nothing, so
# the wake never fabricates a user message.
AUTONOMOUS_RESUME_KIND = "autonomous_resume"

# Answers an input-required pause, carrying the request id and the decision or answers.
INPUT_RESPONSE_KIND = "input_response"

# Opens a turn that exists only to remind a session it has not reported to the session that
# created it. Modelled like an autonomous wake — an agent-role message with a prose-less part
# — but a distinct kind, because a wake delivers a result that is waiting and this delivers
# nothing at all; the autonomous path would close it as a no-op for exactly that reason.
REPORT_REMINDER_KIND = "report_reminder"


class Metadata:
    """Field names inside the turn-metadata object stored under :data:`DAISY_METADATA_KEY`.

    A client sets only ``WORKING_DIRECTORY``/``PERMISSION_MODE``/``PROJECT_ID`` at session
    creation; the rest are set internally once the daemon has resolved the session's runtime
    checkout."""

    WORKING_DIRECTORY = "workingDirectory"
    WORKSPACE_STRATEGY = "workspaceStrategy"
    RUNTIME_WORKING_DIRECTORY = "runtimeWorkingDirectory"
    PROJECT_DIRECTORY = "projectDirectory"
    PROJECT_ID = "projectId"
    PERMISSION_MODE = "permissionMode"
    # Marks a harness-initiated turn (not user input): an autonomous background wake, or an
    # on-demand compaction pass.
    AUTONOMOUS_RESUME = "autonomousResume"
    COMPACTION = "compaction"
    REPORT_REMINDER = "reportReminder"
    # Set by a session sending another session a message. Its presence is what makes the turn
    # a peer turn — the field carries who, and "who" and "not the user" are the same fact here.
    PEER_SENDER = "peerSender"


def turn_metadata(message) -> dict:
    """The turn-metadata object an incoming message carries, or ``{}`` when absent."""
    raw = (getattr(message, "metadata", None) or {}).get(DAISY_METADATA_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def turn_metadata_envelope(fields: dict) -> dict:
    """Wrap turn fields under the namespaced key for an outgoing message, dropping keys whose
    value is ``None`` so the object only carries what was actually set."""
    return {DAISY_METADATA_KEY: {key: value for key, value in fields.items() if value is not None}}


def part_payload(data: dict | None) -> dict:
    """The harness's payload inside a ``DataPart``, or ``{}`` when the part is not ours.

    ``DataPart.data`` is an open dict on messages that reach a session's own A2A socket, so
    the values in it are not all minted here. One namespaced key keeps ours apart from a
    foreign implementation's — the same convention the turn metadata already uses, and the
    reason a bare ``data.kind`` is not enough: this module *dispatches* on that value, so a
    peer's unrelated ``input_response`` would be read as answering a permission gate."""
    payload = (data or {}).get(DAISY_METADATA_KEY)
    return payload if isinstance(payload, dict) else {}


def wrap_part_payload(payload: dict) -> dict:
    """The ``DataPart.data`` dict carrying ``payload`` as the harness's."""
    return {DAISY_METADATA_KEY: payload}


def envelope_part(kind: str, **fields) -> Part:
    """An internal marker part for the envelopes the harness sends itself (compaction,
    autonomous resume, input response, report reminder)."""
    return Part(root=DataPart(data=wrap_part_payload({PART_KIND: kind, **fields})))
