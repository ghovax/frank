"""The harness's own per-turn metadata on the A2A wire, and the envelope kinds it sends itself."""

from __future__ import annotations

from a2a.types import DataPart, Part

# See https://a2a-protocol.org/latest/topics/extensions — "extensions should place custom attributes in the metadata map … using this URI-namespaced convention".
METADATA_KEY = "urn:frank:ext:turn:v1"

# DataPart discriminator: every structured part declares its kind in `data.kind`.
PART_KIND = "kind"

# Opens an on-demand compaction turn.
COMPACTION_KIND = "compaction_request"

# Opens an autonomous wake turn.
AUTONOMOUS_RESUME_KIND = "autonomous_resume"

# Answers an input-required pause, carrying the request id and the decision or answers.
INPUT_RESPONSE_KIND = "input_response"

# Opens a turn for a goal that is still unfinished.
GOAL_CONTINUATION_KIND = "goal_continuation"

# Opens a turn that exists only to remind a session it has not reported to the session that created it.
REPORT_REMINDER_KIND = "report_reminder"


class Metadata:
    """Field names inside the turn-metadata object stored under :data:`METADATA_KEY`."""

    WORKING_DIRECTORY = "workingDirectory"
    WORKSPACE_STRATEGY = "worktreeStrategy"
    RUNTIME_WORKING_DIRECTORY = "runtimeWorkingDirectory"
    PROJECT_DIRECTORY = "projectDirectory"
    WORKSPACE_ID = "workspaceId"
    PERMISSION_MODE = "permissionMode"
    # Marks a harness-initiated turn (not user input): an autonomous background wake, or an on-demand compaction pass.
    AUTONOMOUS_RESUME = "autonomousResume"
    COMPACTION = "compaction"
    REPORT_REMINDER = "reportReminder"
    GOAL_CONTINUATION = "goalContinuation"
    # Set by a session sending another session a message.
    PEER_SENDER = "peerSender"
    # When the harness took this message, as an ISO-8601 instant in UTC.
    RECEIVED_AT = "receivedAt"


def turn_metadata(message) -> dict:
    """The turn-metadata object an incoming message carries, or ``{}`` when absent."""
    raw = (getattr(message, "metadata", None) or {}).get(METADATA_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def turn_metadata_envelope(fields: dict) -> dict:
    """Wrap turn fields under the namespaced key for an outgoing message, dropping keys whose value is ``None`` so the object only carries what was actually set."""
    return {METADATA_KEY: {key: value for key, value in fields.items() if value is not None}}


def part_payload(data: dict | None) -> dict:
    """The harness's payload inside a ``DataPart``, or ``{}`` when the part is not ours."""
    payload = (data or {}).get(METADATA_KEY)
    return payload if isinstance(payload, dict) else {}


def wrap_part_payload(payload: dict) -> dict:
    """The ``DataPart.data`` dict carrying ``payload`` as the harness's."""
    return {METADATA_KEY: payload}


def envelope_part(kind: str, **fields) -> Part:
    """An internal marker part for the envelopes the harness sends itself (compaction, autonomous resume, input response, report reminder, goal continuation)."""
    return Part(root=DataPart(data=wrap_part_payload({PART_KIND: kind, **fields})))
