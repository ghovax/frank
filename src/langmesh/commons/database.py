"""The daemon's SQLAlchemy layer: the declarative base, the history database's records, and its startup reconciliation."""

from __future__ import annotations

from sqlalchemy import Boolean, Index, String, Text, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# The key of the one row in `interface_preferences`, named so the reader and the writer cannot disagree.
SOLE_INTERFACE = "interface"


class SessionRecord(Base):
    """A chat session: one A2A context, and the durable half of what the registry knows."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # == A2A contextId
    agent: Mapped[str] = mapped_column(String, nullable=False)
    # The session that created this one, empty when a person did. What a subtree reap walks.
    parent: Mapped[str] = mapped_column(String, default="")
    # Does this session still exist? `live` or `ended` — never what it is *doing*.
    lifecycle: Mapped[str] = mapped_column(String, default="live")
    # How it finished, and why, for the record that outlives it.
    outcome: Mapped[str] = mapped_column(String, default="")
    exit_reason: Mapped[str] = mapped_column(Text, default="")
    # What its tool children may touch, resolved and clamped once at creation and stored as JSON.
    sandbox: Mapped[str] = mapped_column(Text, default="")
    # The one-time work-habits acknowledgement, durable because a worker is per activation rather than per session.
    work_habits_acknowledged_at: Mapped[str] = mapped_column(String, default="")
    updated_at: Mapped[str] = mapped_column(String, default="")
    # The workspace this session belongs to, any of whose locations the agent may address per call.
    workspace_id: Mapped[str] = mapped_column(String, default="")
    # The source path selected in the interface, from which project-local agents and skills are resolved.
    working_directory: Mapped[str] = mapped_column(Text, default="")
    # Where shell and file tools actually run, which for a Git project is a per-session worktree.
    runtime_working_directory: Mapped[str] = mapped_column(Text, default="")
    worktree_strategy: Mapped[str] = mapped_column(Text, default="none")
    worktree_path: Mapped[str] = mapped_column(Text, default="")
    worktree_branch: Mapped[str] = mapped_column(Text, default="")
    source_repository_root: Mapped[str] = mapped_column(Text, default="")
    runtime_repository_root: Mapped[str] = mapped_column(Text, default="")
    worktree_head: Mapped[str] = mapped_column(Text, default="")
    worktree_error: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    # Per-session permission mode for future turns and frontend hydration.
    permission_mode: Mapped[str] = mapped_column(Text, default="ask")
    input_draft: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_sessions_created_at", "created_at"),
        Index("idx_sessions_workspace", "workspace_id"),
        Index("idx_sessions_lifecycle", "lifecycle"),
    )


class MachineRecord(Base):
    """Another LangMesh this one knows how to reach, and the credential for it."""

    __tablename__ = "machines"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    # What this machine is called here, seeded from the pairing payload and editable because that name is arbitrary.
    name: Mapped[str] = mapped_column(String, nullable=False, default="")
    # The identity of the row, so pairing the same machine again replaces its token rather than growing a stale entry.
    endpoint: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)


class WorkspaceRecord(Base):
    """A set of locations and the sessions that run against them; the locations carry the user-facing identity."""

    __tablename__ = "workspaces"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)
    # The conversation a client opens when it arrives with nothing else to go on, kept here rather than in a browser.
    last_session_id: Mapped[str] = mapped_column(String, nullable=False, default="")


class InterfacePreferenceRecord(Base):
    """How the interface should look and where it should open, as one row of named columns."""

    __tablename__ = "interface_preferences"

    # There is one interface, so there is one row; the column exists because a table needs a key.
    id: Mapped[str] = mapped_column(String, primary_key=True, default=SOLE_INTERFACE)
    # "system" follows the operating system; "light" and "dark" are the explicit choices.
    color_mode: Mapped[str] = mapped_column(String, nullable=False, default="system")
    # A BCP-47 tag the interface has messages for; empty means it has not been chosen.
    locale: Mapped[str] = mapped_column(String, nullable=False, default="")
    # The workspace a fresh launch reopens. Empty until one has been opened.
    last_workspace_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    # Set when computer control is asked for without the grant, so the request outlives the process that took it.
    computer_control_awaiting_grant: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )


class ScheduleRecord(Base):
    """A prompt to run in a workspace on a schedule, with nobody watching, stating its own permission mode."""

    __tablename__ = "schedules"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    cron: Mapped[str] = mapped_column(String, nullable=False)
    timezone: Mapped[str] = mapped_column(String, nullable=False)
    agent: Mapped[str] = mapped_column(String, nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    permission_mode: Mapped[str] = mapped_column(String, nullable=False)
    working_directory: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # When the scheduler last acted on this, so a daemon that was down catches up once rather than replaying every firing.
    last_fired_at: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_session_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_schedules_workspace", "workspace_id"),)


class LocationRecord(Base):
    """A named place a workspace runs tools in, local or reached over SSH."""

    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    workspace_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # derived workspace-scoped label
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # "local" | "remote"
    host_alias: Mapped[str] = mapped_column(Text, default="")  # SSH alias for remotes
    base_directory: Mapped[str] = mapped_column(Text, nullable=False)
    permission_mode: Mapped[str] = mapped_column(Text, default="ask")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_locations_workspace", "workspace_id"),)


class ModelHistoryRecord(Base):
    """Recently selected models, mirroring the project history so a user can switch back to one they used before."""

    __tablename__ = "model_history"

    model_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, default="")
    provider: Mapped[str] = mapped_column(Text, default="")
    selected_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_model_history_selected_at", "selected_at"),)


class TerminalStateRecord(Base):
    """Persisted scrollback for a server-owned terminal session."""

    __tablename__ = "terminal_states"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    terminal_key: Mapped[str] = mapped_column(String, primary_key=True)
    working_directory: Mapped[str] = mapped_column(Text, default="")
    scrollback: Mapped[str] = mapped_column(Text, default="")
    # Creation time, used to order a context's terminals into stable tabs, set once and never touched again.
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_terminal_states_updated", "updated_at"),)


def _apply_history_schema(sync_engine) -> None:
    """Make the on-disk schema match the declarative models, which are the single source of truth."""
    inspector = inspect(sync_engine)
    existing_tables = set(inspector.get_table_names())
    drifted_tables = []
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue
        model_columns = {column.name for column in table.columns}
        live_columns = {column["name"] for column in inspector.get_columns(table_name)}
        if model_columns != live_columns:
            drifted_tables.append(table)
    if drifted_tables:
        with sync_engine.begin() as connection:
            for table in drifted_tables:
                connection.execute(text(f"DROP TABLE {table.name}"))
    Base.metadata.create_all(sync_engine)
