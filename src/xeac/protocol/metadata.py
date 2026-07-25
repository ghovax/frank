"""The harness's own per-turn metadata on the A2A wire, and the envelope kinds it sends itself.

A2A's convention is that an extension places its attributes under a single URI-namespaced
key in a message's ``metadata`` map rather than as bare top-level keys, so they can never
collide with another extension's. Everything XEAC adds to a turn lives under one such key.
"""

from __future__ import annotations

from a2a.types import DataPart, Part

# See https://a2a-protocol.org/latest/topics/extensions — "extensions should place custom
# attributes in the metadata map … using this URI-namespaced convention".
XEAC_METADATA_KEY = "urn:xeac:ext:turn:v1"

# DataPart discriminator: every structured part declares its kind in `data.kind`.
PART_KIND = "kind"

# A user turn whose input is an artifact interaction carries it as a DataPart of this kind
# rather than as prose, so the payload reaches the model intact.
ARTIFACT_EVENT_KIND = "artifact_event"

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


class Metadata:
    """Field names inside the turn-metadata object stored under :data:`XEAC_METADATA_KEY`.

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
    # Set by a session sending another session a message. Its presence is what makes the turn
    # a peer turn — the field carries who, and "who" and "not the user" are the same fact here.
    PEER_SENDER = "peerSender"


def turn_metadata(message) -> dict:
    """The turn-metadata object an incoming message carries, or ``{}`` when absent."""
    raw = (getattr(message, "metadata", None) or {}).get(XEAC_METADATA_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def turn_metadata_envelope(fields: dict) -> dict:
    """Wrap turn fields under the namespaced key for an outgoing message, dropping keys whose
    value is ``None`` so the object only carries what was actually set."""
    return {XEAC_METADATA_KEY: {key: value for key, value in fields.items() if value is not None}}


def envelope_part(kind: str, **fields) -> Part:
    """An internal marker part for the envelopes the harness sends itself (compaction,
    autonomous resume, input response). These are not client-facing wire events, so they stay
    a bare ``{kind, **fields}`` dict."""
    return Part(root=DataPart(data={PART_KIND: kind, **fields}))
