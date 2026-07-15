from dataclasses import dataclass
from typing import Literal


AgentMessageKind = Literal["question", "response", "failed", "withdrawn"]


@dataclass(frozen=True)
class AgentMessage:
    """One mailbox delivery between two active agents in the same A2A context."""

    identifier: str
    kind: AgentMessageKind
    sender_task_id: str
    sender_task_identifier: str
    sender_agent_name: str
    recipient_task_id: str
    recipient_task_identifier: str
    recipient_agent_name: str
    content: str
    question_identifier: str

