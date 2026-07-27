"""The daemon's SQLAlchemy layer: the declarative ``Base``, the ORM records that map the
history database's tables, and the lightweight additive schema-reconciliation applied on
startup.

Split into its own leaf module so the persistence schema is one self-contained thing that
``boot``, the services, and the routes depend on, rather than living amongst the request
handlers.
"""

from __future__ import annotations

from sqlalchemy import Boolean, Index, Integer, String, Text, inspect, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SessionRecord(Base):
    """A chat session — one A2A context. Tasks live in the A2A task store; this
    table only indexes sessions for the sidebar."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # == A2A contextId
    agent: Mapped[str] = mapped_column(String, nullable=False)
    # The project this session belongs to; the agent may address any of the project's
    # locations per tool call.
    project_id: Mapped[str] = mapped_column(String, default="")
    # Source path selected in the UI. Project-local agents/skills/instructions
    # are resolved from here.
    working_directory: Mapped[str] = mapped_column(Text, default="")
    # Actual path where shell and file tools run. For Git projects this is a
    # per-session worktree; for non-Git directories it falls back to the source.
    runtime_working_directory: Mapped[str] = mapped_column(Text, default="")
    workspace_strategy: Mapped[str] = mapped_column(Text, default="none")
    workspace_path: Mapped[str] = mapped_column(Text, default="")
    workspace_branch: Mapped[str] = mapped_column(Text, default="")
    source_repository_root: Mapped[str] = mapped_column(Text, default="")
    runtime_repository_root: Mapped[str] = mapped_column(Text, default="")
    workspace_head: Mapped[str] = mapped_column(Text, default="")
    workspace_error: Mapped[str] = mapped_column(Text, default="")
    title: Mapped[str] = mapped_column(Text, default="")
    # Per-session permission mode for future turns and frontend hydration.
    permission_mode: Mapped[str] = mapped_column(Text, default="default")
    input_draft: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (
        Index("idx_sessions_created_at", "created_at"),
        Index("idx_sessions_project", "project_id"),
    )


class SessionLifecycleRecord(Base):
    """Durable lifecycle facts for one chat session.

    This row is intentionally independent of ``SessionRecord`` because lifecycle events can
    occur before workspace preparation creates the sidebar record. Live execution machinery
    stays in memory; only facts that must survive runtime reconstruction belong here.
    """

    __tablename__ = "session_lifecycle"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    work_habits_acknowledged_at: Mapped[str] = mapped_column(String, default="")


class ProjectRecord(Base):
    """The internal grouping key for a set of locations and their sessions.

    Locations carry the user-facing identity; a project has no separate editable metadata.
    """

    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class LocationRecord(Base):
    """A named place a project runs tools in: the home server's own filesystem
    (``kind="local"``) or a remote reached over SSH (``kind="remote"``, referencing a
    ``~/.ssh/config`` host alias). ``permission_mode`` is the one execution policy a
    location carries (``read_only`` etc. is enforced per tool call); ``name`` is derived
    from the connection (host alias / folder), not user-entered. The model-facing location
    URI is generated from the resolved connection, not stored (so it can't go stale)."""

    __tablename__ = "locations"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # generated uuid
    project_id: Mapped[str] = mapped_column(String, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)  # derived project-scoped label
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # "local" | "remote"
    host_alias: Mapped[str] = mapped_column(Text, default="")  # SSH alias for remotes
    base_directory: Mapped[str] = mapped_column(Text, nullable=False)
    permission_mode: Mapped[str] = mapped_column(Text, default="default")
    created_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_locations_project", "project_id"),)


class ModelHistoryRecord(Base):
    """Recently selected models (provider/model id + label), mirroring the project
    history so a user can quickly switch back to a model they used before."""

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
    # Creation time, used to order a context's terminals into stable tabs; set once on
    # insert and never touched again (unlike updated_at, which moves on every write).
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)

    __table_args__ = (Index("idx_terminal_states_updated", "updated_at"),)


def _apply_history_schema(sync_engine) -> None:
    """Make the on-disk schema match the declarative models exactly — the models (and
    their ``__table_args__`` indexes) are the single source of truth. Missing tables and
    indexes are created; any existing table whose columns have drifted from its model (an
    older dev build) is dropped and recreated fresh. There is deliberately no
    backward-compatibility migration path: with no data worth preserving across a schema
    change, "make it proper" means recreate, not hand-patch individual columns."""
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
