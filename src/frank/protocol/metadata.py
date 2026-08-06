"""The harness's own per-turn metadata on the A2A wire, under a single namespaced key."""

from __future__ import annotations

from a2a.types import DataPart, Part

# The URI-namespaced convention the extension specification asks an extension to place its attributes under.
METADATA_KEY = "urn:frank:ext:turn:v1"

# DataPart discriminator: every structured part declares its kind in `data.kind`.
PART_KIND = "kind"

# Opens an on-demand compaction turn, which runs no model turn and so is modelled like an autonomous wake.
COMPACTION_KIND = "compaction_request"

# Opens an autonomous wake turn, modelled as an agent message since A2A has no harness role.
AUTONOMOUS_RESUME_KIND = "autonomous_resume"

# Answers an input-required pause, carrying the request id and the decision or answers.
INPUT_RESPONSE_KIND = "input_response"

# Opens a turn for a goal that is still unfinished, kept distinct because it carries the goal itself.
GOAL_CONTINUATION_KIND = "goal_continuation"

# Opens a turn that exists only to remind a session it has not reported back to its creator.
REPORT_REMINDER_KIND = "report_reminder"


class Metadata:
    """Field names inside the turn-metadata object, only three of which a client sets."""

    WORKING_DIRECTORY = "workingDirectory"
    WORKSPACE_STRATEGY = "worktreeStrategy"
    RUNTIME_WORKING_DIRECTORY = "runtimeWorkingDirectory"
    PROJECT_DIRECTORY = "projectDirectory"
    WORKSPACE_ID = "workspaceId"
    PERMISSION_MODE = "permissionMode"
    # Marks a harness-initiated turn rather than user input.
    AUTONOMOUS_RESUME = "autonomousResume"
    COMPACTION = "compaction"
    REPORT_REMINDER = "reportReminder"
    GOAL_CONTINUATION = "goalContinuation"
    # Set by a session sending another session a message, whose presence is what makes the turn a peer turn.
    PEER_SENDER = "peerSender"
    # When the harness took this message, stamped by the receiving side because that is the clock it can vouch for.
    RECEIVED_AT = "receivedAt"


def turn_metadata(message) -> dict:
    """The turn-metadata object an incoming message carries, or ``{}`` when absent."""
    raw = (getattr(message, "metadata", None) or {}).get(METADATA_KEY)
    return dict(raw) if isinstance(raw, dict) else {}


def turn_metadata_envelope(fields: dict) -> dict:
    """Wrap turn fields under the namespaced key, dropping unset ones so the object carries only what was given."""
    return {METADATA_KEY: {key: value for key, value in fields.items() if value is not None}}


def part_payload(data: dict | None) -> dict:
    """The harness's payload inside a data part, or `{}` when the part is not ours."""
    payload = (data or {}).get(METADATA_KEY)
    return payload if isinstance(payload, dict) else {}


def wrap_part_payload(payload: dict) -> dict:
    """The ``DataPart.data`` dict carrying ``payload`` as the harness's."""
    return {METADATA_KEY: payload}


def envelope_part(kind: str, **fields) -> Part:
    """An internal marker part for the envelopes the harness sends itself."""
    return Part(root=DataPart(data=wrap_part_payload({PART_KIND: kind, **fields})))
