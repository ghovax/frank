"""Append-only research blackboard subsystem.

The research package is intentionally separate from the chat/task history. Chat
sessions are conversational context; research workspaces are durable blackboards
that several agents can read from and append to without overwriting each other.
"""

from harness.research.service import (
    research_board,
    research_evidence,
    research_open,
)

__all__ = ["research_board", "research_evidence", "research_open"]
